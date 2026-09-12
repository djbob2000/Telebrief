"""Canonical DigestCoverageTrace models and builders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.publication.digest_narrative import (
    DigestNarrativeDraft,
    DigestNarrativePlan,
)
from src.publication.digest_presentation import (
    DigestPresentationMode,
    DigestPresentationPlan,
    city_situation_group_reader_text,
)
from src.publication.errors import DigestCoverageInvariantError


@dataclass(frozen=True)
class DigestStoryCoverageTrace:
    story_id: str
    mode: DigestPresentationMode
    city_situation_group_ids: tuple[str, ...]
    detail_item_ids: tuple[str, ...]
    dashboard_support_ids: tuple[str, ...]
    detail_support_ids: tuple[str, ...]
    dashboard_texts: tuple[str, ...] = ()
    detail_texts: tuple[str, ...] = ()


@dataclass(frozen=True)
class DigestFactCoverageTrace:
    fact_id: str
    group_id: str
    story_ids: tuple[str, ...]
    required_support_ids: tuple[str, ...]
    covered: bool
    cited_support_ids: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class DigestCoverageTrace:
    stories: tuple[DigestStoryCoverageTrace, ...]
    facts: tuple[DigestFactCoverageTrace, ...] = ()

    @property
    def story_ids(self) -> tuple[str, ...]:
        return tuple(item.story_id for item in self.stories)

    @property
    def story_coverage(self) -> float:
        if not self.stories:
            return 1.0
        covered = 0
        for item in self.stories:
            dashboard_ok = bool(item.city_situation_group_ids and item.dashboard_support_ids)
            detail_ok = bool(item.detail_item_ids and item.detail_support_ids)
            if item.mode == "DASHBOARD_ONLY" and dashboard_ok:
                covered += 1
            elif item.mode == "DETAIL_ONLY" and detail_ok:
                covered += 1
            elif item.mode == "DASHBOARD_AND_DRILLDOWN" and dashboard_ok and detail_ok:
                covered += 1
        return covered / len(self.stories)

    @property
    def material_fact_coverage(self) -> float:
        if not self.facts:
            return 1.0
        covered = sum(1 for f in self.facts if f.covered)
        return covered / len(self.facts)

    def to_dict(self) -> list[dict[str, Any]]:
        return [
            {
                "story_id": s.story_id,
                "mode": s.mode,
                "city_situation_group_ids": list(s.city_situation_group_ids),
                "detail_item_ids": list(s.detail_item_ids),
                "dashboard_support_ids": list(s.dashboard_support_ids),
                "detail_support_ids": list(s.detail_support_ids),
                "dashboard_texts": list(s.dashboard_texts),
                "detail_texts": list(s.detail_texts),
            }
            for s in self.stories
        ]

    def facts_to_dict(self) -> list[dict[str, Any]]:
        return [
            {
                "fact_id": f.fact_id,
                "group_id": f.group_id,
                "story_ids": list(f.story_ids),
                "required_support_ids": list(f.required_support_ids),
                "covered": f.covered,
                "cited_support_ids": list(f.cited_support_ids),
                "text": f.text,
            }
            for f in self.facts
        ]


def build_digest_coverage_trace(
    plan: DigestPresentationPlan,
    final_draft: DigestNarrativeDraft,
    narrative_plan: DigestNarrativePlan | None = None,
) -> DigestCoverageTrace:
    """Build and audit the canonical final digest coverage trace."""
    # Map dashboard coverage
    dashboard_groups_by_story: dict[str, list[str]] = {}
    dashboard_supports_by_story: dict[str, list[str]] = {}
    dashboard_texts_by_story: dict[str, list[str]] = {}

    has_situation_draft = bool(final_draft and getattr(final_draft, "situation_items", None))

    if plan.city_situation and plan.city_situation.groups:
        if not has_situation_draft:
            # Deterministic template rendering mode: plan groups are rendered directly
            for group in plan.city_situation.groups:
                group_text = city_situation_group_reader_text(group)
                for sid in group.covered_story_ids:
                    if group.group_id not in dashboard_groups_by_story.setdefault(sid, []):
                        dashboard_groups_by_story[sid].append(group.group_id)
                    if group_text not in dashboard_texts_by_story.setdefault(sid, []):
                        dashboard_texts_by_story[sid].append(group_text)
                    for sup_id in group.cited_support_ids:
                        if sup_id not in dashboard_supports_by_story.setdefault(sid, []):
                            dashboard_supports_by_story[sid].append(sup_id)
        else:
            # Narrative single_call mode: ONLY credit what situation_items actually express!
            for s_item in final_draft.situation_items:
                s_group = next(
                    (g for g in plan.city_situation.groups if g.group_id == s_item.group_id), None
                )
                if not s_group:
                    continue
                sit_text = f"{s_item.label}: {s_item.body}"
                if s_item.claims:
                    for claim in s_item.claims:
                        c_text = claim.text.strip()
                        c_supports = tuple(claim.cited_support_ids)
                        claim_stories: set[str] = set(claim.covered_story_ids)
                        for rf in getattr(s_group, "required_facts", ()):
                            if (set(c_supports) & set(rf.support_ids)) or (
                                claim.covered_fact_ids and rf.fact_id in claim.covered_fact_ids
                            ):
                                claim_stories.update(rf.story_ids)
                        for sid in claim_stories:
                            if s_group.group_id not in dashboard_groups_by_story.setdefault(
                                sid, []
                            ):
                                dashboard_groups_by_story[sid].append(s_group.group_id)
                            if c_text and c_text not in dashboard_texts_by_story.setdefault(
                                sid, []
                            ):
                                dashboard_texts_by_story[sid].append(c_text)
                            elif sit_text not in dashboard_texts_by_story.setdefault(sid, []):
                                dashboard_texts_by_story[sid].append(sit_text)
                            for sup_id in c_supports:
                                if sup_id not in dashboard_supports_by_story.setdefault(sid, []):
                                    dashboard_supports_by_story[sid].append(sup_id)
                else:
                    for sid in s_group.covered_story_ids:
                        for rf in getattr(s_group, "required_facts", ()):
                            if sid in rf.story_ids and (
                                set(s_item.cited_support_ids) & set(rf.support_ids)
                            ):
                                if s_group.group_id not in dashboard_groups_by_story.setdefault(
                                    sid, []
                                ):
                                    dashboard_groups_by_story[sid].append(s_group.group_id)
                                if sit_text not in dashboard_texts_by_story.setdefault(sid, []):
                                    dashboard_texts_by_story[sid].append(sit_text)
                                for sup_id in set(s_item.cited_support_ids) & set(rf.support_ids):
                                    if sup_id not in dashboard_supports_by_story.setdefault(
                                        sid, []
                                    ):
                                        dashboard_supports_by_story[sid].append(sup_id)

    # Audit material operational facts
    fact_traces: list[DigestFactCoverageTrace] = []
    if plan.required_facts:
        for rf in plan.required_facts:
            covered = False
            cited_sups: list[str] = []

            # 1. Match situation items if present
            if final_draft and getattr(final_draft, "situation_items", None):
                for sit_item in final_draft.situation_items:
                    if sit_item.claims:
                        for c in sit_item.claims:
                            c_sups = set(c.cited_support_ids)
                            if (set(rf.support_ids) & c_sups) or (
                                c.covered_fact_ids and rf.fact_id in c.covered_fact_ids
                            ):
                                covered = True
                                for s in c.cited_support_ids:
                                    if s not in cited_sups:
                                        cited_sups.append(s)
                    else:
                        if set(rf.support_ids) & set(sit_item.cited_support_ids):
                            covered = True
                            for s in sit_item.cited_support_ids:
                                if s in set(rf.support_ids) and s not in cited_sups:
                                    cited_sups.append(s)

            # 2. Match detail block items and claims
            if final_draft and getattr(final_draft, "blocks", None):
                for block in final_draft.blocks:
                    for it in block.items:
                        if getattr(it, "claims", None):
                            for c in it.claims:
                                c_sups = set(c.cited_support_ids)
                                if (set(rf.support_ids) & c_sups) or (
                                    c.covered_fact_ids and rf.fact_id in c.covered_fact_ids
                                ):
                                    covered = True
                                    for s in c.cited_support_ids:
                                        if s not in cited_sups:
                                            cited_sups.append(s)
                        else:
                            if set(rf.support_ids) & set(it.cited_support_ids):
                                covered = True
                                for s in it.cited_support_ids:
                                    if s in set(rf.support_ids) and s not in cited_sups:
                                        cited_sups.append(s)

            # 3. Deterministic template rendering mode fallback
            if not covered and not has_situation_draft and not getattr(final_draft, "blocks", None):
                covered = True
                cited_sups.extend(rf.support_ids)

            fact_traces.append(
                DigestFactCoverageTrace(
                    fact_id=rf.fact_id,
                    group_id=f"situation:{rf.subject_key}",
                    story_ids=rf.story_ids,
                    required_support_ids=rf.support_ids,
                    covered=covered,
                    cited_support_ids=tuple(dict.fromkeys(cited_sups)),
                    text=rf.text,
                )
            )
    elif plan.city_situation and plan.city_situation.groups:
        for group in plan.city_situation.groups:
            matched_sit_item: Any = None
            if final_draft and getattr(final_draft, "situation_items", None):
                matched_sit_item = next(
                    (it for it in final_draft.situation_items if it.group_id == group.group_id),
                    None,
                )
            for rf in getattr(group, "required_facts", ()):
                covered = False
                cited_sups = []
                if matched_sit_item is not None:
                    if matched_sit_item.claims:
                        for c in matched_sit_item.claims:
                            c_sups = set(c.cited_support_ids)
                            if (set(rf.support_ids) & c_sups) or (
                                c.covered_fact_ids and rf.fact_id in c.covered_fact_ids
                            ):
                                covered = True
                                for s in c.cited_support_ids:
                                    if s not in cited_sups:
                                        cited_sups.append(s)
                    else:
                        if set(rf.support_ids) & set(matched_sit_item.cited_support_ids):
                            covered = True
                            cited_sups.extend(
                                s
                                for s in matched_sit_item.cited_support_ids
                                if s in set(rf.support_ids)
                            )
                elif not has_situation_draft:
                    covered = True
                    cited_sups.extend(rf.support_ids)

                fact_traces.append(
                    DigestFactCoverageTrace(
                        fact_id=rf.fact_id,
                        group_id=group.group_id,
                        story_ids=rf.story_ids,
                        required_support_ids=rf.support_ids,
                        covered=covered,
                        cited_support_ids=tuple(cited_sups),
                        text=rf.text,
                    )
                )

    # Build story-to-allowed-supports mapping from narrative_plan if provided
    story_to_allowed_supports: dict[str, set[str]] = {}
    if narrative_plan is not None:
        for plan_block in narrative_plan.blocks:
            for sid, sup_ids in getattr(plan_block, "support_ids_by_story", ()):
                story_to_allowed_supports.setdefault(sid, set()).update(sup_ids)

    # Map detail coverage from final_draft
    detail_items_by_story: dict[str, list[str]] = {}
    detail_supports_by_story: dict[str, list[str]] = {}
    detail_texts_by_story: dict[str, list[str]] = {}

    for block in final_draft.blocks:
        for idx, item in enumerate(block.items):
            item_id = f"{block.block_id}:item:{idx}"
            detail_text = f"{item.headline.strip()}: {item.body.strip()}"
            if getattr(item, "claims", None):
                for claim in item.claims:
                    claim_text = claim.text.strip()
                    for sid in claim.covered_story_ids:
                        if item_id not in detail_items_by_story.setdefault(sid, []):
                            detail_items_by_story[sid].append(item_id)
                        if claim_text and claim_text not in detail_texts_by_story.setdefault(
                            sid, []
                        ):
                            detail_texts_by_story[sid].append(claim_text)
                        allowed_for_story = story_to_allowed_supports.get(sid)
                        for sup_id in claim.cited_support_ids:
                            if allowed_for_story is None or sup_id in allowed_for_story:
                                if sup_id not in detail_supports_by_story.setdefault(sid, []):
                                    detail_supports_by_story[sid].append(sup_id)
            else:
                for sid in item.covered_story_ids:
                    if item_id not in detail_items_by_story.setdefault(sid, []):
                        detail_items_by_story[sid].append(item_id)
                    if detail_text not in detail_texts_by_story.setdefault(sid, []):
                        detail_texts_by_story[sid].append(detail_text)

                    allowed_for_story = story_to_allowed_supports.get(sid)
                    for sup_id in item.cited_support_ids:
                        if allowed_for_story is None or sup_id in allowed_for_story:
                            if sup_id not in detail_supports_by_story.setdefault(sid, []):
                                detail_supports_by_story[sid].append(sup_id)

    story_traces: list[DigestStoryCoverageTrace] = []
    for pres in plan.story_presentations:
        sid = pres.story_id
        dash_groups = tuple(dashboard_groups_by_story.get(sid, ()))
        dash_supports = tuple(dashboard_supports_by_story.get(sid, ()))
        dash_texts = tuple(dashboard_texts_by_story.get(sid, ()))
        detail_items = tuple(detail_items_by_story.get(sid, ()))
        detail_supports = tuple(detail_supports_by_story.get(sid, ()))
        detail_texts = tuple(detail_texts_by_story.get(sid, ()))

        if pres.mode == "DASHBOARD_ONLY":
            if not dash_groups or not dash_supports:
                raise DigestCoverageInvariantError(
                    f"missing dashboard coverage for {sid} (mode=DASHBOARD_ONLY)"
                )
        elif pres.mode == "DETAIL_ONLY":
            if not detail_items or not detail_supports:
                raise DigestCoverageInvariantError(
                    f"missing detail coverage for {sid} (mode=DETAIL_ONLY)"
                )
        elif pres.mode == "DASHBOARD_AND_DRILLDOWN":
            if not dash_groups or not dash_supports:
                raise DigestCoverageInvariantError(
                    f"missing dashboard coverage for {sid} (mode=DASHBOARD_AND_DRILLDOWN)"
                )
            if not detail_items or not detail_supports:
                raise DigestCoverageInvariantError(
                    f"missing detail coverage for {sid} (mode=DASHBOARD_AND_DRILLDOWN)"
                )

        story_traces.append(
            DigestStoryCoverageTrace(
                story_id=sid,
                mode=pres.mode,
                city_situation_group_ids=dash_groups,
                detail_item_ids=detail_items,
                dashboard_support_ids=dash_supports,
                detail_support_ids=detail_supports,
                dashboard_texts=dash_texts,
                detail_texts=detail_texts,
            )
        )

    trace = DigestCoverageTrace(stories=tuple(story_traces), facts=tuple(fact_traces))
    if set(trace.story_ids) != set(plan.story_ids):
        raise DigestCoverageInvariantError(
            f"trace story set mismatch: {trace.story_ids} vs {plan.story_ids}"
        )
    return trace
