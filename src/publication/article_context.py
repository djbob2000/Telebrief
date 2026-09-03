"""Editorial context packaging for long-form article synthesis."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from src.domain.operational_state import ResolvedObservation
from src.editorial_models import SourceRecord, StoryCard
from src.publication.digest_contracts import GENERIC_FALLBACK_TOPICS
from src.publication.evidence import PublicationEvidence

_FRAG_ID_RE = re.compile(r":frag:(\d+)")
_ITEM_ID_RE = re.compile(r":item:(\d+)")

TemporalRole = Literal["CURRENT_WINDOW", "HISTORICAL_CONTEXT", "FUTURE_SCHEDULED"]


@dataclass(frozen=True)
class ArticleSelectionSignal:
    """Editorial priority signal assigned by selector model for an article story."""

    story_id: str
    intent: str
    rank: int


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
    evidence_kind: str = "established_fact"
    source_roles: tuple[str, ...] = ()
    story_id: str = ""


def _support_framing(support: ArticleSupport) -> str:
    if support.evidence_kind == "resident_question":
        return "question_context"
    if support.evidence_kind in {"community_report", "community_observation", "quote_assertion"}:
        return "attributed_report"
    if support.evidence_kind == "official_statement":
        return "official_attribution"
    if support.evidence_kind == "service_access":
        return "service_access_report"
    if support.support_kind == "operational" and support.source_roles == ("community",):
        return "attributed_report"
    return "factual_support"


def render_operational_state_fact(obs: object) -> str:
    """Render a reader-safe Russian factual sentence from an operational observation without technical leaks."""
    if hasattr(obs, "observation"):
        obs = obs.observation
    state_raw = getattr(obs, "state", None) or ""
    state_upper = state_raw.strip().upper()
    if state_upper not in {
        "AVAILABLE",
        "UNAVAILABLE",
        "DEGRADED",
        "RESTRICTED",
        "UNKNOWN",
        "SCHEDULED",
    }:
        raise ValueError(f"Unknown operational state: {state_raw!r}")

    subject = getattr(obs, "subject_label", "") or getattr(obs, "subject_key", "")
    location = getattr(obs, "location", "")
    entity = getattr(obs, "entity", "")
    detail = (getattr(obs, "detail", "") or "").strip()

    loc_ent_parts = [p for p in (location, entity) if p]
    loc_ent = ": ".join(loc_ent_parts) if loc_ent_parts else ""

    parts: list[str] = []
    if subject:
        parts.append(subject)
    if loc_ent:
        parts.append(loc_ent)

    header = " — ".join(parts) if parts else ""

    if state_upper == "SCHEDULED":
        state_desc = f"запланировано: {detail}" if detail else "запланировано"
    elif state_upper == "UNKNOWN":
        state_desc = detail or "ситуация уточняется"
    elif state_upper == "UNAVAILABLE":
        state_desc = f"отсутствует — {detail}" if detail else "отсутствует"
    elif state_upper == "AVAILABLE":
        state_desc = f"работает — {detail}" if detail else "работает"
    elif state_upper == "DEGRADED":
        state_desc = (
            f"работает с ограничениями — {detail}" if detail else "работает с ограничениями"
        )
    elif state_upper == "RESTRICTED":
        state_desc = f"ограничено — {detail}" if detail else "ограничено"
    else:
        raise ValueError(f"Unhandled operational state: {state_upper}")

    if header:
        return f"{header}: {state_desc}"
    return state_desc


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
    edition_name: str = ""
    edition_anchor_terms: tuple[str, ...] = ()
    story_cards: tuple[StoryCard, ...] = ()
    selection_by_story: dict[str, ArticleSelectionSignal] = field(default_factory=dict)

    @property
    def supports(self) -> tuple[ArticleSupport, ...]:
        return self.support_index

    def to_prompt_context(self) -> str:
        """Render deterministic, structured support context for the single article writing LLM call."""
        blocks: list[str] = []
        if self.edition_name:
            blocks.append(f"EDITION CONTEXT: {self.edition_name}")
        if self.publication_window is not None:
            blocks.append(
                f"REPORT WINDOW: {self.publication_window.lookback_start.isoformat()} .. {self.publication_window.snapshot_at.isoformat()}"
            )
        for sup in self.support_index:
            if sup.publication_use == "EXCLUDE":
                continue
            roles = ",".join(sup.source_roles) if sup.source_roles else "unknown"
            lines = [
                f"[SUPPORT {sup.support_id}]",
                f"role={sup.temporal_role} kind={sup.support_kind} publication_use={sup.publication_use}",
                f"evidence_kind={sup.evidence_kind} source_roles={roles}",
                f"framing={_support_framing(sup)}",
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


def _edition_anchor_terms(edition_name: str) -> tuple[str, ...]:
    clean = edition_name.strip()
    if not clean:
        return ()
    try:
        from pathlib import Path

        import yaml

        from src.domain.edition_geography import resolve_edition_geography

        slug = clean.lower()
        if "бердян" in slug:
            slug = "berdyansk"
        geo = resolve_edition_geography(slug, clean)
        terms = {clean}
        terms.update(geo.target_locations)
        terms.update(geo.district_locations)

        profile_path = Path(f"data/city_profiles/{slug}.yaml")
        if profile_path.exists():
            with open(profile_path, "r", encoding="utf-8") as f:
                pdata = yaml.safe_load(f) or {}
            c_geo = pdata.get("stable_context", {}).get("geography", {})

            def add_name_or_alias(item: Any) -> None:
                if isinstance(item, str) and item.strip():
                    terms.add(item.strip())
                elif isinstance(item, dict):
                    for k in ("name", "canonical_name", "alias", "area_name", "text"):
                        val = item.get(k)
                        if isinstance(val, str) and val.strip():
                            terms.add(val.strip())

            for area in c_geo.get("editorial_scale_area_set", ()):
                add_name_or_alias(area)
                for alias in area.get("aliases", ()) if isinstance(area, dict) else ():
                    add_name_or_alias(alias)
            for aset in c_geo.get("area_sets", ()):
                for area in aset.get("areas", ()) if isinstance(aset, dict) else ():
                    add_name_or_alias(area)
                    for alias in area.get("aliases", ()) if isinstance(area, dict) else ():
                        add_name_or_alias(alias)
            for landmark in c_geo.get("landmarks", ()):
                add_name_or_alias(landmark)
                for alias in landmark.get("aliases", ()) if isinstance(landmark, dict) else ():
                    add_name_or_alias(alias)
            sg = c_geo.get("street_gazetteer", {})
            if isinstance(sg, dict):
                for entry in sg.get("entries", ()):
                    add_name_or_alias(entry)
                    for alias in entry.get("aliases", ()) if isinstance(entry, dict) else ():
                        add_name_or_alias(alias)

        return tuple(sorted(terms))
    except Exception:
        return (clean,)


def build_article_editorial_context(
    cards: Sequence[StoryCard],
    evidence_items: Sequence[PublicationEvidence],
    operational_observations: Sequence[ResolvedObservation] = (),
    source_records: Mapping[str, SourceRecord] | None = None,
    *,
    snapshot_at: dt.datetime | None = None,
    lookback_hours: int | None = None,
    publication_window: PublicationWindow | None = None,
    edition_name: str = "",
    selection_by_story: dict[str, ArticleSelectionSignal] | None = None,
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
                evidence_kind=evi.kind,
                source_roles=(evi.source_role,) if evi.source_role else (),
                story_id=f"story:{evi.story_id}" if evi.story_id is not None else "",
            )
        )

    story_id_by_source_ref: dict[str, str] = {}
    for card in cards:
        for ref in card.representative_source_refs:
            story_id_by_source_ref.setdefault(ref, card.id)

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

            fact_text = render_operational_state_fact(obs)

            op_role: TemporalRole = "CURRENT_WINDOW"
            if pub_win is not None:
                op_role = classify_support_temporal_role(
                    observed_at=obs_res.observed_at,
                    effective_from=obs_res.effective_from,
                    effective_until=obs_res.effective_until,
                    support_kind="operational",
                    window=pub_win,
                )

            source_roles = tuple(
                dict.fromkeys(
                    records[ref].source_type
                    for ref in matching_refs
                    if ref in records and records[ref].source_type
                )
            )

            op_story_ids = [
                story_id_by_source_ref[ref]
                for ref in matching_refs
                if ref in story_id_by_source_ref
            ]
            op_story_id = op_story_ids[0] if op_story_ids else ""

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
                    evidence_kind="operational_observation",
                    source_roles=source_roles,
                    story_id=op_story_id,
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
        edition_name=edition_name,
        edition_anchor_terms=_edition_anchor_terms(edition_name),
        story_cards=tuple(cards),
        selection_by_story=selection_by_story or {},
    )
