"""Event-First editorial adapter mapping rich story clusters and frozen fragments to editorial packages."""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Sequence
from typing import Any

import psycopg

from src.collector import Message
from src.db.uow import DatabaseUnitOfWork
from src.domain.event_payload import OperationalObservationPayload
from src.editorial_models import (
    EditorialAnalysis,
    PreparedBundle,
    SourceRecord,
    StoryCard,
    StoryElement,
    Uncertainty,
)
from src.processing.event_analysis import EventAnalysisPayload
from src.publication.editorial_adapter import FrozenEditorialInput
from src.publication.models import PublicationInput
from src.publication.repository import PublicationRepository

logger = logging.getLogger(__name__)


def _evidence_story_element(
    *,
    text: str,
    kind: str,
    source_refs: list[str],
    attribution: str,
) -> tuple[str, StoryElement]:
    status = "established" if kind == "established_fact" else "attributed"
    element = StoryElement(
        text=text,
        source_refs=source_refs,
        status=status,
        attribution=attribution if status == "attributed" else "",
    )

    if kind == "community_report":
        return "community_observations", element
    if kind == "service_access":
        return "useful_details", element
    return "hard_facts", element


def _attribution_for_refs(source_refs: list[str], records: dict[str, SourceRecord]) -> str:
    names: list[str] = []
    for ref in source_refs:
        record = records.get(ref)
        if record is None:
            continue
        name = record.message.channel_name or record.message.sender or record.source_type
        if name and name not in names:
            names.append(name)
    return ", ".join(names)


