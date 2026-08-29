"""Event-First editorial adapter mapping rich story clusters and frozen fragments to editorial packages."""

from __future__ import annotations

import datetime as dt
import json
import logging

import psycopg

from src.collector import Message
from src.db.uow import DatabaseUnitOfWork
from src.editorial_models import (
    EditorialAnalysis,
    PreparedBundle,
    SourceRecord,
    StoryCard,
    StoryElement,
)
from src.processing.event_analysis import EventAnalysisPayload
from src.publication.editorial_adapter import FrozenEditorialInput
from src.publication.models import PublicationInput
from src.publication.repository import PublicationRepository

logger = logging.getLogger(__name__)


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
                       COALESCE(si.first_collected_at, f.created_at)
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

                if ref_key not in records:
                    link = canon_url or src_url or f"https://t.me/{str(src_ext_id).lstrip('@')}"
                    msg = Message(
                        text=ftext,
                        sender=author_name or src_name or "unknown",
                        timestamp=collected_at if isinstance(collected_at, dt.datetime) else None,
                        link=link,
                        channel_id=str(src_ext_id or src_id),
                        channel_name=src_name or platform,
                    )
                    records[ref_key] = SourceRecord(
                        ref=ref_key,
                        message=msg,
                        source_type=src_role or "unknown",
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

            comm_obs = [
                StoryElement(
                    text=obs,
                    source_refs=fallback_refs,
                    status="attributed",
                )
                for obs in (payload.community_observations if payload else [])
            ] + op_obs_elements

            importance = "high" if payload and payload.urgency in ("critical", "high") else "medium"
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
                community_observations=comm_obs,
            )
            story_cards.append(card)

        analysis = EditorialAnalysis(
            cards=story_cards,
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
