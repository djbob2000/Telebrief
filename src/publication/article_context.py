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

    def to_prompt_context(self) -> str:
        """Render deterministic, structured support context for the single article writing LLM call."""
        blocks: list[str] = []
        for sup in self.support_index:
            if sup.publication_use == "EXCLUDE":
                continue
            lines = [
                f"[SUPPORT {sup.support_id}]",
                f"kind={sup.support_kind} publication_use={sup.publication_use}",
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
) -> ArticleEditorialContext:
    """Build structured ArticleEditorialContext with unified ArticleSupport packets."""
    records = source_records or {}

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
    )
