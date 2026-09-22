"""Presentation-level composition bundles for Event-First article writing.

Bundles are deliberately a view over an :class:`ArticleCoveragePlan`.  They
give the writer a useful thematic shape while leaving Stories, evidence, and
their provenance independent for validation.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleProminence,
    ArticleStoryCoverage,
    _story_topic_signature,
)

if TYPE_CHECKING:
    from src.publication.article_context import ArticleEditorialContext
    from src.publication.article_material import ArticleMaterialProjection


_STORY_ID_RE = re.compile(r"story:(?:[^:]+|\d+)")


@dataclass(frozen=True)
class ArticleCompositionBundle:
    """A thematic presentation container with independently traceable members."""

    bundle_id: str
    section_id: str
    theme_key: str
    lead_story_id: str
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    prominence: ArticleProminence


@dataclass(frozen=True)
class ArticleCompositionPlan:
    """Composition bundles and the projection-only stories omitted from them."""

    bundles: tuple[ArticleCompositionBundle, ...]
    suppressed_story_ids: tuple[str, ...] = ()

    @property
    def bundle_by_story_id(self) -> dict[str, ArticleCompositionBundle]:
        return {story_id: bundle for bundle in self.bundles for story_id in bundle.story_ids}

    def to_metadata(self) -> dict[str, object]:
        return {
            "bundle_count": len(self.bundles),
            "suppressed_story_ids": list(self.suppressed_story_ids),
            "bundles": [
                {
                    "bundle_id": bundle.bundle_id,
                    "section_id": bundle.section_id,
                    "theme_key": bundle.theme_key,
                    "lead_story_id": bundle.lead_story_id,
                    "story_ids": list(bundle.story_ids),
                    "support_ids": list(bundle.support_ids),
                    "prominence": bundle.prominence,
                }
                for bundle in self.bundles
            ],
        }


def _story_id_from_support_id(support_id: str) -> str:
    match = _STORY_ID_RE.search(support_id)
    return match.group(0) if match else ""


def _support_belongs_to_story(
    support_id: str, story_id: str, context: ArticleEditorialContext
) -> bool:
    support = getattr(context, "support_by_id", {}).get(support_id)
    owner = getattr(support, "story_id", "") if support is not None else ""
    owner = owner or _story_id_from_support_id(support_id)
    return owner == story_id


def _story_support_ids(
    story_id: str,
    coverage_support_ids: tuple[str, ...],
    context: ArticleEditorialContext,
) -> tuple[str, ...]:
    """Keep a member's own planned evidence when plans pool thread supports."""
    return tuple(
        dict.fromkeys(
            support_id
            for support_id in coverage_support_ids
            if _support_belongs_to_story(support_id, story_id, context)
        )
    )


def build_article_composition_plan(
    coverage_plan: ArticleCoveragePlan,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> ArticleCompositionPlan:
    """Group plan Stories by final section and generic topic signature.

    The grouping key is presentation-only.  Each Story remains a separate
    member, and the bundle's support IDs are only an index of member evidence;
    the writer renderer resolves and prints support lines per member.
    """
    plan_story_ids = set(coverage_plan.story_ids)
    suppressed = tuple(
        story_id
        for story_id in coverage_plan.story_ids
        if story_id in set(material_projection.suppressed_story_ids)
    )

    grouped: dict[tuple[str, str], list[ArticleStoryCoverage]] = defaultdict(list)
    for story in coverage_plan.stories:
        if story.story_id in suppressed:
            continue
        section = coverage_plan.section_for_story(story.story_id)
        section_id = section.section_id if section is not None else "city_life"
        theme_key = _story_topic_signature(story, context)
        grouped[(section_id, theme_key)].append(story)

    bundles: list[ArticleCompositionBundle] = []
    for bundle_index, ((section_id, theme_key), stories) in enumerate(grouped.items(), start=1):
        ordered_stories = sorted(stories, key=lambda story: (story.rank, story.story_id))
        story_ids = tuple(story.story_id for story in ordered_stories)
        lead = ordered_stories[0]
        support_ids: list[str] = []
        for story in ordered_stories:
            planned_support_ids = tuple(
                dict.fromkeys((*story.detail_support_ids, *story.support_ids))
            )
            for support_id in _story_support_ids(story.story_id, planned_support_ids, context):
                if support_id not in support_ids:
                    support_ids.append(support_id)

        bundles.append(
            ArticleCompositionBundle(
                bundle_id=f"bundle:{bundle_index}:{section_id}:{theme_key}",
                section_id=section_id,
                theme_key=theme_key,
                lead_story_id=lead.story_id,
                story_ids=story_ids,
                support_ids=tuple(support_ids),
                prominence=lead.prominence,
            )
        )

    # Keep this invariant explicit: projection suppression may hide a Story
    # from the writer, but it must never mutate the coverage plan itself.
    visible_ids = [story_id for bundle in bundles for story_id in bundle.story_ids]
    expected_ids = [story_id for story_id in coverage_plan.story_ids if story_id not in suppressed]
    if len(visible_ids) != len(set(visible_ids)) or set(visible_ids) != set(expected_ids):
        raise ValueError(
            "article composition did not preserve one bundle membership per visible Story: "
            f"{visible_ids!r} != {expected_ids!r}"
        )
    if not set(suppressed).issubset(plan_story_ids):
        raise ValueError("article composition suppression contains an unknown Story")

    return ArticleCompositionPlan(
        bundles=tuple(bundles),
        suppressed_story_ids=suppressed,
    )