class EventEditorialAdapter:
    """Adapts event-first publication inputs into frozen editorial bundles."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        repo: PublicationRepository | None = None,
    ) -> None:
        self.uow = uow
        self.repo = repo or PublicationRepository()

    async def adapt_inputs(
        self,
        run_id: int,
        *,
        inputs: list[PublicationInput] | None = None,
    ) -> FrozenEditorialInput:
        """Load frozen publication inputs and map rich event payloads into StoryCards and source bundles."""
        async with self.uow.transaction() as conn:
            return await self.adapt_inputs_on(conn, run_id, inputs=inputs)

    async def adapt_inputs_on(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        *,
        inputs: list[PublicationInput] | None = None,
    ) -> FrozenEditorialInput:
        if inputs is None:
            inputs = await self.repo.load_sealed_inputs(conn, run_id)

        story_cards: list[StoryCard] = []
        records: dict[str, SourceRecord] = {}
        all_evidence: dict[str, Any] = {}
        pure_op_story_ids: set[int] = set()
        all_observations_with_time: list[
            tuple[OperationalObservationPayload, dt.datetime, Sequence[str]]
        ] = []

        for _rank, inp in enumerate(inputs, start=1):
            # 1. Fetch story revision event_payload
            cursor = await conn.execute(
                """
                SELECT sr.id, sr.title, sr.summary, sr.semantic_text, sr.event_payload,
                       s.created_at, sr.created_at
                FROM story_revisions sr
                JOIN stories s ON s.id = sr.story_id
                WHERE sr.id = %s
                """,
                (inp.story_revision_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                continue

            raw_payload = row[4]
            if isinstance(raw_payload, str):
                try:
                    payload_dict = json.loads(raw_payload)
                except Exception:
                    payload_dict = {}
            elif isinstance(raw_payload, dict):
                payload_dict = raw_payload
            else:
                payload_dict = {}

            payload = EventAnalysisPayload.from_dict(payload_dict) if payload_dict else None

            # 2. Fetch frozen fragments with source metadata
            f_cur = await conn.execute(
                """
                SELECT f.id, f.text_content, s.id, s.platform, s.name, s.role, s.url,
                       s.external_id, si.id, sir.id, si.canonical_url, si.author_name,
                       COALESCE(si.published_at, si.first_collected_at, f.created_at)
                FROM source_fragments f
                JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
                JOIN source_items si ON si.id = sir.source_item_id
                JOIN sources s ON s.id = si.source_id
                WHERE f.id = ANY(%s)
                ORDER BY f.id ASC
                """,
                (inp.fragment_ids or [0],),
            )
            frag_rows = await f_cur.fetchall()

            card_source_refs: list[str] = []
            frag_id_to_ref: dict[int, str] = {}
            frag_ts_map: dict[int, dt.datetime] = {}
            frag_meta_map: dict[int, dict[str, Any]] = {}
            for f_row in frag_rows:
                (
                    fid,
                    ftext,
                    src_id,
                    platform,
                    src_name,
                    src_role,
                    src_url,
                    src_ext_id,
                    item_id,
                    rev_id,
                    canon_url,
                    author_name,
                    collected_at,
                ) = f_row

                ref_key = f"{platform}:source:{src_id}:item:{item_id}:rev:{rev_id}:frag:{fid}"
                card_source_refs.append(ref_key)
                frag_id_to_ref[fid] = ref_key
                obs_time = (
                    collected_at
                    if isinstance(collected_at, dt.datetime)
                    else (row[6] or row[5] or dt.datetime.now(dt.timezone.utc))
                )
                frag_ts_map[fid] = obs_time
                frag_meta_map[fid] = {
                    "source_id": src_id,
                    "source_item_id": item_id,
                    "source_role": src_role or "unknown",
                    "observed_at": obs_time,
                    "source_ref": ref_key,
                    "source_text": ftext,
                }

                if ref_key not in records:
                    link = canon_url or src_url or f"https://t.me/{str(src_ext_id).lstrip('@')}"
                    msg = Message(
                        text=ftext,
                        sender=author_name or src_name or "unknown",
                        timestamp=obs_time,
                        link=link,
                        channel_id=str(src_ext_id or src_id),
                        channel_name=src_name or platform,
                    )
                    records[ref_key] = SourceRecord(
                        ref=ref_key,
                        message=msg,
                        source_type=src_role or "unknown",
                    )

            # Build PublicationEvidence items
            from src.publication.evidence import PublicationEvidence

            if payload and payload.evidence_items:
                for item_idx, evi in enumerate(payload.evidence_items):
                    for fid in evi.source_fragment_ids:
                        if fid in frag_meta_map:
                            meta = frag_meta_map[fid]
                            evi_id = f"story:{inp.story_id}:evidence:{item_idx}:frag:{fid}"
                            all_evidence[evi_id] = PublicationEvidence(
                                evidence_id=evi_id,
                                story_id=inp.story_id,
                                text=evi.text,
                                source_text=str(meta["source_text"]),
                                kind=evi.kind,
                                publication_use=evi.publication_use,
                                fragment_id=fid,
                                source_ref=meta["source_ref"],
                                source_id=meta["source_id"],
                                source_item_id=meta["source_item_id"],
                                source_role=meta["source_role"],
                                observed_at=meta["observed_at"],
                            )
            elif frag_rows:
                facts = payload.key_facts if (payload and payload.key_facts) else []
                if facts:
                    for f_idx, fact in enumerate(facts):
                        for fid, meta in frag_meta_map.items():
                            evi_id = f"story:{inp.story_id}:evidence:{f_idx}:frag:{fid}"
                            all_evidence[evi_id] = PublicationEvidence(
                                evidence_id=evi_id,
                                story_id=inp.story_id,
                                text=fact,
                                source_text=str(meta["source_text"]),
                                kind="established_fact",
                                publication_use="PUBLISH",
                                fragment_id=fid,
                                source_ref=meta["source_ref"],
                                source_id=meta["source_id"],
                                source_item_id=meta["source_item_id"],
                                source_role=meta["source_role"],
                                observed_at=meta["observed_at"],
                            )
                else:
                    for fid, meta in frag_meta_map.items():
                        evi_id = f"story:{inp.story_id}:evidence:0:frag:{fid}"
                        all_evidence[evi_id] = PublicationEvidence(
                            evidence_id=evi_id,
                            story_id=inp.story_id,
                            text=str(meta["source_text"]),
                            source_text=str(meta["source_text"]),
                            kind="established_fact",
                            publication_use="PUBLISH",
                            fragment_id=fid,
                            source_ref=meta["source_ref"],
                            source_id=meta["source_id"],
                            source_item_id=meta["source_item_id"],
                            source_role=meta["source_role"],
                            observed_at=meta["observed_at"],
                        )

            # 3. Build StoryCard
            headline = (
                payload.headline
                if payload and payload.headline
                else (row[1] or row[3] or f"Событие #{inp.story_id}")
            )
            digest_summary = (
                payload.digest_summary
                if payload and payload.digest_summary
                else (row[2] or row[3] or "")
            )

            fallback_refs = card_source_refs or [f"story:{inp.story_id}"]

            hard_facts: list[StoryElement] = []
            community_evidence: list[StoryElement] = []
            useful_details: list[StoryElement] = []
            uncertainties: list[Uncertainty] = []

            if payload and payload.evidence_items:
                for evi in payload.evidence_items:
                    if evi.publication_use == "EXCLUDE":
                        continue
                    source_refs = [
                        frag_id_to_ref[fid]
                        for fid in evi.source_fragment_ids
                        if fid in frag_id_to_ref
                    ] or fallback_refs
                    if evi.kind == "resident_question":
                        uncertainties.append(
                            Uncertainty(
                                text=evi.text,
                                basis="resident_question",
                                related_source_refs=source_refs,
                            )
                        )
                        continue
                    attribution = _attribution_for_refs(source_refs, records)
                    bucket, element = _evidence_story_element(
                        text=evi.text,
                        kind=evi.kind,
                        source_refs=source_refs,
                        attribution=attribution,
                    )
                    if bucket == "community_observations":
                        community_evidence.append(element)
                    elif bucket == "useful_details":
                        useful_details.append(element)
                    else:
                        hard_facts.append(element)
            else:
                hard_facts = [
                    StoryElement(
                        text=fact,
                        source_refs=fallback_refs,
                        status="established",
                    )
                    for fact in (payload.key_facts if payload else [])
                ]

            op_obs_elements: list[StoryElement] = []
            if payload and payload.operational_observations:
                for obs in payload.operational_observations:
                    obs_refs = [
                        frag_id_to_ref[fid]
                        for fid in obs.source_fragment_ids
                        if fid in frag_id_to_ref
                    ]
                    if not obs_refs:
                        obs_refs = fallback_refs

                    obs_parts = []
                    if obs.subject_label:
                        obs_parts.append(f"[{obs.subject_label}]")
                    loc_ent = ": ".join([p for p in (obs.location, obs.entity) if p])
                    if loc_ent:
                        obs_parts.append(loc_ent)
                    state_det = " — ".join([p for p in (obs.state, obs.detail) if p])
                    if state_det:
                        obs_parts.append(state_det)
                    obs_text = " ".join(obs_parts) if obs_parts else obs.detail
                    if obs_text and obs_text.strip():
                        op_obs_elements.append(
                            StoryElement(
                                text=obs_text.strip(),
                                source_refs=obs_refs,
                                status="attributed",
                                areas=[obs.location] if obs.location else [],
                            )
                        )

                    # Expand multi-fragment observation into separately timestamped entries
                    for fid in obs.source_fragment_ids:
                        if fid in frag_ts_map:
                            f_ref = [frag_id_to_ref[fid]] if fid in frag_id_to_ref else obs_refs
                            all_observations_with_time.append((obs, frag_ts_map[fid], f_ref))
                    if not obs.source_fragment_ids:
                        obs_ts = (
                            row[6]
                            if isinstance(row[6], dt.datetime)
                            else dt.datetime.now(dt.timezone.utc)
                        )
                        all_observations_with_time.append((obs, obs_ts, obs_refs))

            legacy_community_observations = [
                StoryElement(
                    text=obs,
                    source_refs=fallback_refs,
                    status="attributed",
                )
                for obs in (payload.community_observations if payload else [])
            ]

            importance = "high" if payload and payload.urgency in ("critical", "high") else "medium"
            # Check if this story is a pure operational update
            if payload and payload.operational_observations:
                has_non_op_evidence = any(
                    evi.kind not in ("service_access", "utility_status")
                    and evi.publication_use == "PUBLISH"
                    for evi in payload.evidence_items
                )
                cat = (payload.category or "").lower()
                tags = {t.lower() for t in payload.tags}
                is_utility_domain = (
                    cat
                    in (
                        "utilities",
                        "municipal_service",
                        "municipal_infrastructure",
                        "transport",
                        "banking",
                        "telecom",
                    )
                    or bool(
                        tags.intersection(
                            {
                                "utilities",
                                "жкх",
                                "коммуналка",
                                "водоснабжение",
                                "электроснабжение",
                                "газоснабжение",
                                "транспорт",
                                "связь",
                            }
                        )
                    )
                    or cat == ""
                )
                if is_utility_domain and not has_non_op_evidence:
                    pure_op_story_ids.add(inp.story_id)

            if payload and payload.evidence_items:
                has_publishable_evidence = any(
                    evi.publication_use == "PUBLISH" and evi.kind != "resident_question"
                    for evi in payload.evidence_items
                )
                has_valid_operational = bool(payload.operational_observations)
                should_emit_card = has_publishable_evidence or has_valid_operational
            else:
                should_emit_card = True

            if should_emit_card:
                card = StoryCard(
                    id=f"story:{inp.story_id}",
                    topic=headline,
                    importance=importance,
                    summary=digest_summary or headline,
                    tags=list(payload.tags) if payload else [],
                    rubric_id="",
                    category=payload.category if payload else "",
                    representative_source_refs=card_source_refs,
                    hard_facts=hard_facts,
                    community_observations=community_evidence
                    + legacy_community_observations
                    + op_obs_elements,
                    useful_details=useful_details,
                    uncertainties=uncertainties,
                )
                story_cards.append(card)

        # Build CitySituationRollup for digest publication runs
        run = await self.repo.get_run_by_id(conn, run_id)
        city_rollup = None
        if run is not None and run.publication_type in (
            "digest_grouped",
            "digest_channel",
            "digest",
        ):
            if all_observations_with_time:
                from src.domain.operational_state import resolve_operational_states
                from src.publication.city_situation import build_city_situation_rollup

                resolved_states = resolve_operational_states(all_observations_with_time)
                city_rollup = (
                    build_city_situation_rollup(resolved_states) if resolved_states else None
                )

            # When rollup is active, suppress pure operational cards so they are not duplicated
            if city_rollup and city_rollup.items:
                story_cards = [
                    c
                    for c in story_cards
                    if not (
                        c.id.startswith("story:")
                        and c.id.split(":")[1].isdigit()
                        and int(c.id.split(":")[1]) in pure_op_story_ids
                    )
                ]

        # Build ArticleEditorialContext for article runs
        article_ctx = None
        if run is not None and run.publication_type == "article":
            eligibility = await self.repo.get_eligibility_policy_by_id(
                conn, run.eligibility_policy_id
            )
            if eligibility is None:
                raise ValueError(f"eligibility policy {run.eligibility_policy_id} not found")

            lookback_raw = eligibility.config.get("lookback_hours")
            if isinstance(lookback_raw, bool) or not isinstance(lookback_raw, (int, float)):
                raise ValueError("frozen eligibility policy missing numeric lookback_hours")
            lookback_hours = int(lookback_raw)
            if lookback_hours <= 0:
                raise ValueError("frozen lookback_hours must be positive")

            from src.domain.operational_state import ResolvedObservation, _parse_iso_ts
            from src.publication.article_context import build_article_editorial_context

            resolved_obs_list: list[ResolvedObservation] = []
            for obs, obs_ts, o_refs in all_observations_with_time:
                eff_from = _parse_iso_ts(obs.effective_from)
                eff_until = _parse_iso_ts(obs.effective_until)
                resolved_obs_list.append(
                    ResolvedObservation(
                        observation=obs,
                        observed_at=obs_ts,
                        source_refs=tuple(o_refs),
                        effective_from=eff_from,
                        effective_until=eff_until,
                    )
                )
            article_ctx = build_article_editorial_context(
                cards=story_cards,
                evidence_items=list(all_evidence.values()),
                operational_observations=resolved_obs_list,
                source_records=records,
                snapshot_at=run.snapshot_at,
                lookback_hours=lookback_hours,
            )

        analysis = EditorialAnalysis(
            cards=story_cards,
            city_situation=city_rollup,
            evidence=all_evidence,
            article_context=article_ctx,
        )
        bundle = PreparedBundle(
            records=records,
            prompt_text="",
            total_messages=len(records),
            candidate_count=len(story_cards),
        )

        return FrozenEditorialInput(
            analysis=analysis,
            writer_bundle=bundle,
            run_id=run_id,
        )


def format_event_digest_grouped(editorial_input: FrozenEditorialInput) -> str:
    """Render structured telegram HTML digest from Event-First StoryCards."""
    cards = editorial_input.analysis.cards
    if not cards:
        return "<b>Главные события дня</b>\n\nСобытий не зафиксировано."

    lines = ["<b>Главные события дня</b>\n"]
    for i, card in enumerate(cards, start=1):
        lines.append(f"<b>{i}. {card.topic}</b>")
        if card.summary:
            lines.append(f"{card.summary}")
        if card.hard_facts:
            for fact_elem in card.hard_facts[:3]:
                lines.append(f" • {fact_elem.text}")
        lines.append("")

    return "\n".join(lines).strip()
