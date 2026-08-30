"""Editorial context packaging for long-form article synthesis."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from src.domain.operational_state import ResolvedObservation
from src.editorial_models import SourceRecord, StoryCard
from src.publication.digest_contracts import GENERIC_FALLBACK_TOPICS
from src.publication.evidence import PublicationEvidence

_FRAG_ID_RE = re.compile(r":frag:(\d+)")
_ITEM_ID_RE = re.compile(r":item:(\d+)")

TemporalRole = Literal["CURRENT_WINDOW", "HISTORICAL_CONTEXT", "FUTURE_SCHEDULED"]


@dataclass(frozen=True)
class PublicationWindow:
    """Explicit frozen reporting window for publication generation."""

    snapshot_at: dt.datetime
    lookback_start: dt.datetime


def _normalize_dt(d: dt.datetime | None) -> dt.datetime | None:
    if d is None:
        return None
    if d.tzinfo is None:
        return d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def classify_support_temporal_role(
    *,
    observed_at: dt.datetime | None,
    effective_from: dt.datetime | None,
    effective_until: dt.datetime | None,
    support_kind: Literal["evidence", "operational"],
    window: PublicationWindow,
) -> TemporalRole:
    """Classify an ArticleSupport item into its temporal reporting role."""
    snap = _normalize_dt(window.snapshot_at)
    start = _normalize_dt(window.lookback_start)
    ef = _normalize_dt(effective_from)
    eu = _normalize_dt(effective_until)
    obs = _normalize_dt(observed_at)

    if snap is None or start is None:
        return "CURRENT_WINDOW"

    # 1. effective_from > snapshot_at -> FUTURE_SCHEDULED
    if ef is not None and ef > snap:
        return "FUTURE_SCHEDULED"

    # 2. lookback_start <= observed_at <= snapshot_at -> CURRENT_WINDOW
    if obs is not None and start <= obs <= snap:
        return "CURRENT_WINDOW"

    # 3. for support_kind == "operational", if effective_from <= snapshot_at and (effective_until is None or effective_until >= snapshot_at) -> CURRENT_WINDOW
    if support_kind == "operational" and ef is not None and ef <= snap:
        if eu is None or eu >= snap:
            return "CURRENT_WINDOW"

    # 4. otherwise -> HISTORICAL_CONTEXT
    return "HISTORICAL_CONTEXT"


@dataclass(frozen=True)
class ArticleSupport:
    """Unified citable support packet for article writer units."""

    support_id: str
    text: str
    source_text: str
    support_kind: Literal["evidence", "operational"]
    publication_use: Literal["PUBLISH", "CONTEXT"]
    source_refs: tuple[str, ...]
    fragment_ids: tuple[int, ...]
    source_item_ids: tuple[int, ...]
    observed_at: dt.datetime | None
    effective_from: dt.datetime | None = None
    effective_until: dt.datetime | None = None
    temporal_role: TemporalRole = "CURRENT_WINDOW"


@dataclass(frozen=True)
class ArticleEditorialContext:
    """Rich, structured editorial context for long-form article generation."""

    headline_candidates: tuple[str, ...]
    support_index: tuple[ArticleSupport, ...]
    support_by_id: dict[str, ArticleSupport]
    recurring_topics: tuple[str, ...]
    operational_timeline: tuple[ResolvedObservation, ...] = ()
    evidence_index: tuple[PublicationEvidence, ...] = ()
    evidence_by_id: dict[str, PublicationEvidence] | None = None
    general_facts: tuple[PublicationEvidence, ...] = ()
    resident_observations: tuple[PublicationEvidence, ...] = ()
    publication_window: PublicationWindow | None = None

    def to_prompt_context(self) -> str:
        """Render deterministic, structured support context for the single article writing LLM call."""
        blocks: list[str] = []
        if self.publication_window is not None:
            blocks.append(
                f"REPORT WINDOW: {self.publication_window.lookback_start.isoformat()} .. {self.publication_window.snapshot_at.isoformat()}"
            )
        for sup in self.support_index:
            if sup.publication_use == "EXCLUDE":
                continue
            lines = [
                f"[SUPPORT {sup.support_id}]",
                f"role={sup.temporal_role} kind={sup.support_kind} publication_use={sup.publication_use}",
            ]
            if sup.observed_at:
                lines.append(f"observed_at={sup.observed_at.isoformat()}")
            if sup.effective_from:
                lines.append(f"effective_from={sup.effective_from.isoformat()}")
            if sup.effective_until:
                lines.append(f"effective_until={sup.effective_until.isoformat()}")
            lines.append(f"fact={sup.text}")
            if sup.source_text:
                lines.append(f"source={sup.source_text}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks).strip()


def build_article_editorial_context(
    cards: Sequence[StoryCard],
    evidence_items: Sequence[PublicationEvidence],
    operational_observations: Sequence[ResolvedObservation] = (),
    source_records: Mapping[str, SourceRecord] | None = None,
    *,
    snapshot_at: dt.datetime | None = None,
    lookback_hours: int | None = None,
    publication_window: PublicationWindow | None = None,
) -> ArticleEditorialContext:
    """Build structured ArticleEditorialContext with unified ArticleSupport packets."""
    records = source_records or {}

    pub_win: PublicationWindow | None = None
    if publication_window is not None:
        pub_win = publication_window
    elif snapshot_at is not None and lookback_hours is not None:
        snap_norm = _normalize_dt(snapshot_at)
        if snap_norm is not None:
            pub_win = PublicationWindow(
                snapshot_at=snap_norm,
                lookback_start=snap_norm - dt.timedelta(hours=lookback_hours),
            )

    # Filter out generic fallback titles from headline candidates
    headlines: list[str] = []
    seen_headlines: set[str] = set()
    for card in cards:
        cand = (card.topic or card.summary or "").strip()
        if cand and cand.lower() not in {t.lower() for t in GENERIC_FALLBACK_TOPICS}:
            if cand not in seen_headlines:
                seen_headlines.add(cand)
                headlines.append(cand)

    support_list: list[ArticleSupport] = []
    general_facts: list[PublicationEvidence] = []
    resident_obs: list[PublicationEvidence] = []
    recurring_topics: set[str] = set()
    evidence_by_id: dict[str, PublicationEvidence] = {}

    for evi in evidence_items:
        evidence_by_id[evi.evidence_id] = evi
        if evi.publication_use == "EXCLUDE":
            continue

        if evi.kind in ("community_observation", "quote_assertion"):
            resident_obs.append(evi)
        else:
            general_facts.append(evi)

        # Build ArticleSupport for allowed evidence
        temporal_role: TemporalRole = "CURRENT_WINDOW"
        if pub_win is not None:
            temporal_role = classify_support_temporal_role(
                observed_at=evi.observed_at,
                effective_from=None,
                effective_until=None,
                support_kind="evidence",
                window=pub_win,
            )

        support_list.append(
            ArticleSupport(
                support_id=evi.evidence_id,
                text=evi.text,
                source_text=evi.source_text or evi.text,
                support_kind="evidence",
                publication_use=evi.publication_use,  # "PUBLISH" or "CONTEXT"
                source_refs=(evi.source_ref,) if evi.source_ref else (),
                fragment_ids=(evi.fragment_id,) if evi.fragment_id else (),
                source_item_ids=(evi.source_item_id,) if evi.source_item_id else (),
                observed_at=evi.observed_at,
                temporal_role=temporal_role,
            )
        )

    # Build ArticleSupport for operational observations
    for obs_res in operational_observations:
        obs = obs_res.observation
        frag_ids = list(obs.source_fragment_ids)
        if not frag_ids:
            for ref in obs_res.source_refs:
                m = _FRAG_ID_RE.search(ref)
                if m:
                    frag_ids.append(int(m.group(1)))

        if not frag_ids:
            frag_ids = [0]

        for fid in frag_ids:
            if fid != 0:
                sup_id = f"op:{obs.subject_key}:{obs.dimension}:frag:{fid}"
            else:
                sup_id = f"op:{obs.subject_key}:{obs.dimension}"

            # Find matching source ref
            matching_refs = [ref for ref in obs_res.source_refs if f":frag:{fid}" in ref] or list(
                obs_res.source_refs
            )

            source_text = ""
            for ref in matching_refs:
                if ref in records:
                    source_text = records[ref].message.text
                    break
            if not source_text:
                source_text = obs.detail or ""

            item_ids: list[int] = []
            for ref in matching_refs:
                m_item = _ITEM_ID_RE.search(ref)
                if m_item:
                    item_ids.append(int(m_item.group(1)))

            fact_parts: list[str] = []
            if obs.subject_label:
                fact_parts.append(f"[{obs.subject_label}]")
            if obs.location or obs.entity:
                loc_ent = ": ".join([p for p in (obs.location, obs.entity) if p])
                fact_parts.append(loc_ent)
            if obs.state or obs.detail:
                st_det = " — ".join([p for p in (obs.state, obs.detail) if p])
                fact_parts.append(st_det)
            fact_text = " ".join(fact_parts) if fact_parts else obs.detail

            op_role: TemporalRole = "CURRENT_WINDOW"
            if pub_win is not None:
                op_role = classify_support_temporal_role(
                    observed_at=obs_res.observed_at,
                    effective_from=obs_res.effective_from,
                    effective_until=obs_res.effective_until,
                    support_kind="operational",
                    window=pub_win,
                )

            support_list.append(
                ArticleSupport(
                    support_id=sup_id,
                    text=fact_text,
                    source_text=source_text,
                    support_kind="operational",
                    publication_use="PUBLISH",
                    source_refs=tuple(matching_refs),
                    fragment_ids=(fid,) if fid != 0 else (),
                    source_item_ids=tuple(dict.fromkeys(item_ids)),
                    observed_at=obs_res.observed_at,
                    effective_from=obs_res.effective_from,
                    effective_until=obs_res.effective_until,
                    temporal_role=op_role,
                )
            )

    for card in cards:
        if card.category:
            recurring_topics.add(card.category)
        for tag in card.tags:
            recurring_topics.add(tag)

    sorted_timeline = tuple(sorted(operational_observations, key=lambda x: x.observed_at))
    support_by_id = {s.support_id: s for s in support_list}

    return ArticleEditorialContext(
        headline_candidates=tuple(headlines),
        support_index=tuple(support_list),
        support_by_id=support_by_id,
        recurring_topics=tuple(sorted(recurring_topics)),
        operational_timeline=sorted_timeline,
        evidence_index=tuple(evidence_items),
        evidence_by_id=evidence_by_id,
        general_facts=tuple(general_facts),
        resident_observations=tuple(resident_obs),
        publication_window=pub_win,
    )
