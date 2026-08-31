"""Deterministic block planning, models, validation, and single-call writer for Event-First narrative digests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.editorial_models import StoryCard
from src.publication.article_claims import ConcreteClaim, find_unsupported_claims
from src.publication.evidence import PublicationEvidence

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
    detail_support_ids_by_story: tuple[tuple[str, tuple[str, ...]], ...] = ()
    merge_group_by_story: tuple[tuple[str, str], ...] = ()
    detail_roles_by_story: tuple[tuple[str, str], ...] = ()
    presentation_modes_by_story: tuple[tuple[str, str], ...] = ()
    dashboard_support_ids_by_story: tuple[tuple[str, tuple[str, ...]], ...] = ()


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

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DigestSituationItemDraft:
        if not isinstance(raw, Mapping):
            raise ValueError("situation item must be a mapping")
        group_id = str(raw.get("group_id", "")).strip()
        label = str(raw.get("label", "")).strip()
        body = str(raw.get("body", "")).strip()
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
        return cls(
            group_id=group_id,
            label=label,
            body=body,
            cited_support_ids=support_ids,
        )


@dataclass(frozen=True)
class DigestEditorialItemDraft:
    """A single scan-first editorial item within a narrative digest block."""

    headline: str
    body: str
    covered_story_ids: tuple[str, ...]
    cited_support_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DigestEditorialItemDraft:
        if not isinstance(raw, Mapping):
            raise ValueError("digest item must be a mapping")
        headline = str(raw.get("headline", "")).strip()
        body = str(raw.get("body", "")).strip()
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
        if not headline or not body or not story_ids or not support_ids:
            raise ValueError("digest editorial item requires headline, body, stories and supports")
        return cls(
            headline=headline,
            body=body,
            covered_story_ids=story_ids,
            cited_support_ids=support_ids,
        )


@dataclass(frozen=True)
class DigestNarrativeBlockDraft:
    """A single rendered block in a narrative digest draft."""

    block_id: str
    items: tuple[DigestEditorialItemDraft, ...]


@dataclass(frozen=True)
class DigestNarrativeDraft:
    """Complete output draft from the single-call narrative digest writer."""

    blocks: tuple[DigestNarrativeBlockDraft, ...]
    situation_items: tuple[DigestSituationItemDraft, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> DigestNarrativeDraft:
        """Parse structured narrative digest draft with strict structural validation."""
        if not isinstance(data, Mapping):
            raise ValueError("root must be a mapping")

        raw_situation = data.get("situation_items", [])
        if raw_situation is None:
            raw_situation = []
        if not isinstance(raw_situation, list):
            raise ValueError("'situation_items' must be a list")

        seen_sit_ids: set[str] = set()
        situation_drafts: list[DigestSituationItemDraft] = []
        for s_raw in raw_situation:
            item = DigestSituationItemDraft.from_dict(s_raw)
            if item.group_id in seen_sit_ids:
                raise ValueError(f"duplicate situation group_id: {item.group_id}")
            seen_sit_ids.add(item.group_id)
            situation_drafts.append(item)

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

        return cls(blocks=tuple(block_drafts), situation_items=tuple(situation_drafts))


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

    for rid, rname, _ in rubric_infos:
        if not rid:
            continue
        rubric_cards = cards_by_rubric.get(rid, [])
        if not rubric_cards:
            continue

        bound = max(1, max_cards_per_block)
        for chunk_idx in range(0, len(rubric_cards), bound):
            chunk = rubric_cards[chunk_idx : chunk_idx + bound]
            block_id = f"block:{rid}:{chunk_idx // bound}"
            story_ids = tuple(c.id for c in chunk)

            # Collect canonical notes from cards
            notes: list[str] = []
            block_support_ids: list[str] = []
            for c in chunk:
                if c.summary:
                    notes.append(f"{c.topic}: {c.summary}")
                elif c.topic:
                    notes.append(c.topic)
                for hf in c.hard_facts:
                    if hf.text and hf.text not in notes:
                        notes.append(hf.text)
                for co in c.community_observations:
                    if co.text and co.text not in notes:
                        notes.append(co.text)

                # Extract numeric story ID if story:123
                if c.id.startswith("story:"):
                    raw_sid = c.id.split(":", 1)[1]
                    if raw_sid.isdigit():
                        num_sid = int(raw_sid)
                        for eid, evi in evidence.items():
                            if (
                                getattr(evi, "story_id", None) == num_sid
                                and getattr(evi, "publication_use", "PUBLISH") == "PUBLISH"
                                and eid not in block_support_ids
                            ):
                                block_support_ids.append(eid)

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

            blocks.append(
                DigestNarrativeBlock(
                    block_id=block_id,
                    rubric_id=rid,
                    rubric_title=rname,
                    story_ids=story_ids,
                    support_ids=tuple(block_support_ids),
                    canonical_notes=tuple(notes),
                    detail_support_ids_by_story=detail_supports,
                    merge_group_by_story=merge_groups,
                    detail_roles_by_story=detail_roles,
                    presentation_modes_by_story=pres_modes,
                    dashboard_support_ids_by_story=dash_supports,
                )
            )

    return DigestNarrativePlan(blocks=tuple(blocks))


DIGEST_ITEM_HEADLINE_MAX_CHARS = 140
DIGEST_ITEM_BODY_MAX_CHARS = 900
DIGEST_ITEM_MAX_STORIES = 6
DIGEST_SITUATION_BODY_MAX_CHARS = 360


def validate_digest_narrative(
    draft: DigestNarrativeDraft,
    plan: DigestNarrativePlan,
    support_index: Mapping[str, str] | None = None,
    *,
    support_text_by_id: Mapping[str, str] | None = None,
    situation_plan: Any = None,
) -> DigestNarrativeValidationResult:
    """Validate structured narrative digest draft strictly against deterministic plan and evidence."""
    from src.publication.digest_relation_support import find_unsupported_digest_relations

    violations: list[str] = []
    unsupported_claims: list[Any] = []
    support_map = support_index if support_index is not None else (support_text_by_id or {})

    # Validate City Situation items if situation_plan is supplied
    if situation_plan is not None and draft.situation_items:
        plan_groups = getattr(situation_plan, "groups", ()) or ()
        plan_group_ids = [g.group_id for g in plan_groups]
        draft_group_ids = [s.group_id for s in draft.situation_items]
        plan_groups_by_id = {g.group_id: g for g in plan_groups}

        if len(draft.situation_items) != len(plan_groups):
            violations.append(
                f"SITUATION_GROUP_SET_MISMATCH: expected {len(plan_groups)} groups, got {len(draft.situation_items)}"
            )
        elif draft_group_ids != plan_group_ids:
            violations.append(
                f"SITUATION_GROUP_SET_MISMATCH: expected {plan_group_ids}, got {draft_group_ids}"
            )

        for sit_item in draft.situation_items:
            plan_grp = plan_groups_by_id.get(sit_item.group_id)
            if plan_grp is None:
                violations.append(f"UNKNOWN_SITUATION_GROUP_ID: {sit_item.group_id}")
                continue

            if len(sit_item.body) > DIGEST_SITUATION_BODY_MAX_CHARS:
                violations.append(
                    f"SITUATION_BODY_TOO_LONG: body exceeds {DIGEST_SITUATION_BODY_MAX_CHARS} chars in {sit_item.group_id}"
                )

            if _INTERNAL_LEAKAGE_RE.search(sit_item.label) or _INTERNAL_LEAKAGE_RE.search(
                sit_item.body
            ):
                violations.append(
                    f"INTERNAL_ID_LEAK: found internal identifier in situation item {sit_item.group_id}"
                )

            if not sit_item.cited_support_ids:
                violations.append(
                    f"MISSING_SUPPORT_CITATION: situation item {sit_item.group_id} cites no supports"
                )

            allowed_sit_supports = set(getattr(plan_grp, "source_refs", ()))
            for sup_id in sit_item.cited_support_ids:
                if sup_id not in allowed_sit_supports and allowed_sit_supports:
                    violations.append(
                        f"SUPPORT_OUTSIDE_GROUP: {sup_id} not allowed in situation group {sit_item.group_id}"
                    )
                if sup_id not in support_map:
                    violations.append(
                        f"UNKNOWN_SUPPORT_ID: {sup_id} not found in support text index"
                    )

            c_supports = [support_map[s] for s in sit_item.cited_support_ids if s in support_map]
            for unc in find_unsupported_claims(sit_item.label, c_supports):
                unsupported_claims.append(unc)
                violations.append(
                    f"UNSUPPORTED_CONCRETE_CLAIM: [{unc.kind}] '{unc.raw}' in situation label {sit_item.group_id}"
                )
            for unc in find_unsupported_claims(sit_item.body, c_supports):
                unsupported_claims.append(unc)
                violations.append(
                    f"UNSUPPORTED_CONCRETE_CLAIM: [{unc.kind}] '{unc.raw}' in situation body {sit_item.group_id}"
                )
            for rel in find_unsupported_digest_relations(sit_item.label, c_supports):
                violations.append(
                    f"UNSUPPORTED_DIGEST_RELATION: '{rel.raw}' in situation label {sit_item.group_id}"
                )
            for rel in find_unsupported_digest_relations(sit_item.body, c_supports):
                violations.append(
                    f"UNSUPPORTED_DIGEST_RELATION: '{rel.raw}' in situation body {sit_item.group_id}"
                )

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
        detail_roles_map = dict(plan_block.detail_roles_by_story)
        detail_supports_map = dict(plan_block.detail_support_ids_by_story)
        modes_map = dict(getattr(plan_block, "presentation_modes_by_story", ()))
        dashboard_supports_map = dict(getattr(plan_block, "dashboard_support_ids_by_story", ()))

        flat_story_ids = [sid for item in out_block.items for sid in item.covered_story_ids]
        if len(flat_story_ids) != len(set(flat_story_ids)):
            violations.append(f"DUPLICATE_STORY_COVERAGE: {out_block.block_id}")

        for sid in flat_story_ids:
            if sid not in expected_story_ids:
                violations.append(f"UNKNOWN_STORY_ID: {sid} in block {out_block.block_id}")

        if set(flat_story_ids) != expected_story_ids:
            violations.append(f"STORY_PARTITION_MISMATCH: {out_block.block_id}")

        for item in out_block.items:
            if len(item.covered_story_ids) > DIGEST_ITEM_MAX_STORIES:
                violations.append(
                    f"ITEM_TOO_MANY_STORIES: item in block {out_block.block_id} covers {len(item.covered_story_ids)} stories (max {DIGEST_ITEM_MAX_STORIES})"
                )
            if len(item.covered_story_ids) > 1 and merge_group_map:
                m_groups = {merge_group_map.get(sid, sid) for sid in item.covered_story_ids}
                if len(m_groups) > 1:
                    violations.append(f"UNRELATED_STORY_GROUPING: {out_block.block_id}")
            if len(item.headline) > DIGEST_ITEM_HEADLINE_MAX_CHARS:
                violations.append(
                    f"HEADLINE_TOO_LONG: headline exceeds {DIGEST_ITEM_HEADLINE_MAX_CHARS} chars in block {out_block.block_id}"
                )
            if len(item.body) > DIGEST_ITEM_BODY_MAX_CHARS:
                violations.append(
                    f"BODY_TOO_LONG: body exceeds {DIGEST_ITEM_BODY_MAX_CHARS} chars in block {out_block.block_id}"
                )

            if _INTERNAL_LEAKAGE_RE.search(item.headline) or _INTERNAL_LEAKAGE_RE.search(item.body):
                violations.append(
                    f"INTERNAL_ID_LEAK: found internal identifier in block {out_block.block_id}"
                )

            if not item.cited_support_ids:
                violations.append(
                    f"MISSING_SUPPORT_CITATION: item in block {out_block.block_id} cites no supports"
                )

            for sid in item.covered_story_ids:
                mode = modes_map.get(sid)
                role = detail_roles_map.get(sid)
                if mode == "DASHBOARD_AND_DRILLDOWN" or role == "DRILL_DOWN":
                    dashboard_ids = set(dashboard_supports_map.get(sid, ()))
                    story_detail_ids = set(detail_supports_map.get(sid, ()))
                    item_ids = set(item.cited_support_ids)
                    if story_detail_ids and not (item_ids & story_detail_ids):
                        violations.append(
                            f"DRILL_DOWN_MISSING_DISTINCT_SUPPORT: drill-down story {sid} in block {out_block.block_id} must cite at least one non-dashboard PUBLISH support"
                        )
                    elif (
                        dashboard_ids
                        and not ((item_ids & story_detail_ids) - dashboard_ids)
                        and not (item_ids - dashboard_ids)
                    ):
                        violations.append(
                            f"DRILL_DOWN_MISSING_DISTINCT_SUPPORT: drill-down story {sid} in block {out_block.block_id} must cite at least one non-dashboard PUBLISH support"
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
            for unc in find_unsupported_claims(item.headline, c_supports):
                unsupported_claims.append(unc)
                violations.append(
                    f"UNSUPPORTED_CONCRETE_CLAIM: [{unc.kind}] '{unc.raw}' in headline of block {out_block.block_id}"
                )
            for unc in find_unsupported_claims(item.body, c_supports):
                unsupported_claims.append(unc)
                violations.append(
                    f"UNSUPPORTED_CONCRETE_CLAIM: [{unc.kind}] '{unc.raw}' in body of block {out_block.block_id}"
                )
            for rel in find_unsupported_digest_relations(item.headline, c_supports):
                violations.append(
                    f"UNSUPPORTED_DIGEST_RELATION: '{rel.raw}' in headline of block {out_block.block_id}"
                )
            for rel in find_unsupported_digest_relations(item.body, c_supports):
                violations.append(
                    f"UNSUPPORTED_DIGEST_RELATION: '{rel.raw}' in body of block {out_block.block_id}"
                )

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
) -> DigestNarrativeDraft:
    """Build a deterministic, provenance-bearing DigestNarrativeDraft from the presentation plan."""
    from src.publication.article_claims import find_unsupported_claims

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
        for sid in plan_block.story_ids:
            pres = presentations_by_id.get(sid)
            card = cards_by_id.get(sid)
            if not card:
                continue

            dash_supp_ids = dashboard_supports_by_story.get(sid, set())
            eligible_supports: list[str] = []

            if pres and pres.detail_support_ids:
                for supp_id in pres.detail_support_ids:
                    if supp_id not in dash_supp_ids and supp_id in evidence:
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
                raise ValueError(f"no deterministic detail support for {sid}")

            chosen_supports = eligible_supports[:2]
            support_texts = [
                evidence[s].text or evidence[s].source_text
                for s in chosen_supports
                if s in evidence
            ]

            topic = card.topic.strip()
            if topic and not find_unsupported_claims(topic, support_texts):
                headline = topic
            else:
                first_evi = evidence[chosen_supports[0]]
                headline = _render_deterministic_digest_evidence(first_evi).rstrip(".")

            rendered_sentences = [
                _render_deterministic_digest_evidence(evidence[s])
                for s in chosen_supports
                if s in evidence
            ]

            item_drafts.append(
                DigestEditorialItemDraft(
                    headline=headline,
                    body=" ".join(rendered_sentences),
                    covered_story_ids=(sid,),
                    cited_support_ids=tuple(chosen_supports),
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


class DigestNarrativeWriter:
    """Single-call narrative digest writer synthesizing flowing prose across rubric blocks."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

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

        narrative_contract = build_digest_narrative_contract(output_language=language)
        schema_desc = (
            "{\n"
            '  "blocks": [\n'
            "    {\n"
            '      "block_id": "string (must match input block_id exactly)",\n'
            '      "items": [\n'
            "        {\n"
            '          "headline": "string (bold mini-summary answer to what happened)",\n'
            '          "body": "string (compact 2-4 sentences adding context/chronology/status/microdetails)",\n'
            '          "covered_story_ids": ["string (story IDs covered)"],\n'
            '          "cited_support_ids": ["string (support IDs cited)"]\n'
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
        )

        system_prompt = (
            "You are a professional regional newsroom editor and journalist.\n"
            "Your task is to write a cohesive, engaging, and strictly factual daily news digest.\n\n"
            f"{narrative_contract}\n\n"
            "OUTPUT FORMAT REQUIREMENTS:\n"
            "Return ONLY valid JSON strictly matching this schema:\n"
            f"{schema_desc}"
        )
        user_dict = {"blocks": blocks_payload}
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

    return index
