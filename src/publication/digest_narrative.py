"""Deterministic block planning, models, validation, and single-call writer for Event-First narrative digests."""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from src.editorial_models import StoryCard
from src.publication.article_claims import ConcreteClaim, find_unsupported_claims
from src.publication.digest_presentation import RequiredDigestFact
from src.publication.errors import DigestCoverageInvariantError
from src.publication.evidence import PublicationEvidence

logger = logging.getLogger(__name__)

_INTERNAL_LEAKAGE_RE = re.compile(r"\[(?:story:\d+|SUPPORT\s+\d+|ref-\d+|tg:\S+)\]", re.IGNORECASE)


@dataclass(frozen=True)
class DigestNarrativeValidationResult:
    """Outcome of validating a narrative digest draft against a deterministic plan."""

    is_valid: bool
    violations: tuple[str, ...]
    unsupported_claims: tuple[ConcreteClaim, ...]


@dataclass(frozen=True)
class DigestNarrativeBlock:
    """Immutable presentation block grouping a fixed subset of rubric story cards."""

    block_id: str
    rubric_id: str
    rubric_title: str
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    canonical_notes: tuple[str, ...]
    required_facts: tuple[RequiredDigestFact, ...] = ()
    detail_support_ids_by_story: tuple[tuple[str, tuple[str, ...]], ...] = ()
    merge_group_by_story: tuple[tuple[str, str], ...] = ()
    detail_roles_by_story: tuple[tuple[str, str], ...] = ()
    presentation_modes_by_story: tuple[tuple[str, str], ...] = ()
    dashboard_support_ids_by_story: tuple[tuple[str, tuple[str, ...]], ...] = ()
    required_story_groups: tuple[tuple[str, ...], ...] = ()
    support_ids_by_story: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class DigestNarrativePlan:
    """Deterministic plan of immutable narrative digest blocks."""

    blocks: tuple[DigestNarrativeBlock, ...]


