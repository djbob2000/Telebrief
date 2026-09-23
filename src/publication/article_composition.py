"""Presentation-level composition based on explicitly supported relations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from src.city_context import CityContextResolver
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleProminence,
    ArticleStoryCoverage,
)

if TYPE_CHECKING:
    from src.publication.article_context import ArticleEditorialContext, ArticleSupport
    from src.publication.article_material import ArticleMaterialProjection

CompositionRelation = Literal[
    "localized_contrast",
    "temporal_progression",
    "practical_consequence",
    "shared_condition",
    "independent",
]

_STORY_ID_RE = re.compile(r"story:(?:[^:]+|\d+)")
_SERVICE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("power", ("электр", "свет", "напряжен", "вольт", "энерг")),
    ("water", ("вод", "водоканал", "водовод", "насос")),
    ("internet", ("интернет", "связ", "провайдер", "роутер")),
    ("transport", ("автобус", "маршрут", "транспорт", "рейс")),
)


@dataclass(frozen=True)
class ArticleCompositionMember:
    story_id: str
    prominence: ArticleProminence
    support_ids: tuple[str, ...]


@dataclass(frozen=True)
class ArticleCompositionBundle:
    """One relation-bound reader-facing group; members keep independent evidence."""

    bundle_id: str
    section_id: str
    theme_key: str
    lead_story_id: str
    members: tuple[ArticleCompositionMember, ...]
    prominence: ArticleProminence
    relation: CompositionRelation = "independent"
    heading_hint: str | None = None

    @property
    def story_ids(self) -> tuple[str, ...]:
        return tuple(member.story_id for member in self.members)


@dataclass(frozen=True)
class ArticleCompositionPlan:
    """Composition groups and projection-only Stories omitted from the writer view."""

    bundles: tuple[ArticleCompositionBundle, ...]
    suppressed_story_ids: tuple[str, ...] = ()

    @property
    def bundle_by_story_id(self) -> dict[str, ArticleCompositionBundle]:
        return {story_id: bundle for bundle in self.bundles for story_id in bundle.story_ids}

    def to_metadata(self) -> dict[str, object]:
        return {
            "line_count": len(self.bundles),
            "group_count": len(self.bundles),
            "bundle_count": len(self.bundles),
            "suppressed_story_ids": list(self.suppressed_story_ids),
            "groups": [
                {
                    "bundle_id": bundle.bundle_id,
                    "section_id": bundle.section_id,
                    "theme_key": bundle.theme_key,
                    "lead_story_id": bundle.lead_story_id,
                    "relation": bundle.relation,
                    "heading_hint": bundle.heading_hint,
                    "prominence": bundle.prominence,
                    "members": [
                        {
                            "story_id": member.story_id,
                            "prominence": member.prominence,
                            "support_ids": list(member.support_ids),
                        }
                        for member in bundle.members
                    ],
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


def _member_support_ids(
    story: ArticleStoryCoverage, context: ArticleEditorialContext
) -> tuple[str, ...]:
    planned = tuple(dict.fromkeys((*story.detail_support_ids, *story.support_ids)))
    return tuple(sid for sid in planned if _support_belongs_to_story(sid, story.story_id, context))


def _all_service_text(story: ArticleStoryCoverage, context: ArticleEditorialContext) -> str:
    texts = [story.topic]
    for support_id in story.support_ids:
        support = context.support_by_id.get(support_id)
        if support is not None:
            texts.extend((support.text, support.source_text))
    return " ".join(texts).casefold()


def _service_key(story: ArticleStoryCoverage, context: ArticleEditorialContext) -> str | None:
    text = _all_service_text(story, context)
    matches = [
        key for key, markers in _SERVICE_MARKERS if any(marker in text for marker in markers)
    ]
    return matches[0] if len(matches) == 1 else None


def _state_key(story: ArticleStoryCoverage, context: ArticleEditorialContext) -> str | None:
    texts = [
        _support_text(context.support_by_id[sid]).casefold()
        for sid in story.support_ids
        if sid in context.support_by_id
    ]
    markers: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "unavailable",
            (
                "нет света",
                "света нет",
                "нет воды",
                "воды нет",
                "нет электричества",
                "отключ",
                "не подаётся",
                "не подается",
            ),
        ),
        ("degraded", ("пониженн", "ограничен", "с перебоями", "нестабильн")),
        (
            "available",
            (
                "восстанов",
                "подаётся",
                "подается",
                "подавалась",
                "работает",
                "есть вода",
                "есть свет",
            ),
        ),
    )
    states = {
        state
        for text in texts
        for state, state_markers in markers
        if any(marker in text for marker in state_markers)
    }
    return next(iter(states)) if len(states) == 1 else None


def _support_text(support: ArticleSupport) -> str:
    return " ".join(part for part in (support.text, support.source_text) if part)


def _place_keys(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    resolver: CityContextResolver | None,
) -> tuple[str, ...]:
    texts = [
        _support_text(context.support_by_id[sid])
        for sid in story.support_ids
        if sid in context.support_by_id
    ]
    if resolver is not None:
        places = {
            entity.entity_id
            for text in texts
            for entity in resolver.resolve(text).entities
            if entity.kind == "place" and entity.confidence == "high"
        }
        if places:
            return tuple(sorted(places))
    names: set[str] = set()
    for text in texts:
        for match in re.finditer(r"(?:улиц[аыеу]|ул\.)\s+([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z-]+)", text):
            names.add(match.group(1).casefold())
    return tuple(sorted(names))


def _effective_intervals(
    story: ArticleStoryCoverage, context: ArticleEditorialContext
) -> tuple[tuple[object | None, object | None], ...]:
    intervals = []
    for support_id in story.support_ids:
        support = context.support_by_id.get(support_id)
        if support is not None and (
            support.effective_from is not None or support.effective_until is not None
        ):
            intervals.append((support.effective_from, support.effective_until))
    return tuple(intervals)


def _relation(
    left: ArticleStoryCoverage,
    right: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    resolver: CityContextResolver | None,
) -> CompositionRelation:
    left_service = _service_key(left, context)
    right_service = _service_key(right, context)
    left_state = _state_key(left, context)
    right_state = _state_key(right, context)
    if not left_service or left_service != right_service or not left_state or not right_state:
        return "independent"

    left_places = _place_keys(left, context, resolver)
    right_places = _place_keys(right, context, resolver)
    if (
        left_places
        and right_places
        and set(left_places).isdisjoint(right_places)
        and left_state != right_state
    ):
        return "localized_contrast"

    left_intervals = _effective_intervals(left, context)
    right_intervals = _effective_intervals(right, context)
    same_place = bool(left_places and right_places and set(left_places) == set(right_places))
    if (
        same_place
        and left_state != right_state
        and left_intervals
        and right_intervals
        and set(left_intervals).isdisjoint(right_intervals)
    ):
        return "temporal_progression"
    if same_place and left_state == right_state:
        return "shared_condition"
    return "independent"


def _place_resolver(context: ArticleEditorialContext) -> CityContextResolver | None:
    slug = (context.edition_slug or "").strip()
    profile_path = Path("data/city_profiles") / f"{slug}.yaml" if slug else None
    if profile_path is None or not profile_path.is_file():
        return None
    return CityContextResolver.from_yaml(profile_path)


def build_article_composition_plan(
    coverage_plan: ArticleCoveragePlan,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> ArticleCompositionPlan:
    """Build supported relation groups without changing coverage or evidence ownership."""
    suppressed_set = set(material_projection.suppressed_story_ids)
    suppressed = tuple(
        story.story_id for story in coverage_plan.stories if story.story_id in suppressed_set
    )
    visible = [story for story in coverage_plan.stories if story.story_id not in suppressed_set]
    resolver = _place_resolver(context)

    grouped: list[tuple[list[ArticleStoryCoverage], CompositionRelation]] = []
    for story in visible:
        placed = False
        for group_index, (members, current_relation) in enumerate(grouped):
            candidate_relations = {
                _relation(story, member, context, resolver) for member in members
            }
            if len(candidate_relations) == 1 and "independent" not in candidate_relations:
                relation = next(iter(candidate_relations))
                if current_relation in {"independent", relation}:
                    members.append(story)
                    grouped[group_index] = (members, relation)
                    placed = True
                    break
        if not placed:
            grouped.append(([story], "independent"))

    # Resolve relation labels after pair grouping; singletons remain independent.
    result: list[ArticleCompositionBundle] = []
    for bundle_index, (stories, _) in enumerate(grouped, start=1):
        ordered = sorted(stories, key=lambda item: (item.rank, item.story_id))
        relations = {
            _relation(left, right, context, resolver)
            for index, left in enumerate(ordered)
            for right in ordered[index + 1 :]
        }
        relations.discard("independent")
        group_relation: CompositionRelation = (
            next(iter(relations)) if len(relations) == 1 else "independent"
        )
        if len(ordered) == 1:
            group_relation = "independent"
        composition_members = tuple(
            ArticleCompositionMember(
                story_id=story.story_id,
                prominence=story.prominence,
                support_ids=_member_support_ids(story, context),
            )
            for story in ordered
        )
        section = coverage_plan.section_for_story(ordered[0].story_id)
        section_id = section.section_id if section is not None else ""
        service = _service_key(ordered[0], context) or "independent"
        lead = ordered[0]
        result.append(
            ArticleCompositionBundle(
                bundle_id=f"group:{bundle_index}:{group_relation}:{service}",
                section_id=section_id,
                theme_key=service,
                lead_story_id=lead.story_id,
                members=composition_members,
                prominence=lead.prominence,
                relation=group_relation,
                heading_hint=None if group_relation == "independent" else service,
            )
        )

    expected_ids = {story.story_id for story in visible}
    member_ids = [story_id for bundle in result for story_id in bundle.story_ids]
    if len(member_ids) != len(set(member_ids)) or set(member_ids) != expected_ids:
        raise ValueError(
            "article composition must preserve exactly one membership per visible Story"
        )
    if not set(suppressed).issubset(set(coverage_plan.story_ids)):
        raise ValueError("article composition suppression contains an unknown Story")
    return ArticleCompositionPlan(bundles=tuple(result), suppressed_story_ids=suppressed)
