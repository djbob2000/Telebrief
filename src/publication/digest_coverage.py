"""Canonical DigestCoverageTrace models and builders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.publication.digest_narrative import DigestNarrativeDraft
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
class DigestCoverageTrace:
    stories: tuple[DigestStoryCoverageTrace, ...]

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


def build_digest_coverage_trace(
    plan: DigestPresentationPlan,
    final_draft: DigestNarrativeDraft,
) -> DigestCoverageTrace:
    """Build and audit the canonical final digest coverage trace."""
    # Map dashboard coverage
    dashboard_groups_by_story: dict[str, list[str]] = {}
    dashboard_supports_by_story: dict[str, list[str]] = {}
    dashboard_texts_by_story: dict[str, list[str]] = {}

    if plan.city_situation and plan.city_situation.groups:
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

    # Map detail coverage from final_draft
    detail_items_by_story: dict[str, list[str]] = {}
    detail_supports_by_story: dict[str, list[str]] = {}
    detail_texts_by_story: dict[str, list[str]] = {}

    for block in final_draft.blocks:
        for idx, item in enumerate(block.items):
            item_id = f"{block.block_id}:item:{idx}"
            detail_text = f"{item.headline.strip()}: {item.body.strip()}"
            for sid in item.covered_story_ids:
                if item_id not in detail_items_by_story.setdefault(sid, []):
                    detail_items_by_story[sid].append(item_id)
                if detail_text not in detail_texts_by_story.setdefault(sid, []):
                    detail_texts_by_story[sid].append(detail_text)
                for sup_id in item.cited_support_ids:
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

    trace = DigestCoverageTrace(stories=tuple(story_traces))
    if set(trace.story_ids) != set(plan.story_ids):
        raise DigestCoverageInvariantError(
            f"trace story set mismatch: {trace.story_ids} vs {plan.story_ids}"
        )
    return trace