@dataclass(frozen=True)
class DigestSituationItemDraft:
    """A single rendered operational item within the City Situation section."""

    group_id: str
    label: str
    body: str
    cited_support_ids: tuple[str, ...]
    emoji: str = ""
    claims: tuple[DigestClaimAtom, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DigestSituationItemDraft:
        if not isinstance(raw, Mapping):
            raise ValueError("situation item must be a mapping")
        group_id = str(raw.get("group_id", "")).strip()
        label = str(raw.get("label", "")).strip()
        body = str(raw.get("body", "")).strip()
        emoji = str(raw.get("emoji", "")).strip()
        raw_supports = raw.get("cited_support_ids", [])
        if isinstance(raw_supports, (str, int)):
            raw_supports = [raw_supports]
        if not isinstance(raw_supports, list):
            raise ValueError("cited_support_ids must be a list")
        support_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_supports
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        if not group_id or not label or not body or not support_ids:
            raise ValueError("situation item requires group_id, label, body and cited_support_ids")
        raw_claims = raw.get("claims", [])
        if raw_claims is None:
            raw_claims = []
        if not isinstance(raw_claims, list):
            raise ValueError("claims must be a list")
        claims_list = [DigestClaimAtom.from_dict(c) for c in raw_claims]
        return cls(
            group_id=group_id,
            label=label,
            body=body,
            cited_support_ids=support_ids,
            emoji=emoji,
            claims=tuple(claims_list),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "label": self.label,
            "body": self.body,
            "cited_support_ids": list(self.cited_support_ids),
            "emoji": self.emoji,
            "claims": [c.to_dict() for c in self.claims],
        }


@dataclass(frozen=True)
class DigestClaimAtom:
    """A single supported claim atom within a digest editorial item."""

    text: str
    covered_story_ids: tuple[str, ...] = ()
    cited_support_ids: tuple[str, ...] = ()
    covered_fact_ids: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DigestClaimAtom:
        if not isinstance(raw, Mapping):
            raise ValueError("claim atom must be a mapping")
        text = str(raw.get("text", "")).strip()
        raw_stories = raw.get("covered_story_ids", [])
        if isinstance(raw_stories, (str, int)):
            raw_stories = [raw_stories]
        if not isinstance(raw_stories, list):
            raise ValueError("covered_story_ids must be a list")
        story_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_stories
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        raw_supports = raw.get("cited_support_ids", [])
        if isinstance(raw_supports, (str, int)):
            raw_supports = [raw_supports]
        if not isinstance(raw_supports, list):
            raise ValueError("cited_support_ids must be a list")
        support_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_supports
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        raw_facts = raw.get("covered_fact_ids", [])
        if isinstance(raw_facts, (str, int)):
            raw_facts = [raw_facts]
        if not isinstance(raw_facts, list):
            raise ValueError("covered_fact_ids must be a list")
        fact_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_facts
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        return cls(
            text=text,
            covered_story_ids=story_ids,
            cited_support_ids=support_ids,
            covered_fact_ids=fact_ids,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "text": self.text,
            "covered_story_ids": list(self.covered_story_ids),
            "cited_support_ids": list(self.cited_support_ids),
        }
        if self.covered_fact_ids:
            d["covered_fact_ids"] = list(self.covered_fact_ids)
        return d


@dataclass(frozen=True)
class DigestEditorialItemDraft:
    """A single scan-first editorial item within a narrative digest block."""

    headline: str = ""
    body: str = ""
    covered_story_ids: tuple[str, ...] = ()
    cited_support_ids: tuple[str, ...] = ()
    claims: tuple[DigestClaimAtom, ...] = ()
    emoji: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DigestEditorialItemDraft:
        if not isinstance(raw, Mapping):
            raise ValueError("digest item must be a mapping")
        headline = str(raw.get("headline", "")).strip()
        body = str(raw.get("body", "")).strip()
        emoji = str(raw.get("emoji", "")).strip()
        raw_stories = raw.get("covered_story_ids", [])
        if isinstance(raw_stories, (str, int)):
            raw_stories = [raw_stories]
        if not isinstance(raw_stories, list):
            raise ValueError("covered_story_ids must be a list")
        story_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_stories
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        if not story_ids:
            # Fall back to single story_id if provided
            fallback_sid = str(raw.get("story_id", "")).strip()
            if fallback_sid:
                story_ids = (fallback_sid,)
        raw_supports = raw.get("cited_support_ids", [])
        if isinstance(raw_supports, (str, int)):
            raw_supports = [raw_supports]
        if not isinstance(raw_supports, list):
            raise ValueError("cited_support_ids must be a list")
        support_ids = tuple(
            dict.fromkeys(
                str(x).strip()
                for x in raw_supports
                if x and isinstance(x, (str, int)) and str(x).strip()
            )
        )
        raw_claims = raw.get("claims", [])
        claims: list[DigestClaimAtom] = []
        if isinstance(raw_claims, list):
            for rc in raw_claims:
                if isinstance(rc, Mapping):
                    claims.append(DigestClaimAtom.from_dict(rc))

        # Union claim-level support IDs into item support_ids
        for c in claims:
            for s in c.cited_support_ids:
                if s and s not in support_ids:
                    support_ids = (*support_ids, s)

        # Sanitize headline to replace causal connectors with neutral phrasing
        clean_headline = (
            re.sub(r"\bиз-за\b", "при", headline, flags=re.IGNORECASE) if headline else ""
        )
        # Sanitize body: remove conversational assumption markers
        clean_body = body
        if clean_body:
            clean_body = re.sub(r"[«\"]по свету ноль[»\"]", "по свету ноль", clean_body)
            clean_body = re.sub(r"\s+вместо\s+220(?:\s*[вВвольт]+)?", "", clean_body)

        clean_claims: list[DigestClaimAtom] = []
        for c in claims:
            c_text = re.sub(r"[«\"]по свету ноль[»\"]", "по свету ноль", c.text)
            c_text = re.sub(r"\s+вместо\s+220(?:\s*[вВвольт]+)?", "", c_text)
            clean_claims.append(replace(c, text=c_text))

        if not clean_body or not story_ids or not support_ids:
            raise ValueError("digest editorial item requires body, stories and supports")
        return cls(
            headline=clean_headline,
            body=clean_body,
            covered_story_ids=story_ids,
            cited_support_ids=support_ids,
            claims=tuple(clean_claims),
            emoji=emoji,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "body": self.body,
            "covered_story_ids": list(self.covered_story_ids),
            "cited_support_ids": list(self.cited_support_ids),
            "claims": [c.to_dict() for c in self.claims],
            "emoji": self.emoji,
        }


@dataclass(frozen=True)
class DigestNarrativeBlockDraft:
    """A single rendered block in a narrative digest draft."""

    block_id: str
    items: tuple[DigestEditorialItemDraft, ...]


@dataclass(frozen=True)
class DigestNarrativeDraft:
    """Complete output draft from the single-call narrative digest writer."""

    blocks: tuple[DigestNarrativeBlockDraft, ...]
    situation_items: tuple[Any, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> DigestNarrativeDraft:
        """Parse structured narrative digest draft with strict structural validation."""
        if not isinstance(data, Mapping):
            raise ValueError("root must be a mapping")

        # Legacy situation_items are silently accepted but not stored — backward compat.

        raw_blocks = data.get("blocks")
        if raw_blocks is None:
            raise ValueError("missing 'blocks' list")
        if not isinstance(raw_blocks, list):
            raise ValueError("'blocks' must be a list")

        seen_block_ids: set[str] = set()
        block_drafts: list[DigestNarrativeBlockDraft] = []

        for b in raw_blocks:
            if not isinstance(b, Mapping):
                raise ValueError("block item must be a mapping")

            block_id = str(b.get("block_id") or "").strip()
            if not block_id:
                raise ValueError("missing or empty 'block_id'")
            if block_id in seen_block_ids:
                raise ValueError(f"duplicate block_id: {block_id}")
            seen_block_ids.add(block_id)

            raw_items = b.get("items")
            if raw_items is None or not isinstance(raw_items, list) or len(raw_items) == 0:
                raise ValueError(f"block {block_id} must contain at least one item")

            item_drafts: list[DigestEditorialItemDraft] = []
            for item_raw in raw_items:
                item_drafts.append(DigestEditorialItemDraft.from_dict(item_raw))

            block_drafts.append(
                DigestNarrativeBlockDraft(
                    block_id=block_id,
                    items=tuple(item_drafts),
                )
            )

        return cls(blocks=tuple(block_drafts), situation_items=())


def plan_digest_narrative_blocks(
    *,
    cards: Sequence[StoryCard],
    evidence: Mapping[str, PublicationEvidence],
    rubrics: Sequence[Any],
    max_cards_per_block: int = 6,
    presentation_plan: Any = None,
) -> DigestNarrativePlan:
    """Build immutable narrative blocks from classified story cards strictly preserving order."""
    if not cards:
        return DigestNarrativePlan(blocks=())

    def _get_r_info(r: Any) -> tuple[str, str, bool]:
        if isinstance(r, Mapping):
            return (
                str(r.get("id", "")),
                str(r.get("title") or r.get("name") or ""),
                bool(r.get("fallback", False)),
            )
        return (
            str(getattr(r, "id", "")),
            str(getattr(r, "name", "")),
            bool(getattr(r, "fallback", False)),
        )

    presentations_by_id = {}
    if presentation_plan is not None and getattr(presentation_plan, "story_presentations", None):
        presentations_by_id = {p.story_id: p for p in presentation_plan.story_presentations}
    elif presentation_plan is not None and getattr(presentation_plan, "story_hints", None):
        presentations_by_id = {h.story_id: h for h in presentation_plan.story_hints}

    dashboard_supports_by_story_map: dict[str, set[str]] = {}

    if presentation_plan is not None and getattr(presentation_plan, "city_situation", None):
        groups = getattr(presentation_plan.city_situation, "groups", ()) or ()
        for g in groups:
            for sid in getattr(g, "covered_story_ids", ()):
                dashboard_supports_by_story_map.setdefault(sid, set()).update(
                    getattr(g, "cited_support_ids", ())
                )

    rubric_infos = [_get_r_info(r) for r in rubrics]
    rubric_ids = [info[0] for info in rubric_infos if info[0]]
    fallback_info = next(
        (info for info in rubric_infos if info[2]),
        rubric_infos[0] if rubric_infos else ("other", "Другое", True),
    )
    fallback_id = fallback_info[0] if fallback_info[0] else "other"

    # Group cards by rubric, preserving rubric sequence
    cards_by_rubric: dict[str, list[StoryCard]] = {rid: [] for rid in rubric_ids}
    for card in cards:
        rid = card.rubric_id if card.rubric_id in cards_by_rubric else fallback_id
        if rid not in cards_by_rubric:
            cards_by_rubric[rid] = []
        cards_by_rubric[rid].append(card)

    blocks: list[DigestNarrativeBlock] = []

    assigned_fact_ids: set[str] = set()
    for rid, rname, _ in rubric_infos:
        if not rid:
            continue
        rubric_cards = cards_by_rubric.get(rid, [])
        if not rubric_cards:
            continue

        # Partition cards using deterministic presentation compression units
        from src.publication.digest_presentation import build_digest_presentation_units

        units = build_digest_presentation_units(
            rubric_cards,
            presentation_plan=presentation_plan,
            max_synthesis_size=24,
            max_normal_size=8,
            max_brief_size=6,
        )
        card_by_id = {c.id: c for c in rubric_cards}

        # Pack units into blocks
        current_block_units: list[Any] = []
        current_card_count = 0
        block_units_list: list[list[Any]] = []

        for unit in units:
            unit_len = len(unit.story_ids)
            bound = max(max_cards_per_block, unit_len)
            if current_block_units and (current_card_count + unit_len > bound):
                block_units_list.append(current_block_units)
                current_block_units = [unit]
                current_card_count = unit_len
            else:
                current_block_units.append(unit)
                current_card_count += unit_len

        if current_block_units:
            block_units_list.append(current_block_units)

        for chunk_idx, block_units in enumerate(block_units_list):
            chunk = [card_by_id[sid] for u in block_units for sid in u.story_ids]
            block_id = f"block:{rid}:{chunk_idx}"
            story_ids = tuple(c.id for c in chunk)

            # If presentation_plan provides explicit merge_group_ids, build
            # required_story_groups from them (preserving first-seen order).
            # Otherwise fall back to unit-level grouping.
            if presentations_by_id:
                mg_seen: dict[str, list[str]] = {}
                for c in chunk:
                    mgid = getattr(presentations_by_id.get(c.id), "merge_group_id", None) or c.id
                    mg_seen.setdefault(mgid, []).append(c.id)
                req_story_groups = tuple(tuple(v) for v in mg_seen.values())
            else:
                req_story_groups = tuple(tuple(u.story_ids) for u in block_units)

            # Collect canonical notes from cards and track support ownership per story
            notes: list[str] = []
            block_support_ids: list[str] = []
            story_support_ids_map: list[tuple[str, tuple[str, ...]]] = []

            for c in chunk:
                story_sups: list[str] = []
                if c.summary:
                    notes.append(f"{c.topic}: {c.summary}")
                    if f"{c.id}:summary" not in story_sups:
                        story_sups.append(f"{c.id}:summary")
                elif c.topic:
                    notes.append(c.topic)
                for hf in c.hard_facts:
                    if hf.text and hf.text not in notes:
                        notes.append(hf.text)
                    for r in hf.source_refs:
                        if r not in story_sups:
                            story_sups.append(r)
                for co in c.community_observations:
                    if co.text and co.text not in notes:
                        notes.append(co.text)
                    for r in co.source_refs:
                        if r not in story_sups:
                            story_sups.append(r)
                for r in getattr(c, "representative_source_refs", ()):
                    if r not in story_sups:
                        story_sups.append(r)
                if c.id not in story_sups:
                    story_sups.append(c.id)

                if c.id in presentations_by_id:
                    for supp_id in presentations_by_id[c.id].detail_support_ids:
                        if supp_id not in story_sups:
                            story_sups.append(supp_id)

                # Extract numeric story ID if story:123
                num_sid: int | None = None
                if c.id.startswith("story:"):
                    raw_sid = c.id.split(":", 1)[1]
                    if raw_sid.isdigit():
                        num_sid = int(raw_sid)

                for eid, evi in evidence.items():
                    evi_sid = getattr(evi, "story_id", None)
                    if (
                        evi_sid == c.id
                        or str(evi_sid) == str(c.id)
                        or (num_sid is not None and evi_sid == num_sid)
                        or eid.startswith(f"{c.id}:")
                    ):
                        if (
                            getattr(evi, "publication_use", "PUBLISH") == "PUBLISH"
                            and eid not in story_sups
                        ):
                            story_sups.append(eid)

                for sup in story_sups:
                    if sup not in block_support_ids:
                        block_support_ids.append(sup)
                story_support_ids_map.append((c.id, tuple(story_sups)))

            detail_supports = tuple(
                (c.id, presentations_by_id[c.id].detail_support_ids)
                for c in chunk
                if c.id in presentations_by_id and presentations_by_id[c.id].detail_support_ids
            )
            merge_groups = tuple(
                (c.id, presentations_by_id[c.id].merge_group_id)
                for c in chunk
                if c.id in presentations_by_id
            )
            detail_roles = tuple(
                (c.id, getattr(presentations_by_id[c.id], "detail_role", "NORMAL"))
                for c in chunk
                if c.id in presentations_by_id
            )
            pres_modes = tuple(
                (c.id, getattr(presentations_by_id[c.id], "mode", "DETAIL_ONLY"))
                for c in chunk
                if c.id in presentations_by_id
            )
            dash_supports = tuple(
                (c.id, tuple(dashboard_supports_by_story_map.get(c.id, ())))
                for c in chunk
                if c.id in dashboard_supports_by_story_map
            )

            # Assign required facts matching this block
            block_req_facts: list[RequiredDigestFact] = []
            if presentation_plan is not None and getattr(presentation_plan, "required_facts", None):
                story_id_set = set(story_ids)
                for rf in presentation_plan.required_facts:
                    if rf.fact_id in assigned_fact_ids:
                        continue
                    if bool(set(rf.story_ids) & story_id_set):
                        block_req_facts.append(rf)
                        assigned_fact_ids.add(rf.fact_id)

            blocks.append(
                DigestNarrativeBlock(
                    block_id=block_id,
                    rubric_id=rid,
                    rubric_title=rname,
                    story_ids=story_ids,
                    support_ids=tuple(block_support_ids),
                    canonical_notes=tuple(notes),
                    required_facts=tuple(block_req_facts),
                    detail_support_ids_by_story=detail_supports,
                    merge_group_by_story=merge_groups,
                    detail_roles_by_story=detail_roles,
                    presentation_modes_by_story=pres_modes,
                    dashboard_support_ids_by_story=dash_supports,
                    required_story_groups=req_story_groups,
                    support_ids_by_story=tuple(story_support_ids_map),
                )
            )

    expected_fact_ids = (
        {rf.fact_id for rf in getattr(presentation_plan, "required_facts", ())}
        if presentation_plan is not None
        else set()
    )
    unassigned = expected_fact_ids - assigned_fact_ids
    if unassigned and presentation_plan is not None and blocks:
        for rf in presentation_plan.required_facts:
            if rf.fact_id in unassigned:
                target_block = next((b for b in blocks if b.rubric_id == rf.rubric_id), None)
                if target_block is None:
                    target_block = next(
                        (b for b in blocks if bool(set(rf.story_ids) & set(b.story_ids))), None
                    )
                if target_block is None:
                    target_block = blocks[0]
                idx = blocks.index(target_block)
                blocks[idx] = replace(
                    target_block,
                    required_facts=(*target_block.required_facts, rf),
                    support_ids=tuple(dict.fromkeys((*target_block.support_ids, *rf.support_ids))),
                )
                assigned_fact_ids.add(rf.fact_id)

    if assigned_fact_ids != expected_fact_ids:
        raise DigestCoverageInvariantError(
            f"UNASSIGNED_REQUIRED_FACTS: {expected_fact_ids - assigned_fact_ids}"
        )

    return DigestNarrativePlan(blocks=tuple(blocks))


DIGEST_ITEM_HEADLINE_MAX_CHARS = 140
DIGEST_ITEM_BODY_MAX_CHARS = 1200
DIGEST_SITUATION_BODY_MAX_CHARS = 360

_RECOMMENDATION_SENTENCE_PATTERN = re.compile(
    r"(?:^|\s+)(?:Стоит|Следует|Рекомендуется|Необходимо|Лучше)\s+(?:заранее\s+)?(?:позаботиться|запастись|сделать\s+запас\w*|подготовить|подзарядить|иметь\s+в\s+виду)[^.!?\n]*[.!?]",
    re.IGNORECASE,
)


def find_unsupported_digest_recommendations(
    text: str,
    cited_supports: Sequence[str],
) -> list[str]:
    """Find reader advice / calls-to-action that are not grounded in cited supports."""
    if not text or not cited_supports:
        return []
    combined_support = " ".join(cited_supports).lower()
    violations: list[str] = []
    for match in _RECOMMENDATION_SENTENCE_PATTERN.finditer(text):
        matched_text = match.group(0).strip()
        matched_tokens = [w for w in re.split(r"\W+", matched_text.lower()) if len(w) >= 5]
        supported_count = sum(1 for tok in matched_tokens if tok in combined_support)
        if supported_count < 2:
            violations.append(matched_text)
    return violations


def strip_unsupported_recommendations(text: str, source_content: str) -> str:
    """Strip fabricated reader advice/calls-to-action unless explicitly supported by source content."""
    source_lower = source_content.lower()

    def _replace_if_unsupported(match: re.Match[str]) -> str:
        matched_text = match.group(0).strip()
        matched_tokens = [w for w in re.split(r"\W+", matched_text.lower()) if len(w) >= 5]
        supported_count = sum(1 for tok in matched_tokens if tok in source_lower)
        if supported_count >= 2:
            return match.group(0)
        return ""

    cleaned = _RECOMMENDATION_SENTENCE_PATTERN.sub(_replace_if_unsupported, text)
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def validate_digest_narrative(
    draft: DigestNarrativeDraft,
    plan: DigestNarrativePlan,
    support_index: Mapping[str, str] | None = None,
    *,
    support_text_by_id: Mapping[str, str] | None = None,
    situation_plan: Any = None,
    allowed_context_terms: Sequence[str] = (),
    all_known_draft_supports: Sequence[str] = (),
) -> DigestNarrativeValidationResult:
    """Validate structured narrative digest draft strictly against deterministic plan and evidence."""
    from src.publication.digest_relation_support import find_unsupported_digest_relations

    ctx_terms: Sequence[str] = tuple(allowed_context_terms) if allowed_context_terms else ()
    known_supports: Sequence[str] = (
        tuple(all_known_draft_supports) if all_known_draft_supports else ()
    )
    violations: list[str] = []
    unsupported_claims: list[Any] = []
    support_map = support_index if support_index is not None else (support_text_by_id or {})

    plan_blocks_by_id = {b.block_id: b for b in plan.blocks}
    draft_block_ids = [b.block_id for b in draft.blocks]
    plan_block_ids = [b.block_id for b in plan.blocks]

    if len(draft.blocks) != len(plan.blocks):
        violations.append(
            f"BLOCK_SET_MISMATCH: expected {len(plan.blocks)} blocks, got {len(draft.blocks)}"
        )

    if draft_block_ids != plan_block_ids:
        violations.append(f"BLOCK_SET_MISMATCH: expected {plan_block_ids}, got {draft_block_ids}")

    for out_block in draft.blocks:
        plan_block = plan_blocks_by_id.get(out_block.block_id)
        if plan_block is None:
            violations.append(f"UNKNOWN_BLOCK_ID: {out_block.block_id}")
            continue

        allowed_supports = set(plan_block.support_ids)
        expected_story_ids = set(plan_block.story_ids)
        merge_group_map = dict(plan_block.merge_group_by_story)

        flat_story_ids = [sid for item in out_block.items for sid in item.covered_story_ids]
        if len(flat_story_ids) != len(set(flat_story_ids)):
            violations.append(f"DUPLICATE_STORY_COVERAGE: {out_block.block_id}")

        for sid in flat_story_ids:
            if sid not in expected_story_ids:
                violations.append(f"UNKNOWN_STORY_ID: {sid} in block {out_block.block_id}")

        if set(flat_story_ids) != expected_story_ids:
            violations.append(f"STORY_PARTITION_MISMATCH: {out_block.block_id}")

        allowed_by_story = dict(plan_block.support_ids_by_story)

        for item in out_block.items:
            for sid in item.covered_story_ids:
                story_allowed = set(allowed_by_story.get(sid, ()))
                if story_allowed and not (set(item.cited_support_ids) & story_allowed):
                    violations.append(
                        f"STORY_SUPPORT_MISSING: story {sid} in block {out_block.block_id}"
                    )

            # Require structured claims only when the block carries required_facts to cover
            if not item.claims and plan_block.required_facts:
                violations.append(f"ITEM_CLAIMS_MISSING: item in block {out_block.block_id}")

            if item.claims:
                claimed_story_ids = {sid for c in item.claims for sid in c.covered_story_ids}
                for sid in item.covered_story_ids:
                    if sid not in claimed_story_ids:
                        violations.append(f"STORY_CLAIM_COVERAGE_MISSING:{sid}")

                for claim in item.claims:
                    for sid in claim.covered_story_ids:
                        if sid not in item.covered_story_ids:
                            violations.append(
                                f"UNKNOWN_STORY_ID: {sid} in claim of block {out_block.block_id}"
                            )
                        story_allowed = set(allowed_by_story.get(sid, ()))
                        if story_allowed and not (set(claim.cited_support_ids) & story_allowed):
                            violations.append(
                                f"STORY_SUPPORT_MISSING: story {sid} in claim of block {out_block.block_id}"
                            )
                    if not claim.cited_support_ids:
                        violations.append(
                            f"CLAIM_WITHOUT_SUPPORT: claim '{claim.text[:30]}' in block {out_block.block_id} cites no supports"
                        )
                    for sup_id in claim.cited_support_ids:
                        if sup_id not in item.cited_support_ids:
                            violations.append(
                                f"CLAIM_SUPPORT_OUTSIDE_ITEM: {sup_id} in block {out_block.block_id}"
                            )
                        if sup_id not in allowed_supports and allowed_supports:
                            violations.append(
                                f"SUPPORT_OUTSIDE_BLOCK: {sup_id} not allowed in block {out_block.block_id}"
                            )
                        if sup_id not in support_map:
                            violations.append(
                                f"UNKNOWN_SUPPORT_ID: {sup_id} not found in support text index"
                            )

                    c_claim_supports = [
                        support_map[s] for s in claim.cited_support_ids if s in support_map
                    ]
                    for unc in find_unsupported_claims(
                        claim.text,
                        c_claim_supports,
                        allowed_context_terms=ctx_terms,
                        all_known_draft_supports=known_supports,
                    ):
                        unsupported_claims.append(unc)
                        violations.append(
                            f"UNSUPPORTED_CONCRETE_CLAIM: [{unc.kind}] '{unc.raw}' in claim of block {out_block.block_id}"
                        )
                    for rel in find_unsupported_digest_relations(claim.text, c_claim_supports):
                        violations.append(
                            f"UNSUPPORTED_DIGEST_RELATION: '{rel.raw}' in claim of block {out_block.block_id}"
                        )
                    for rec in find_unsupported_digest_recommendations(
                        claim.text, c_claim_supports
                    ):
                        violations.append(
                            f"UNSUPPORTED_DIGEST_RECOMMENDATION: '{rec}' in claim of block {out_block.block_id}"
                        )

                    # Validate material fact references within the claim
                    block_req_facts_by_id = {rf.fact_id: rf for rf in plan_block.required_facts}
                    for fid in claim.covered_fact_ids:
                        if fid not in block_req_facts_by_id:
                            violations.append(f"UNKNOWN_DIGEST_FACT_ID:{fid}")
                        else:
                            rf = block_req_facts_by_id[fid]
                            rf_allowed_sups = set(rf.support_ids)
                            story_sups: set[str] = set()
                            for sid in rf.story_ids:
                                story_sups.update(allowed_by_story.get(sid, ()))
                            allowed_fact_sups = rf_allowed_sups | story_sups
                            if allowed_fact_sups and not (
                                set(claim.cited_support_ids) & allowed_fact_sups
                            ):
                                violations.append(f"DIGEST_FACT_SUPPORT_MISSING:{fid}")

            if len(item.covered_story_ids) > 1 and merge_group_map:
                m_groups = {merge_group_map.get(sid, sid) for sid in item.covered_story_ids}
                if len(m_groups) > 1:
                    violations.append(f"UNRELATED_STORY_GROUPING: {out_block.block_id}")

            # DRILL_DOWN enforcement: items covering a DRILL_DOWN story must cite
            # at least one of that story's designated detail support IDs.
            drill_roles = dict(plan_block.detail_roles_by_story)
            detail_sups_map = dict(plan_block.detail_support_ids_by_story)
            for sid in item.covered_story_ids:
                if drill_roles.get(sid) == "DRILL_DOWN":
                    drill_sups = set(detail_sups_map.get(sid, ()))
                    if drill_sups and not (set(item.cited_support_ids) & drill_sups):
                        violations.append(
                            f"DRILL_DOWN_MISSING_DISTINCT_SUPPORT: story {sid} in block {out_block.block_id}"
                        )

            if item.headline:
                if len(item.headline) > DIGEST_ITEM_HEADLINE_MAX_CHARS:
                    violations.append(
                        f"HEADLINE_TOO_LONG: headline exceeds {DIGEST_ITEM_HEADLINE_MAX_CHARS} chars in block {out_block.block_id}"
                    )
                if _INTERNAL_LEAKAGE_RE.search(item.headline):
                    violations.append(
                        f"INTERNAL_ID_LEAK: found internal identifier in block {out_block.block_id}"
                    )

            if len(item.body) > DIGEST_ITEM_BODY_MAX_CHARS:
                violations.append(
                    f"BODY_TOO_LONG: body exceeds {DIGEST_ITEM_BODY_MAX_CHARS} chars in block {out_block.block_id}"
                )
            if _INTERNAL_LEAKAGE_RE.search(item.body):
                violations.append(
                    f"INTERNAL_ID_LEAK: found internal identifier in block {out_block.block_id}"
                )

            if not item.cited_support_ids:
                violations.append(
                    f"MISSING_SUPPORT_CITATION: item in block {out_block.block_id} cites no supports"
                )

            for sup_id in item.cited_support_ids:
                if sup_id not in allowed_supports and allowed_supports:
                    violations.append(
                        f"SUPPORT_OUTSIDE_BLOCK: {sup_id} not allowed in block {out_block.block_id}"
                    )
                if sup_id not in support_map:
                    violations.append(
                        f"UNKNOWN_SUPPORT_ID: {sup_id} not found in support text index"
                    )

            # Validate concrete claims against cited support texts
            c_supports = [support_map[s] for s in item.cited_support_ids if s in support_map]
            if item.headline:
                for unc in find_unsupported_claims(
                    item.headline,
                    c_supports,
                    allowed_context_terms=ctx_terms,
                    all_known_draft_supports=known_supports,
                ):
                    unsupported_claims.append(unc)
                    violations.append(
                        f"UNSUPPORTED_CONCRETE_CLAIM: [{unc.kind}] '{unc.raw}' in headline of block {out_block.block_id}"
                    )
                for rel in find_unsupported_digest_relations(item.headline, c_supports):
                    violations.append(
                        f"UNSUPPORTED_DIGEST_RELATION: '{rel.raw}' in headline of block {out_block.block_id}"
                    )
                for rec in find_unsupported_digest_recommendations(item.headline, c_supports):
                    violations.append(
                        f"UNSUPPORTED_DIGEST_RECOMMENDATION: '{rec}' in headline of block {out_block.block_id}"
                    )
            for unc in find_unsupported_claims(
                item.body,
                c_supports,
                allowed_context_terms=ctx_terms,
                all_known_draft_supports=known_supports,
            ):
                unsupported_claims.append(unc)
                violations.append(
                    f"UNSUPPORTED_CONCRETE_CLAIM: [{unc.kind}] '{unc.raw}' in body of block {out_block.block_id}"
                )
            for rel in find_unsupported_digest_relations(item.body, c_supports):
                violations.append(
                    f"UNSUPPORTED_DIGEST_RELATION: '{rel.raw}' in body of block {out_block.block_id}"
                )
            for rec in find_unsupported_digest_recommendations(item.body, c_supports):
                violations.append(
                    f"UNSUPPORTED_DIGEST_RECOMMENDATION: '{rec}' in body of block {out_block.block_id}"
                )

        # Block-level strict required material facts coverage check
        for rf in plan_block.required_facts:
            rf_covered = False
            rf_allowed_supports = set(rf.support_ids)
            story_sups = set()
            for sid in rf.story_ids:
                story_sups.update(allowed_by_story.get(sid, ()))
            allowed_fact_sups = rf_allowed_supports | story_sups

            for item in out_block.items:
                for c in item.claims:
                    claim_sups = set(c.cited_support_ids)
                    if rf_allowed_supports and (claim_sups & rf_allowed_supports):
                        if not c.covered_fact_ids or rf.fact_id in c.covered_fact_ids:
                            rf_covered = True
                            break
                    elif c.covered_fact_ids and rf.fact_id in c.covered_fact_ids:
                        if not allowed_fact_sups or (claim_sups & allowed_fact_sups):
                            rf_covered = True
                            break
                if rf_covered:
                    break
            if not rf_covered:
                violations.append(f"DIGEST_FACT_COVERAGE_MISSING:{rf.fact_id}")

    is_valid = len(violations) == 0 and len(unsupported_claims) == 0
    return DigestNarrativeValidationResult(
        is_valid=is_valid,
        violations=tuple(violations),
        unsupported_claims=tuple(unsupported_claims),
    )


def _render_deterministic_digest_evidence(evi: PublicationEvidence) -> str:
    text = (evi.text or evi.source_text).strip()
    if evi.kind in {"community_report", "community_observation", "quote_assertion"}:
        if not text.casefold().startswith(("по сообщениям", "жители сообщают", "по словам")):
            if text:
                text = f"По сообщениям жителей, {text[:1].lower() + text[1:]}"
    return text.rstrip(". ") + "."


def build_deterministic_digest_draft(
    *,
    cards: Sequence[StoryCard],
    evidence: Mapping[str, PublicationEvidence],
    rubrics: Sequence[Any],
    presentation_plan: Any,
    allowed_context_terms: Sequence[str] = (),
    all_known_draft_supports: Sequence[str] = (),
) -> DigestNarrativeDraft:
    """Build a deterministic, provenance-bearing DigestNarrativeDraft from the presentation plan."""
    from src.publication.article_claims import find_unsupported_claims
    from src.publication.digest_relation_support import find_unsupported_digest_relations

    ctx_terms: Sequence[str] = tuple(allowed_context_terms) if allowed_context_terms else ()
    known_supports: Sequence[str] = (
        tuple(all_known_draft_supports) if all_known_draft_supports else ()
    )
    detail_story_ids = set(getattr(presentation_plan, "detail_story_ids", ()))
    detail_cards = [c for c in cards if c.id in detail_story_ids]

    narrative_plan = plan_digest_narrative_blocks(
        cards=detail_cards,
        evidence=evidence,
        rubrics=rubrics,
        presentation_plan=presentation_plan,
    )

    presentations_by_id = {
        p.story_id: p for p in getattr(presentation_plan, "story_presentations", ())
    }
    cards_by_id = {c.id: c for c in detail_cards}

    dashboard_supports_by_story: dict[str, set[str]] = {}
    if getattr(presentation_plan, "city_situation", None):
        for g in getattr(presentation_plan.city_situation, "groups", ()):
            for sid in getattr(g, "covered_story_ids", ()):
                dashboard_supports_by_story.setdefault(sid, set()).update(
                    getattr(g, "cited_support_ids", ())
                )

    block_drafts: list[DigestNarrativeBlockDraft] = []
    for plan_block in narrative_plan.blocks:
        item_drafts: list[DigestEditorialItemDraft] = []
        groups_to_cover = (
            plan_block.required_story_groups
            if plan_block.required_story_groups
            else tuple((sid,) for sid in plan_block.story_ids)
        )
        for story_group in groups_to_cover:
            group_chosen_supports: list[str] = []
            group_rendered_sentences: list[str] = []
            group_support_texts: list[str] = []
            lead_topic: str = ""

            for sid in story_group:
                pres = presentations_by_id.get(sid)
                card = cards_by_id.get(sid)
                if not card:
                    continue
                if not lead_topic and card.topic:
                    lead_topic = card.topic.strip()

                dash_supp_ids = dashboard_supports_by_story.get(sid, set())
                eligible_supports: list[str] = []

                if pres and pres.detail_support_ids:
                    for supp_id in pres.detail_support_ids:
                        if supp_id not in dash_supp_ids and (supp_id in evidence or supp_id):
                            eligible_supports.append(supp_id)

                if not eligible_supports:
                    num_sid: int | None = None
                    if sid.startswith("story:"):
                        num_part = sid.split(":", 1)[1]
                        if num_part.isdigit():
                            num_sid = int(num_part)

                    for eid, evi in evidence.items():
                        evi_sid = getattr(evi, "story_id", None)
                        if (
                            (evi_sid is not None and num_sid is not None and evi_sid == num_sid)
                            or eid.startswith(f"{sid}:")
                            or getattr(evi, "story_id", None) == sid
                        ):
                            if (
                                getattr(evi, "publication_use", "PUBLISH") == "PUBLISH"
                                and getattr(evi, "kind", "") != "resident_question"
                                and eid not in dash_supp_ids
                            ):
                                eligible_supports.append(eid)

                if not eligible_supports:
                    if card.summary and f"{card.id}:summary" not in dash_supp_ids:
                        eligible_supports.append(f"{card.id}:summary")
                    for r in getattr(card, "representative_source_refs", ()):
                        if r not in dash_supp_ids and r not in eligible_supports:
                            eligible_supports.append(r)
                    for hf in getattr(card, "hard_facts", ()):
                        for r in getattr(hf, "source_refs", ()):
                            if r not in dash_supp_ids and r not in eligible_supports:
                                eligible_supports.append(r)
                    for co in getattr(card, "community_observations", ()):
                        for r in getattr(co, "source_refs", ()):
                            if r not in dash_supp_ids and r not in eligible_supports:
                                eligible_supports.append(r)
                    if not eligible_supports and card.id not in dash_supp_ids:
                        eligible_supports.append(card.id)

                if not eligible_supports:
                    raise ValueError(f"no deterministic detail support for {sid}")

                per_story_cap = 2 if len(story_group) == 1 else 1
                chosen_supports = eligible_supports[:per_story_cap]

                for s in chosen_supports:
                    text = ""
                    kind = "established_fact"
                    actual_sup_id = s
                    if s in evidence:
                        text = (evidence[s].text or evidence[s].source_text).strip()
                        kind = getattr(evidence[s], "kind", "established_fact")
                    elif (
                        s == f"{card.id}:summary"
                        or s == card.id
                        or s in getattr(card, "representative_source_refs", ())
                    ):
                        if card.summary:
                            text = card.summary.strip()
                        elif card.topic:
                            text = card.topic.strip()
                    else:
                        for hf in getattr(card, "hard_facts", ()):
                            if s in getattr(hf, "source_refs", ()) or s == getattr(hf, "text", ""):
                                text = hf.text.strip()
                                kind = "established_fact"
                                break
                        if not text:
                            for co in getattr(card, "community_observations", ()):
                                if s in getattr(co, "source_refs", ()) or s == getattr(
                                    co, "text", ""
                                ):
                                    text = co.text.strip()
                                    kind = "community_report"
                                    break
                        if not text:
                            if card.summary:
                                text = card.summary.strip()
                                actual_sup_id = f"{card.id}:summary"
                            elif card.topic:
                                text = card.topic.strip()
                                actual_sup_id = card.id

                    if text:
                        group_chosen_supports.append(actual_sup_id)
                        group_support_texts.append(text)
                        if kind in {"community_report", "community_observation", "quote_assertion"}:
                            if not text.casefold().startswith(
                                ("по сообщениям", "жители сообщают", "по словам")
                            ):
                                text = f"По сообщениям жителей, {text[:1].lower() + text[1:]}"
                        group_rendered_sentences.append(text.rstrip(". ") + ".")

            topic = lead_topic
            topic_claims = (
                find_unsupported_claims(
                    topic,
                    group_support_texts,
                    allowed_context_terms=ctx_terms,
                    all_known_draft_supports=known_supports,
                )
                if topic
                else []
            )
            topic_relations = (
                find_unsupported_digest_relations(topic, group_support_texts) if topic else []
            )
            if (
                topic
                and not topic_claims
                and not topic_relations
                and len(topic) <= DIGEST_ITEM_HEADLINE_MAX_CHARS
            ):
                headline = topic
            elif group_rendered_sentences:
                chosen_sent = None
                for sent in group_rendered_sentences:
                    cand = sent.rstrip(". ")
                    if len(cand) > DIGEST_ITEM_HEADLINE_MAX_CHARS:
                        truncated = cand[:DIGEST_ITEM_HEADLINE_MAX_CHARS]
                        for sep in [". ", "! ", "? ", "; ", ", ", " — ", " - "]:
                            if sep in truncated:
                                parts = truncated.rsplit(sep, 1)
                                if len(parts[0].strip()) >= 20:
                                    truncated = parts[0].strip()
                                    break
                        else:
                            if " " in truncated:
                                truncated = truncated.rsplit(" ", 1)[0].strip()
                        cand = truncated.rstrip(".:;, ")
                    c_claims = find_unsupported_claims(
                        cand,
                        group_support_texts,
                        allowed_context_terms=ctx_terms,
                        all_known_draft_supports=known_supports,
                    )
                    c_rels = find_unsupported_digest_relations(cand, group_support_texts)
                    if not c_claims and not c_rels:
                        chosen_sent = cand
                        break
                if not chosen_sent:
                    chosen_sent = (
                        group_rendered_sentences[0]
                        .rstrip(". ")[:DIGEST_ITEM_HEADLINE_MAX_CHARS]
                        .rstrip(".:;, ")
                    )
                headline = chosen_sent
            else:
                headline = (
                    topic[:DIGEST_ITEM_HEADLINE_MAX_CHARS] if topic else story_group[0]
                ).rstrip(".:;, ")

            body_text = " ".join(group_rendered_sentences)
            if len(body_text) > DIGEST_ITEM_BODY_MAX_CHARS:
                body_text = (
                    body_text[:DIGEST_ITEM_BODY_MAX_CHARS].rsplit(" ", 1)[0].rstrip(".:;, ") + "."
                )

            item_drafts.append(
                DigestEditorialItemDraft(
                    headline=headline,
                    body=body_text,
                    covered_story_ids=tuple(story_group),
                    cited_support_ids=tuple(group_chosen_supports),
                )
            )

        if item_drafts:
            block_drafts.append(
                DigestNarrativeBlockDraft(
                    block_id=plan_block.block_id,
                    items=tuple(item_drafts),
                )
            )

    return DigestNarrativeDraft(blocks=tuple(block_drafts), situation_items=())


def format_digest_date_ru(snapshot_at: dt.datetime) -> str:
    """Format date in Russian: e.g. 04 сентября 2026."""
    months_ru = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    return f"{snapshot_at.day:02d} {months_ru[snapshot_at.month - 1]} {snapshot_at.year}"


DIGEST_PROMPT_TEMPLATE = """Вы — старший редактор регионального издания, готовящий ежедневный вечерний Telegram-дайджест города {city} за {date}.

ВАША ГЛАВНАЯ ЦЕЛЬ:
Сформировать структурированный СПИСОК ПУНКТОВ (bulleted list) новостей по рубрикам на основе проверенных городских сообщений за последние 24 часа.
Дайджест — это НЕ связный рассказ, НЕ статья и НЕ сплошной текст! Это удобный, быстрый для сканирования перечень отдельных пунктов, где каждая отдельная новость или событие оформлена своим пунктом с эмодзи в начале.

СТРУКТУРА ДАЙДЖЕСТА:
Дайджест · {date}

[Тематические рубрики дня из доступного списка:
В фокусе внимания
Коммунальная обстановка
Безопасность и чрезвычайные ситуации
Связь и интернет
Транспорт и дороги
Медицина и здоровье
Социальная помощь
Полезные контакты
Другое]

ВЫВОДИТЕ ТОЛЬКО те рубрики, по которым сегодня есть реальные новости. Пустые рубрики НЕ выводить!
Рубрику «В фокусе внимания» выводить в самом начале ТОЛЬКО если есть главное резонансное событие дня с прямым влиянием на весь город (масштабная авария, ЧП, крупный инцидент).

ФОРМАТ ЗАГОЛОВКА РУБРИКИ:
- Чистый текст названия рубрики на отдельной строке.
- БЕЗ эмодзи в заголовке рубрики! (Эмодзи ставятся в начале каждого пункта, а не в названии рубрики).
- БЕЗ звездочек ** и символов #.
- БЕЗ разделителей (---, ***, ___). Рубрики и пункты разделяются ТОЛЬКО одной пустой строкой.

ФОРМАТ ПУНКТОВ ВНУТРИ РУБРИКИ:
Каждое отдельное событие, новость или происшествие оформляется КАК ОТДЕЛЬНЫЙ ПУНКТ СПИСКА (абзац), отделенный от соседних пустой строкой.
Каждый пункт начинается с подходящего тематического эмодзи:
- Формат с заголовком: [Эмодзи] [Краткий заголовок факта]: [Фактическое раскрытие сути в 1-3 предложениях с конкретикой (улицы, время, цифры, статус, комментарии служб, решения жителей)].
- Формат без заголовка (для кратких оперативных сообщений, фиксации звуков или предупреждений): [Эмодзи] [Фактическое раскрытие сути в 1-2 предложениях].

ПРИМЕР СТРУКТУРЫ (ВНИМАНИЕ: ЭТО ТОЛЬКО ШАБЛОН ОФОРМЛЕНИЯ! КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО КОПИРОВАТЬ ЭТОТ ТЕКСТ В ДАЙДЖЕСТ):
Дайджест · {date}

Коммунальная обстановка

⚡️ [Краткий заголовок темы электричества]: [Фактическое раскрытие сути в 1-3 предложениях с конкретикой: улицы, районы, графики, комментарии служб из предоставленных материалов дня].

💧 [Краткий заголовок темы водоснабжения]: [Фактическое раскрытие сути в 1-3 предложениях из материалов дня].

Безопасность и чрезвычайные ситуации

💥 [Краткий заголовок происшествия]: [Фактическое раскрытие сути происшествия из материалов дня].

🛡 [Краткое оперативное сообщение о звуках взрывов или работе ПВО без заголовка в 1-2 предложениях].

ПРАВИЛА И СТИЛЬ:
1. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ВЫДУМЫВАНИЕ И КОПИРОВАНИЕ ШАБЛОНА:
   - ЗАПРЕЩЕНО выдумывать новости, детали, провайдеров или копировать текст из шаблона структуры.
   - Пишите ИСКЛЮЧИТЕЛЬНО на основе фактов из блока «МАТЕРИАЛЫ ДНЯ ДЛЯ ДАЙДЖЕСТА».
   - Если по какой-то рубрике (например, «Связь и интернет», «Транспорт», «Медицина») в материалах дня нет фактов — такую рубрику выводить КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО.
2. ОБЪЕДИНЕНИЕ СООБЩЕНИЙ ПО ОДНОЙ ТЕМЕ (СИНТЕЗ):
   - Если в материалах дня есть несколько карточек по одной проблеме (например, несколько сообщений об отключении света на разных улицах или жалобы на запах газа в разных районах), ОБЪЕДИНЯЙТЕ их в ОДИН емкий пункт списка, перечисляя затронутые районы, улицы и важные подробности.
   - НЕ создавайте несколько одинаковых пунктов по одной и той же теме!
3. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА СПЛОШНОЙ ТЕКСТ / «РАССКАЗ»:
   - ЗАПРЕЩЕНО сливать разные события (например, свет, воду, запах газа, безопасность, больницы) в один общий абзац или связный рассказ.
   - Каждая отдельная тема/событие — это ОТДЕЛЬНЫЙ ПУНКТ списка со своим эмодзи.
4. Журналистский стиль: чистый, энергичный русский язык хроники. Точный и грамотный перевод сообщений на украинском языке (названия памятников и ориентиров переводить строго на русский язык, например «у памятника Самолёту», а не «у «Літака»»; «ліхтарі» переводить как «фонари», а не «лихтари»).
5. Сохраняйте микродетали: точные улицы, микрорайоны, графики подачи, номера маршрутов, цены, важные решения жителей.
6. Очистка от рекламы и спама: категорически исключайте коммерческие перевозки за границу ($450), разблокировку карт и счетов, прайс-листы клиник/процедур, рекламу общепита и бытовой чат-флуд («все живые», пустые реплики).
7. Разнообразная естественная атрибуция: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО начинать каждое предложение с «По сообщениям жителей...». Используйте естественные и разнообразные обороты («По словам горожан...», «В местных чатах отмечают...», «Как рассказали жители...») либо пишите сразу от сути события.
8. Ограничение длины (одно сообщение Telegram):
   - Итоговый текст дайджеста должен составлять от 2500 до 3700 знаков (жесткий лимит Telegram — 4096 символов).
   - Включайте 3–6 активных рубрик дня, в каждой — от 1 до 4 самых важных и конкретных пунктов.{toponym_rules}
9. СТРОГО НЕЙТРАЛЬНАЯ ТЕРМИНОЛОГИЯ И УВАЖИТЕЛЬНЫЙ ТОН:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать конфликтные, политизированные, оценочные или враждебные ярлыки («оккупанты», «оккупационная администрация», «оккупационные власти», «захватчики» и т.п.).
   - Всегда используйте строго нейтральные городские и институциональные формулировки: «городская администрация», «местные власти», «представители администрации», «муниципальные службы» либо пишите в нейтрально-деловом ключе («по официальным сообщениям», «согласно заявлению администрации города»).
10. СОДЕРЖАТЕЛЬНОСТЬ И КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ВЫДУМЫВАНИЕ СОВЕТОВ:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдумывать от себя или дописывать назидательные житейские советы читателям («стоит заранее позаботиться о запасах воды», «рекомендуется зарядить пауэрбанки», «следует воздержаться от поездок»). Передавайте ТОЛЬКО факты из материалов дня. Если в исходных сообщениях нет прямого совета или инструкции от служб/жителей — выдумывать советы запрещено.
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО публиковать бессодержательные рекомендации без объяснения причин (например, писать «советуют обновить приложение» без указания того, какая именно проблема, сбой или ошибка возникли в старой версии). Если конкретная техническая причина совета в материалах дня не указана — исключайте такой совет.
11. ЗАПРЕТ НА ВЫДУМЫВАНИЕ БУДУЩИХ ОТКЛЮЧЕНИЙ И ДОДУМЫВАНИЕ ДАТ:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО интерпретировать обрывочные реплики жителей в чатах (например, «отключение с 9», «без света с первого») как анонсы предстоящих отключений в будущем!
   - Анонсировать будущие отключения (графики, предупреждения) разрешено ТОЛЬКО при наличии официального сообщения коммунальных служб (РЭС, Горсвет, Горгаз, Водоканал) или администрации города.
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО додумывать месяц к одиночным цифрам (реплика «с 9» в контексте спора о блэкауте в августе не является отключением 9 сентября!).
12. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ВВОДНЫЕ РЕПЛИКИ И ПРЕАМБУЛЫ:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ любые вступительные фразы от первого лица или приветствия (например, «Вот ежедневный дайджест...», «Здравствуйте!», «Ниже представлен...»).
   - Вывод должен начинаться СТРОГО со строки заголовка («Дайджест · {date}») либо сразу с названия первой рубрики, без каких-либо вводных слов или мета-комментариев.

МАТЕРИАЛЫ ДНЯ ДЛЯ ДАЙДЖЕСТА:
{content}
"""

DIGEST_CONDENSE_PROMPT_TEMPLATE = """Вы — выпускающий редактор регионального Telegram-канала города {city}.
Перед вами черновик вечернего дайджеста за {date}, который превышает допустимый лимит одного сообщения Telegram ({current_len} знаков при лимите {max_chars} знаков).

ВАША ЗАДАЧА:
Отредактировать и уплотнить текст так, чтобы его итоговая длина составила строго от 2800 до {target_chars} знаков, сохранив абсолютно ВСЕ факты, темы и рубрики.

ПРАВИЛА РЕДАКТУРЫ И КОМПРЕССИИ:
1. Сохраняйте структуру СПИСКА ПУНКТОВ: дайджест должен оставаться списком отдельных пунктов с эмодзи по рубрикам. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО превращать пункты в сплошной связный рассказ или статью!
2. НЕ УДАЛЯЙТЕ события, рубрики или пункты. Все новости и темы должны остаться!
3. Уплотняйте синтаксис внутри пунктов: убирайте многословие, вводные конструкции («следует отметить, что», «как стало известно из сообщений»), пространные рассуждения и повторы.
4. Сохраняйте ВСЕ микродетали: названия улиц, номера домов, время, цены, имена, учреждения, номера статей КоАП, марки генераторов.
5. Объединяйте сложноподчиненные предложения в краткие, энергичные фразы.
6. Сохраните формат Telegram: чистые названия рубрик (без эмодзи в заголовке, без ** и ##), разделение ТОЛЬКО пустой строкой. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ разделители (---, ***) и лишняя разметка (**, ##).
7. Верните ТОЛЬКО готовый отредактированный текст без вступительных или заключительных реплик.
8. СТРОГО НЕЙТРАЛЬНАЯ ТЕРМИНОЛОГИЯ: используйте только нейтральные формулировки органов власти («городская администрация», «местные власти»), категорически исключая конфликтные или враждебные ярлыки («оккупанты» и т.п.).
9. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ДОБАВЛЕНИЕ СОВЕТОВ: запрещено добавлять от себя назидательные советы или житейские рекомендации читателям («стоит позаботиться...», «рекомендуется запастись...»).

ЧЕРНОВИК ДАЙДЖЕСТА ДЛЯ КОМПРЕССИИ:
{draft_text}
"""


def _clean_markdown_fence(text: str | None) -> str:
    if not text:
        return ""
    clean = text.strip()
    if clean.startswith("```markdown"):
        clean = clean[len("```markdown") :].strip()
    elif clean.startswith("```"):
        clean = clean[3:].strip()
    if clean.endswith("```"):
        clean = clean[:-3].strip()
    return clean


_TERMINOLOGY_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Prepositional/adverbial phrases with "по данным" / "сообщили в"
    (
        re.compile(
            r"\bпо\s+данным\s+оккупационн(?:ой|ых)\s+(?:администрации|властей)\b",
            re.IGNORECASE,
        ),
        "по данным городской администрации",
    ),
    (
        re.compile(
            r"\bсообщили\s+в\s+оккупационн(?:ой|ых)\s+(?:администрации|властях)\b",
            re.IGNORECASE,
        ),
        "сообщили в городской администрации",
    ),
    (
        re.compile(
            r"\bзаявили\s+в\s+оккупационн(?:ой|ых)\s+(?:администрации|властях)\b",
            re.IGNORECASE,
        ),
        "заявили в городской администрации",
    ),
    # Adjective + noun: администрация
    (
        re.compile(r"\bоккупационн(?:ая)\s+администраци(?:я)\b", re.IGNORECASE),
        "городская администрация",
    ),
    (
        re.compile(r"\bоккупационн(?:ой|ою)\s+администраци(?:ей|ею)\b", re.IGNORECASE),
        "городской администрацией",
    ),
    (
        re.compile(r"\bоккупационн(?:ой)\s+администраци(?:и)\b", re.IGNORECASE),
        "городской администрации",
    ),
    (
        re.compile(r"\bоккупационн(?:ую)\s+администраци(?:ю)\b", re.IGNORECASE),
        "городскую администрацию",
    ),
    # Adjective + noun: власти
    (re.compile(r"\bоккупационн(?:ые)\s+власт(?:и)\b", re.IGNORECASE), "местные власти"),
    (re.compile(r"\bоккупационн(?:ых)\s+власт(?:ей)\b", re.IGNORECASE), "местных властей"),
    (re.compile(r"\bоккупационн(?:ым)\s+власт(?:ям)\b", re.IGNORECASE), "местным властям"),
    (re.compile(r"\bоккупационн(?:ыми)\s+власт(?:ями)\b", re.IGNORECASE), "местными властями"),
    (re.compile(r"\bоккупационн(?:ых)\s+власт(?:ях)\b", re.IGNORECASE), "местных властях"),
    # Adjective + noun: структуры / службы
    (
        re.compile(r"\bоккупационн(?:ые)\s+(?:структуры|службы)\b", re.IGNORECASE),
        "городские службы",
    ),
    (re.compile(r"\bоккупационн(?:ых)\s+(?:структур|служб)\b", re.IGNORECASE), "городских служб"),
    (
        re.compile(r"\bоккупационн(?:ым)\s+(?:структурам|службам)\b", re.IGNORECASE),
        "городским службам",
    ),
    (
        re.compile(r"\bоккупационн(?:ыми)\s+(?:структурами|службами)\b", re.IGNORECASE),
        "городскими службами",
    ),
    # Adjective + noun: комендатура
    (
        re.compile(r"\bоккупационн(?:ая)\s+комендатур(?:а)\b", re.IGNORECASE),
        "городская комендатура",
    ),
    (
        re.compile(r"\bоккупационн(?:ой)\s+комендатур(?:е|ы|ой)\b", re.IGNORECASE),
        "городской комендатуре",
    ),
    # Adjective + noun: режим
    (
        re.compile(r"\bоккупационн(?:ый|ого|ому|ым|ом)\s+режим(?:а|у|ом|е)?\b", re.IGNORECASE),
        "городская администрация",
    ),
    # Noun standalone: оккупанты / захватчики
    (re.compile(r"\b(?:оккупант(?:ы)|захватчик(?:и))\b", re.IGNORECASE), "местные власти"),
    (re.compile(r"\b(?:оккупант(?:ов)|захватчик(?:ов))\b", re.IGNORECASE), "местных властей"),
    (re.compile(r"\b(?:оккупант(?:ам)|захватчик(?:ам))\b", re.IGNORECASE), "местным властям"),
    (re.compile(r"\b(?:оккупант(?:ами)|захватчик(?:ами))\b", re.IGNORECASE), "местными властями"),
    (re.compile(r"\b(?:оккупант(?:ах)|захватчик(?:ах))\b", re.IGNORECASE), "местных властях"),
    (
        re.compile(r"\b(?:оккупант(?:а)|захватчик(?:а))\b", re.IGNORECASE),
        "представителя администрации",
    ),
)


def sanitize_digest_terminology(text: str) -> str:
    """Normalize politically hostile/offensive labels to neutral municipal formulations."""
    if not text:
        return ""

    res = text
    for pattern, repl in _TERMINOLOGY_REPLACEMENTS:

        def _sub(match: re.Match[str], replacement: str = repl) -> str:
            orig = match.group(0)
            if orig and orig[0].isupper():
                return replacement[0].upper() + replacement[1:]
            return replacement[0].lower() + replacement[1:]

        res = pattern.sub(_sub, res)
    return res


def enforce_telegram_single_message_limit(text: str, max_chars: int = 3900) -> str:
    """Deterministic fallback: trims text along structural paragraph boundaries if still over limit."""
    if len(text) <= max_chars:
        return text
    lines = text.splitlines(keepends=True)
    acc: list[str] = []
    cur_len = 0
    for line in lines:
        if cur_len + len(line) <= max_chars:
            acc.append(line)
            cur_len += len(line)
        else:
            break
    return "".join(acc).strip()


def parse_journalistic_markdown_to_draft(
    markdown_text: str,
    cards: Sequence[StoryCard],
    evidence: Mapping[str, PublicationEvidence] | None = None,
    custom_rubrics: Sequence[Any] | None = None,
) -> DigestNarrativeDraft:
    """Parse raw journalistic Telegram Markdown text into a structured DigestNarrativeDraft."""
    lines = markdown_text.splitlines()
    blocks: list[DigestNarrativeBlockDraft] = []
    current_items: list[DigestEditorialItemDraft] = []
    current_rubric_id = "general"
    block_counter = 0

    card_tokens: dict[str, set[str]] = {}
    for c in cards:
        toks = set(re.findall(r"[\w-]+", (c.topic or "").lower()))
        for f in getattr(c, "hard_facts", ()):
            toks |= set(re.findall(r"[\w-]+", (f.text or "").lower()))
        for o in getattr(c, "community_observations", ()):
            toks |= set(re.findall(r"[\w-]+", (o.text or "").lower()))
        card_tokens[c.id] = {t for t in toks if len(t) >= 3}

    story_supports: dict[str, list[str]] = {}
    if evidence:
        for evid, ev in evidence.items():
            sid = (
                f"story:{ev.story_id}"
                if not str(ev.story_id).startswith("story:")
                else str(ev.story_id)
            )
            story_supports.setdefault(sid, []).append(evid)

    def _flush() -> None:
        nonlocal block_counter, current_items
        if current_items:
            blocks.append(
                DigestNarrativeBlockDraft(
                    block_id=f"block:{current_rubric_id}:{block_counter}",
                    items=tuple(current_items),
                )
            )
            block_counter += 1
            current_items = []

    _EMOJI_PATTERN = r"[\U00010000-\U0010ffff\u200d\u2300-\u27bf\ufe0f]"
    re_leading_marker = re.compile(rf"^(?:[•\-\*]\s*)?(?:(?:{_EMOJI_PATTERN})+\s*)?")
    re_bold_headline = re.compile(r"^\*\*([^*]+)\*\*[:.]?\s*(.*)$")

    dynamic_rubrics: list[tuple[str, str, set[str]]] = []
    dynamic_keywords: list[str] = []
    if custom_rubrics:
        for cr in custom_rubrics:
            r_name = getattr(cr, "name", None) or (
                cr.get("name") if isinstance(cr, dict) else str(cr)
            )
            r_id = (
                getattr(cr, "id", None)
                or (cr.get("id") if isinstance(cr, dict) else None)
                or getattr(cr, "key", None)
                or (cr.get("key") if isinstance(cr, dict) else None)
            )
            clean_name = re.sub(r"[^\w\s-]", "", str(r_name)).strip().lower()
            if not r_id:
                slug = re.sub(r"[^\w]+", "_", clean_name).strip("_")
                r_id = slug or "custom"
            words = {w for w in clean_name.split() if len(w) >= 3}
            if clean_name:
                dynamic_rubrics.append((str(r_id), clean_name, words))
                dynamic_keywords.extend(words)

    rubric_keywords = (
        "коммунальн",
        "инфраструктур",
        "жкх",
        "электроснабжен",
        "водоснабжен",
        "безопасн",
        "тревог",
        "чп",
        "обстрел",
        "пво",
        "сирен",
        "взрыв",
        "социальн",
        "выплат",
        "пенси",
        "пособи",
        "связь",
        "интернет",
        "транспорт",
        "дорог",
        "медицин",
        "здоров",
        "больниц",
        "аптек",
        "образов",
        "школ",
        "культур",
        "городск",
        "сред",
        "благоустрой",
        "быт",
        "другое",
        "в фокусе",
        "фокус",
        "полезные контакты",
        "контакт",
        *dynamic_keywords,
    )

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        clean_hdr = re.sub(r"[^\w\s-]", "", stripped).lower()
        if clean_hdr.startswith(("дайджест", "дайдджест")):
            continue

        is_bullet = stripped.startswith(("•", "-", "*•", "* -"))
        is_header = not is_bullet and (
            stripped.startswith(("##", "###"))
            or (
                any(k in clean_hdr for k in rubric_keywords)
                and len(stripped) <= 60
                and not (":" in stripped and len(stripped) > 35)
            )
        )

        if is_header:
            _flush()
            matched_custom = False
            for r_id, clean_name, words in dynamic_rubrics:
                if (
                    clean_name in clean_hdr
                    or clean_hdr in clean_name
                    or (words and words.issubset(set(clean_hdr.split())))
                ):
                    current_rubric_id = r_id
                    matched_custom = True
                    break
            if not matched_custom:
                if any(k in clean_hdr for k in ("в фокусе", "фокус")):
                    current_rubric_id = "focus"
                elif any(k in clean_hdr for k in ("жкх", "коммун", "электр", "вода", "энерг")):
                    current_rubric_id = "infrastructure"
                elif any(
                    k in clean_hdr
                    for k in ("безопасн", "тревог", "чп", "обстрел", "пво", "сирен", "взрыв")
                ):
                    current_rubric_id = "safety"
                elif any(k in clean_hdr for k in ("социальн", "выплат", "пенси", "пособи")):
                    current_rubric_id = "social"
                elif any(k in clean_hdr for k in ("транспорт", "дорог")):
                    current_rubric_id = "transport"
                elif any(k in clean_hdr for k in ("связь", "интернет")):
                    current_rubric_id = "communications"
                elif any(k in clean_hdr for k in ("медицин", "здоров", "больниц", "аптек")):
                    current_rubric_id = "health"
                elif any(k in clean_hdr for k in ("образов", "школ", "культур")):
                    current_rubric_id = "education"
                elif any(k in clean_hdr for k in ("город", "сред", "благоустрой", "быт")):
                    current_rubric_id = "urban_life"
                elif any(k in clean_hdr for k in ("контакт", "служеб")):
                    current_rubric_id = "contacts"
                elif any(k in clean_hdr for k in ("друг", "проч", "остальн", "разн")):
                    current_rubric_id = "general"
                else:
                    slug = re.sub(r"\s+", "_", clean_hdr[:30]).strip("_")
                    current_rubric_id = slug if slug else "general"
            continue

        m_marker = re_leading_marker.match(stripped)
        marker_emoji = m_marker.group(0).strip() if m_marker else ""
        rest = stripped[m_marker.end() :].strip() if m_marker else stripped
        rest = rest.lstrip("\ufe0f").strip()

        headline = ""
        body = ""
        m_bold = re_bold_headline.match(rest)
        if m_bold:
            headline = m_bold.group(1).strip().rstrip(".:;, ")
            body = m_bold.group(2).strip()
        elif (
            ":" in rest
            and len(rest.split(":", 1)[0]) <= 80
            and not rest.split(":", 1)[0].startswith("http")
        ):
            h, b = rest.split(":", 1)
            headline = h.strip().rstrip(".:;, ")
            body = b.strip()
        else:
            parts = re.split(r"[.!?]\s+", rest, maxsplit=1)
            headline = parts[0][:60].strip().rstrip(".:;, ")
            body = rest

        if headline or body:
            if not headline:
                headline = body[:50].strip()
            if not body:
                body = headline

            item_toks = {
                t for t in re.findall(r"[\w-]+", (headline + " " + body).lower()) if len(t) >= 3
            }
            scored = sorted(
                [
                    (len(item_toks & ctoks), cid)
                    for cid, ctoks in card_tokens.items()
                    if (item_toks & ctoks)
                ],
                reverse=True,
            )
            covered_sids = [cid for _, cid in scored[:4]] or ([cards[0].id] if cards else [])
            cited_sups: list[str] = []
            for cid in covered_sids:
                cited_sups.extend(story_supports.get(cid, []))
            current_items.append(
                DigestEditorialItemDraft(
                    headline=headline,
                    body=body,
                    covered_story_ids=tuple(covered_sids),
                    cited_support_ids=tuple(cited_sups[:5]),
                    emoji=marker_emoji,
                )
            )
    _flush()
    return DigestNarrativeDraft(blocks=tuple(blocks), situation_items=())


class DigestNarrativeWriter:
    """Single-call narrative digest writer synthesizing flowing prose across rubric blocks."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    async def generate_journalistic_digest(
        self,
        *,
        city: str,
        date_str: str,
        cards: Sequence[StoryCard],
        evidence: Mapping[str, PublicationEvidence] | None = None,
        custom_rubrics: Sequence[Any] | None = None,
        model: str | None = None,
        max_chars: int = 3900,
        target_chars: int = 3500,
    ) -> tuple[str, DigestNarrativeDraft]:
        cards_text_blocks = []
        for c in cards:
            if not c.topic:
                continue
            facts = [f.text for f in getattr(c, "hard_facts", ()) if f.text]
            obs = [o.text for o in getattr(c, "community_observations", ()) if o.text]
            all_details = facts + obs
            details_str = "; ".join(all_details[:3]) if all_details else (c.summary or "")
            block = f"- [{c.topic}] {details_str}"
            cards_text_blocks.append(block)

        content_for_llm = "\n".join(cards_text_blocks[:45])

        from src.domain.edition_geography import resolve_edition_geography

        geo_ctx = resolve_edition_geography(city.lower(), city)
        toponym_section = ""
        if geo_ctx.toponym_rules:
            toponym_section = "\n\nВАЖНЫЕ МЕСТНЫЕ ТОПОНИМЫ И РАЗЛИЧЕНИЕ СУЩНОСТЕЙ:\n" + "\n".join(
                f"- {r}" for r in geo_ctx.toponym_rules
            )

        prompt = DIGEST_PROMPT_TEMPLATE.format(
            city=city,
            date=date_str,
            content=content_for_llm,
            toponym_rules=toponym_section,
        )

        logger.info("Digest Pass 1: Generating full-text journalistic draft...")
        raw_response = await self._provider.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=model,
        )
        clean_draft = _clean_markdown_fence(raw_response)

        # Pass 2: Conditional AI Editorial Condensation
        if len(clean_draft) > max_chars:
            logger.info(
                "Digest draft (%d chars) exceeds limit (%d chars). Running Pass 2: AI Editorial Condenser...",
                len(clean_draft),
                max_chars,
            )
            condense_prompt = DIGEST_CONDENSE_PROMPT_TEMPLATE.format(
                city=city,
                date=date_str,
                current_len=len(clean_draft),
                max_chars=max_chars,
                target_chars=target_chars,
                draft_text=clean_draft,
            )
            raw_condensed = await self._provider.chat_completion(
                messages=[{"role": "user", "content": condense_prompt}],
                model=model,
            )
            clean_draft = _clean_markdown_fence(raw_condensed)

        # Final deterministic safety net
        if len(clean_draft) > max_chars:
            clean_draft = enforce_telegram_single_message_limit(clean_draft, max_chars=max_chars)

        # Normalize known local toponym errors
        from src.processing.operational_semantics import normalize_berdyansk_toponyms

        clean_draft = normalize_berdyansk_toponyms(clean_draft)

        # Enforce neutral administrative terminology (fail-safe against hostile labels)
        clean_draft = sanitize_digest_terminology(clean_draft)

        # Strip ungrounded reader recommendations / unsolicited advice
        clean_draft = strip_unsupported_recommendations(clean_draft, content_for_llm)

        # Strip redundant leading title header or conversational preamble if generated in body
        clean_draft = re.sub(
            r"^\s*[*_#\s]*дайд[жд]*ест[^\n]*[*_#\s]*\n+",
            "",
            clean_draft,
            flags=re.IGNORECASE,
        ).strip()
        clean_draft = re.sub(
            r"^\s*(?:Вот\s+(?:ежедневный\s+)?дайджест[^\n]*\n+|Ниже\s+(?:представлен|следует)[^\n]*\n+|Здравствуйте[^\n]*\n+)+",
            "",
            clean_draft,
            flags=re.IGNORECASE,
        ).strip()

        # 1. Strip all horizontal dividers / markdown rules (---, ***, ___)
        clean_draft = re.sub(r"(?m)^[^\S\r\n]*[-*_]{3,}[^\S\r\n]*$", "", clean_draft)

        # 2. Strip markdown header hashes (#, ##, ###)
        clean_draft = re.sub(r"(?m)^[^\S\r\n]*#{1,6}[^\S\r\n]+", "", clean_draft)

        # 3. Strip standalone bold/italic markup around rubric titles (**Header**, *Header*)
        clean_draft = re.sub(
            r"(?m)^[^\S\r\n]*\*\*[^\S\r\n]*([^\n*]+?)[^\S\r\n]*\*\*[^\S\r\n]*$",
            r"\1",
            clean_draft,
        )
        clean_draft = re.sub(
            r"(?m)^[^\S\r\n]*\*[^\S\r\n]*([^\n*]+?)[^\S\r\n]*\*[^\S\r\n]*$",
            r"\1",
            clean_draft,
        )

        # 4. Collapse multiple blank lines
        clean_draft = re.sub(r"\n{3,}", "\n\n", clean_draft).strip()

        draft = parse_journalistic_markdown_to_draft(
            clean_draft,
            cards=cards,
            evidence=evidence,
            custom_rubrics=custom_rubrics,
        )
        return clean_draft, draft

    async def generate_narrative_draft(
        self,
        *,
        plan: DigestNarrativePlan,
        cards: Sequence[StoryCard],
        evidence: Mapping[str, PublicationEvidence],
        language: str = "Russian",
        max_output_tokens: int = 4096,
        model: str | None = None,
        situation_rollup: Any | None = None,
        situation_plan: Any | None = None,
    ) -> DigestNarrativeDraft:
        """Synthesize structured narrative draft in exactly one LLM call."""
        import json

        from src.publication.narrative_contract import build_digest_narrative_contract

        blocks_payload = []
        for b in plan.blocks:
            supports_payload = []
            for sid in b.support_ids:
                if sid in evidence:
                    evi = evidence[sid]
                    supports_payload.append(
                        {
                            "id": sid,
                            "text": evi.text,
                            "role": evi.source_role,
                            "evidence_kind": evi.kind,
                            "publication_use": evi.publication_use,
                        }
                    )

            block_dict: dict[str, Any] = {
                "block_id": b.block_id,
                "rubric_id": b.rubric_id,
                "rubric_title": b.rubric_title,
                "story_ids": list(b.story_ids),
                "required_story_groups": [list(grp) for grp in b.required_story_groups],
                "canonical_notes": list(b.canonical_notes),
                "supports": supports_payload,
            }
            if b.detail_support_ids_by_story:
                block_dict["detail_support_hints"] = [
                    {"story_id": sid, "detail_support_ids": list(sids)}
                    for sid, sids in b.detail_support_ids_by_story
                ]
            if b.merge_group_by_story:
                block_dict["merge_group_hints"] = [
                    {"story_id": sid, "merge_group_id": mgid}
                    for sid, mgid in b.merge_group_by_story
                ]
            blocks_payload.append(block_dict)

        situation_payload = []
        if situation_plan is not None and getattr(situation_plan, "groups", None):
            for grp in situation_plan.groups:
                grp_supports = []
                all_grp_refs = tuple(
                    dict.fromkeys(
                        list(getattr(grp, "source_refs", ()))
                        + list(getattr(grp, "cited_support_ids", ()))
                    )
                )
                for ref in all_grp_refs:
                    if ref in evidence:
                        evi = evidence[ref]
                        grp_supports.append(
                            {
                                "id": ref,
                                "text": evi.text,
                                "role": evi.source_role,
                                "evidence_kind": evi.kind,
                                "publication_use": evi.publication_use,
                            }
                        )
                req_facts_payload = [
                    {
                        "fact_id": rf.fact_id,
                        "text": rf.text,
                        "support_ids": list(rf.support_ids),
                    }
                    for rf in getattr(grp, "required_facts", ())
                ]
                situation_payload.append(
                    {
                        "group_id": grp.group_id,
                        "label": grp.subject_label,
                        "state": grp.state,
                        "facts": list(grp.all_detail_lines or grp.detail_lines),
                        "required_facts": req_facts_payload,
                        "allowed_support_ids": list(all_grp_refs),
                        "supports": grp_supports,
                    }
                )

        narrative_contract = build_digest_narrative_contract(output_language=language)
        situation_schema = ""
        if situation_payload:
            situation_schema = (
                '  "situation_items": [\n'
                "    {\n"
                '      "group_id": "string (must match input group_id exactly)",\n'
                "      \"emoji\": \"string (semantic emoji, e.g. '⚡️' for power, '💧' for water, '💨' for gas, '🚌' for transport)\",\n"
                '      "label": "string (subject label matching input group label, e.g. \'Электроснабжение\')",\n'
                '      "body": "string (1-3 sentences of cohesive editorial prose synthesizing the operational facts)",\n'
                '      "cited_support_ids": ["string (support IDs cited)"],\n'
                '      "claims": [\n'
                "        {\n"
                '          "text": "string (atomic factual claim in Russian)",\n'
                '          "covered_fact_ids": ["string (fact IDs this claim covers)"],\n'
                '          "covered_story_ids": ["string (story IDs this claim covers)"],\n'
                '          "cited_support_ids": ["string (support IDs supporting this claim)"]\n'
                "        }\n"
                "      ]\n"
                "    }\n"
                "  ],\n"
            )
        else:
            situation_schema = '  "situation_items": [],\n'

        schema_desc = (
            "{\n"
            f"{situation_schema}"
            '  "blocks": [\n'
            "    {\n"
            '      "block_id": "string (must match input block_id exactly)",\n'
            '      "items": [\n'
            "        {\n"
            "          \"emoji\": \"string (thematic semantic emoji, e.g. '⚡️', '💨', '💥', '🛡', '🌐', '🚌', '🏢', '🚫', '📚', '📌')\",\n"
            '          "headline": "string (bold mini-summary answer to what happened)",\n'
            '          "body": "string (cohesive 2-4 sentence narrative covering what happened, micro-locations in parentheses, explanations, and practical consequences)",\n'
            '          "covered_story_ids": ["string (story IDs covered)"],\n'
            '          "cited_support_ids": ["string (support IDs cited)"],\n'
            '          "claims": [\n'
            "            {\n"
            '              "text": "string (atomic factual claim in Russian)",\n'
            '              "covered_story_ids": ["string (story IDs this claim covers)"],\n'
            '              "cited_support_ids": ["string (support IDs supporting this claim)"]\n'
            "            }\n"
            "          ]\n"
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
        )

        system_prompt = (
            "You are a professional regional newsroom editor and journalist.\n"
            "Your task is to write a cohesive, scan-first, and strictly factual daily news digest in Russian.\n\n"
            "EDITORIAL AND LANGUAGE RULES:\n"
            "- Write in professional Russian regional news style matching top Telegram channels.\n"
            "- Never output bullet points ('•') or dashes ('—') at the beginning of items.\n"
            "- For each item, select an accurate thematic semantic emoji (e.g. '⚡️', '💨', '💥', '🛡', '🌐', '🚌', '🏢', '🚫', '📚', '📌') in 'emoji'.\n"
            "- Craft rich, 2-3 sentence journalistic paragraphs following a cohesive storytelling structure:\n"
            "  1. What occurred + concrete micro-locations/districts/streets (in parentheses if listing multiple).\n"
            "  2. Cause or official/specialist explanation (if supported in evidence, e.g. technical works, scheduled maintenance, odorant markers).\n"
            "  3. Practical civic consequences or advice for residents (contacts, workarounds, billing procedures).\n"
            "- If source facts or notes are in Ukrainian, accurately translate and paraphrase them into Russian.\n"
            "- If 'situation_groups' are provided, synthesize each operational group in 'situation_items'. Every required fact in 'required_facts' must be covered in 'claims' and reflected in the narrative body. Use natural chronology and geographical clarity (e.g. outages, low voltage, and restored sections). Never invent ungrounded numbers or causes. Cite the exact support IDs.\n"
            "- In thematic 'blocks', never repeat the headline in the first sentence of the body text.\n"
            "- Never chain repetitive transitional phrases like 'Также... Ранее также...'.\n"
            "- Attribute source role naturally ('По сообщениям жителей', 'По данным коммунальных служб') at most once per item.\n"
            "- For each thematic item, provide atomic claims in 'claims'. Every story in the item's covered_story_ids must be covered by at least one claim atom.\n"
            "- Claims must be short atomic factual statements supported by cited_support_ids.\n\n"
            f"{narrative_contract}\n\n"
            "OUTPUT FORMAT REQUIREMENTS:\n"
            "Return ONLY valid JSON strictly matching this schema:\n"
            f"{schema_desc}"
        )

        user_dict: dict[str, Any] = {"blocks": blocks_payload}
        if situation_payload:
            user_dict["situation_groups"] = situation_payload
        user_prompt = json.dumps(user_dict, ensure_ascii=False, indent=2)

        chat_kwargs: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        if model:
            chat_kwargs["model"] = model
        if max_output_tokens:
            chat_kwargs["max_tokens"] = max_output_tokens

        raw_response = await self._provider.chat_completion(**chat_kwargs)

        cleaned = (raw_response or "").strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        if "{" in cleaned and "}" in cleaned:
            first_brace = cleaned.find("{")
            last_brace = cleaned.rfind("}")
            if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                cleaned = cleaned[first_brace : last_brace + 1]

        try:
            parsed = json.loads(cleaned)
        except Exception as err:
            raise ValueError(
                f"Failed to decode LLM response as JSON: {err}. Raw was: {raw_response[:200]!r}"
            ) from err

        # Consolidate any duplicate block_ids produced by LLM before strict schema instantiation
        if isinstance(parsed, dict) and isinstance(parsed.get("blocks"), list):
            merged_blocks: list[Any] = []
            block_by_id: dict[str, dict[str, Any]] = {}

            for b in parsed["blocks"]:
                if isinstance(b, dict) and b.get("block_id"):
                    bid = str(b["block_id"]).strip()
                    if bid in block_by_id:
                        existing_items = block_by_id[bid].setdefault("items", [])
                        new_items = b.get("items", [])
                        if isinstance(existing_items, list) and isinstance(new_items, list):
                            existing_items.extend(new_items)
                    else:
                        block_by_id[bid] = b
                        merged_blocks.append(b)
                else:
                    merged_blocks.append(b)
            parsed["blocks"] = merged_blocks

        return DigestNarrativeDraft.from_dict(parsed)


def build_digest_support_text_index(
    *,
    evidence: Mapping[str, PublicationEvidence],
    cards: Sequence[StoryCard],
    frozen_input: Any | None = None,
) -> dict[str, str]:
    """Build unified mapping from support IDs and synthesized card IDs to exact support texts."""
    index: dict[str, str] = {}

    # 1. Primary publication evidence items
    for eid, evi in evidence.items():
        if getattr(evi, "text", None):
            index[eid] = evi.text
        elif getattr(evi, "source_text", None):
            index[eid] = evi.source_text

    # 2. Frozen input writer records if present
    if frozen_input is not None and getattr(frozen_input, "writer_bundle", None):
        records = getattr(frozen_input.writer_bundle, "records", {})
        if isinstance(records, dict):
            for ref, rec in records.items():
                msg = getattr(rec, "message", None)
                msg_text = getattr(msg, "text", "") if msg else ""
                if msg_text and ref not in index:
                    index[ref] = msg_text

    # 3. Card-level canonical notes and elements
    for c in cards:
        card_texts: list[str] = []
        if c.topic:
            card_texts.append(c.topic)
        if c.summary:
            card_texts.append(c.summary)
        for hf in c.hard_facts:
            if hf.text:
                card_texts.append(hf.text)
                for r in hf.source_refs:
                    if r not in index and hf.text:
                        index[r] = hf.text
        for ud in c.useful_details:
            if ud.text:
                card_texts.append(ud.text)
                for r in ud.source_refs:
                    if r not in index and ud.text:
                        index[r] = ud.text
        for co in c.community_observations:
            if co.text:
                card_texts.append(co.text)
                for r in co.source_refs:
                    if r not in index and co.text:
                        index[r] = co.text

        if card_texts and c.id not in index:
            index[c.id] = " ".join(card_texts)
        if c.summary and f"{c.id}:summary" not in index:
            index[f"{c.id}:summary"] = c.summary

    return index
