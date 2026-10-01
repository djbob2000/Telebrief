"""Presentation-level composition based on explicitly supported relations."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
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

ArticleCompositionRelation = Literal[
    "shared_condition",
    "localized_contrast",
    "temporal_progression",
    "practical_consequence",
    "independent",
]
# Kept as a source-compatibility alias; the public shared name is ArticleCompositionRelation.
CompositionRelation = ArticleCompositionRelation
ARTICLE_COMPOSITION_VERSION = "v2"

_STORY_ID_RE = re.compile(r"story:(?:[^:]+|\d+)")
_POWER_GRID_OFFICE_RE = re.compile(r"\bрэс(?:а|у|ом|е|ах)?\b", re.IGNORECASE)
_SERVICE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("power", ("электр", "электросет", "свет", "напряжен", "вольт", "энерг")),
    ("water", ("вод", "водоканал", "водовод", "насос")),
    ("internet", ("интернет", "связ", "провайдер", "роутер", "оптоволокн")),
    ("transport", ("автобус", "маршрут", "транспорт", "рейс")),
)
_SERVICE_HEADINGS = {
    "power": "Электроснабжение",
    "water": "Водоснабжение",
    "internet": "Связь и интернет",
    "transport": "Транспорт",
}
_SERVICE_SECTION_IDS = {
    "power": frozenset({"infrastructure"}),
    "water": frozenset({"infrastructure"}),
    "internet": frozenset({"communications"}),
    "transport": frozenset({"mobility"}),
}
_UNCLASSIFIED_LINE_INTENT = (
    "These supported Stories have no reliable shared theme in the available evidence. "
    "Keep their facts distinct; do not place them under a specific service heading or "
    "imply a connection between them."
)
_MULTI_GROUP_LINE_INTENT = (
    "Develop the shared thematic chapter at its assigned depth; use natural transitions "
    "without implying a shared event, cause, place, or timeline. Keep independent groups "
    "distinct and synthesize only within groups whose relation is supported."
)
_PRACTICAL_BRIDGE_PATTERN = re.compile(
    r"\b(?:поэтому|так\s+что|из-за\s+чего|в\s+результате\s+чего|"
    r"привело\s+к\s+тому,\s+что|для\s+того,\s+чтобы|чтобы)\b"
)


@dataclass(frozen=True)
class ArticleCompositionMember:
    story_id: str
    prominence: ArticleProminence
    support_ids: tuple[str, ...]


@dataclass(frozen=True)
class ArticleCompositionGroup:
    """One relation-bound evidence group with independently traceable members."""

    group_id: str
    narrative_line_id: str
    relation: ArticleCompositionRelation
    lead_story_id: str
    members: tuple[ArticleCompositionMember, ...]
    theme_key: str = "independent"

    @property
    def story_ids(self) -> tuple[str, ...]:
        return tuple(member.story_id for member in self.members)

    # Compatibility for the current writer-context renderer; Task 2 migrates it.
    @property
    def bundle_id(self) -> str:
        return self.group_id

    @property
    def section_id(self) -> str:
        return ""

    @property
    def prominence(self) -> ArticleProminence:
        lead = next((m for m in self.members if m.story_id == self.lead_story_id), None)
        return lead.prominence if lead is not None else "BRIEF"


@dataclass(frozen=True)
class ArticleNarrativeLine:
    line_id: str
    heading_hint: str | None
    narrative_intent: str
    prominence: ArticleProminence
    group_ids: tuple[str, ...]


@dataclass
class _NarrativeLineDraft:
    heading_hint: str | None
    narrative_intent: str
    prominence: ArticleProminence
    group_ids: list[str]


@dataclass(frozen=True)
class _StoryRelationFeatures:
    """Relation inputs resolved once per Story for composition comparisons."""

    service: str | None
    state: str | None
    places: frozenset[str]
    areas: frozenset[str]
    intervals: tuple[tuple[dt.datetime | None, dt.datetime | None], ...]
    practical_service_pairs: frozenset[frozenset[str]]


@dataclass(frozen=True)
class ArticleCompositionPlan:
    """Narrative roadmap and relation groups over frozen coverage."""

    narrative_lines: tuple[ArticleNarrativeLine, ...]
    groups: tuple[ArticleCompositionGroup, ...]
    suppressed_story_ids: tuple[str, ...] = ()

    @property
    def version(self) -> str:
        return ARTICLE_COMPOSITION_VERSION

    @property
    def group_by_story_id(self) -> dict[str, ArticleCompositionGroup]:
        return {story_id: group for group in self.groups for story_id in group.story_ids}

    # Compatibility for the current writer-context renderer; Task 2 migrates it.
    @property
    def bundles(self) -> tuple[ArticleCompositionGroup, ...]:
        return self.groups

    @property
    def bundle_by_story_id(self) -> dict[str, ArticleCompositionGroup]:
        return self.group_by_story_id

    def to_metadata(self) -> dict[str, object]:
        return {
            "version": self.version,
            "order_is_advisory": True,
            "line_count": len(self.narrative_lines),
            "group_count": len(self.groups),
            "suppressed_story_ids": list(self.suppressed_story_ids),
            "narrative_lines": [
                {
                    "line_id": line.line_id,
                    "heading_hint": line.heading_hint,
                    "narrative_intent": line.narrative_intent,
                    "prominence": line.prominence,
                    "group_ids": list(line.group_ids),
                }
                for line in self.narrative_lines
            ],
            "groups": [
                {
                    "group_id": group.group_id,
                    "narrative_line_id": group.narrative_line_id,
                    "relation": group.relation,
                    "lead_story_id": group.lead_story_id,
                    "theme_key": group.theme_key,
                    "members": [
                        {
                            "story_id": member.story_id,
                            "prominence": member.prominence,
                            "support_ids": list(member.support_ids),
                        }
                        for member in group.members
                    ],
                }
                for group in self.groups
            ],
        }


@dataclass(frozen=True)
class ArticleCompositionRichnessSummary:
    """Count-only description of the planned article's thematic/detail breadth."""

    thematic_line_count: int
    develop_line_count: int
    detail_anchor_count: int


def _normalized_projected_support_text(text: str) -> str:
    """Normalize surface punctuation/spacing while preserving word order."""
    return " ".join(re.findall(r"\w+", text.casefold().replace("ё", "е")))


def build_article_composition_richness_summary(
    coverage_plan: ArticleCoveragePlan,
    composition_plan: ArticleCompositionPlan,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> ArticleCompositionRichnessSummary:
    """Summarize known themes, DEVELOP lines, and distinct projected detail anchors."""
    from src.publication.article_context import article_support_theme_hints

    composition_support_ids = {
        support_id
        for group in composition_plan.groups
        for member in group.members
        for support_id in member.support_ids
    }
    planned_support_ids = {
        support_id
        for story in coverage_plan.stories
        for support_id in (*story.support_ids, *story.detail_support_ids)
        if support_id in composition_support_ids
    }

    def projected_support(support_id: str) -> ArticleSupport | None:
        support = context.support_by_id.get(support_id)
        projected_text = material_projection.text_by_support_id.get(support_id, "").strip()
        if (
            support is None
            or support.publication_use != "PUBLISH"
            or support.evidence_kind == "resident_question"
            or material_projection.actions_by_support_id.get(support_id)
            not in {"KEEP", "TRIM_DIRECTORY"}
            or not projected_text
        ):
            return None
        return replace(support, text=projected_text, source_text="")

    themes: set[str] = set()
    for support_id in planned_support_ids:
        support = projected_support(support_id)
        if support is not None:
            themes.update(article_support_theme_hints(support))

    group_by_id = {group.group_id: group for group in composition_plan.groups}
    develop_line_count = sum(
        1
        for line in composition_plan.narrative_lines
        if any(
            member.prominence == "DEVELOP"
            for group_id in line.group_ids
            if (group := group_by_id.get(group_id)) is not None
            for member in group.members
        )
    )

    detail_anchor_texts: set[str] = set()
    for story in coverage_plan.stories:
        for support_id in story.detail_support_ids:
            if support_id not in composition_support_ids:
                continue
            support = projected_support(support_id)
            if support is None:
                continue
            normalized_text = _normalized_projected_support_text(support.text)
            if normalized_text:
                detail_anchor_texts.add(normalized_text)

    return ArticleCompositionRichnessSummary(
        thematic_line_count=len(themes),
        develop_line_count=develop_line_count,
        detail_anchor_count=len(detail_anchor_texts),
    )


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
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> tuple[str, ...]:
    planned = tuple(dict.fromkeys((*story.detail_support_ids, *story.support_ids)))
    return tuple(
        sid
        for sid in planned
        if _support_belongs_to_story(sid, story.story_id, context)
        and (support := context.support_by_id.get(sid)) is not None
        and support.publication_use == "PUBLISH"
        and support.evidence_kind != "resident_question"
        and material_projection.actions_by_support_id.get(sid) in {"KEEP", "TRIM_DIRECTORY"}
        and material_projection.text_by_support_id.get(sid, "").strip()
    )


def _projected_supports(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> tuple[tuple[str, ArticleSupport], ...]:
    supports: list[tuple[str, ArticleSupport]] = []
    for support_id in dict.fromkeys((*story.detail_support_ids, *story.support_ids)):
        support = context.support_by_id.get(support_id)
        if (
            support is None
            or support.publication_use != "PUBLISH"
            or support.evidence_kind == "resident_question"
            or material_projection.actions_by_support_id.get(support_id)
            not in {"KEEP", "TRIM_DIRECTORY"}
            or not material_projection.text_by_support_id.get(support_id, "").strip()
        ):
            continue
        supports.append((support_id, support))
    return tuple(supports)


def _projected_support_text(
    support_id: str,
    material_projection: ArticleMaterialProjection,
) -> str:
    return material_projection.text_by_support_id.get(support_id, "").strip()


def _all_service_text(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> str:
    return " ".join(
        _projected_support_text(support_id, material_projection)
        for support_id, _support in _projected_supports(story, context, material_projection)
    ).casefold()


def _service_key(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> str | None:
    text = _all_service_text(story, context, material_projection)
    matches = {
        key for key, markers in _SERVICE_MARKERS if any(marker in text for marker in markers)
    }
    if _POWER_GRID_OFFICE_RE.search(text):
        matches.add("power")
    return next(iter(matches)) if len(matches) == 1 else None


def _state_key(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> str | None:
    texts = [
        _projected_support_text(support_id, material_projection).casefold()
        for support_id, _support in _projected_supports(story, context, material_projection)
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
    states: set[str] = set()
    for text in texts:
        negated_availability = re.search(r"\bне\s+(?:работает|пода[её]тся|поступает)\b", text)
        if negated_availability:
            states.add("unavailable")
        for state, state_markers in markers:
            for marker in state_markers:
                for match in re.finditer(re.escape(marker), text):
                    prefix = text[max(0, match.start() - 48) : match.start()]
                    has_local_negation = re.search(r"\bне(?:\s+\w+){0,2}\s*$", prefix)
                    if has_local_negation:
                        continue
                    states.add(state)
    return next(iter(states)) if len(states) == 1 else None


def _place_keys(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    resolver: CityContextResolver | None,
    material_projection: ArticleMaterialProjection,
) -> tuple[str, ...]:
    texts = [
        _projected_support_text(support_id, material_projection)
        for support_id, _support in _projected_supports(story, context, material_projection)
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


def _area_keys(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    resolver: CityContextResolver | None,
    material_projection: ArticleMaterialProjection,
) -> frozenset[str]:
    if resolver is None:
        return frozenset()

    from src.publication.article_geography import resolve_article_place_area_map

    areas = {
        area_id
        for support_id, _support in _projected_supports(story, context, material_projection)
        for area_ids in resolve_article_place_area_map(
            _projected_support_text(support_id, material_projection), resolver
        ).values()
        for area_id in area_ids
    }
    # A Story naming multiple verified areas is too broad for a same-district
    # relation. Keep it ungrouped rather than choosing one area arbitrarily.
    return frozenset(areas) if len(areas) == 1 else frozenset()


def _effective_intervals(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> tuple[tuple[dt.datetime | None, dt.datetime | None], ...]:
    intervals = []
    for _support_id, support in _projected_supports(story, context, material_projection):
        if support.effective_from is not None or support.effective_until is not None:
            intervals.append((support.effective_from, support.effective_until))
    return tuple(intervals)


def _practical_service_pairs(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> frozenset[frozenset[str]]:
    """Index service pairs explicitly linked across a connective in one support."""
    pairs: set[frozenset[str]] = set()
    for support_id, _support in _projected_supports(story, context, material_projection):
        projected = _projected_support_text(support_id, material_projection).casefold()
        for sentence in re.split(r"(?<=[.!?])\s+", projected):
            for connective in _PRACTICAL_BRIDGE_PATTERN.finditer(sentence):
                before = sentence[: connective.start()]
                after = sentence[connective.end() :]
                before_services = {
                    service
                    for service, markers in _SERVICE_MARKERS
                    if any(marker in before for marker in markers)
                }
                after_services = {
                    service
                    for service, markers in _SERVICE_MARKERS
                    if any(marker in after for marker in markers)
                }
                pairs.update(
                    frozenset((left, right))
                    for left in before_services
                    for right in after_services
                    if left != right
                )
    return frozenset(pairs)


def _relation_features(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    resolver: CityContextResolver | None,
    material_projection: ArticleMaterialProjection,
) -> _StoryRelationFeatures:
    service = _service_key(story, context, material_projection)
    if not service:
        return _StoryRelationFeatures(
            service=None,
            state=None,
            places=frozenset(),
            areas=frozenset(),
            intervals=(),
            practical_service_pairs=frozenset(),
        )
    return _StoryRelationFeatures(
        service=service,
        state=_state_key(story, context, material_projection),
        places=frozenset(_place_keys(story, context, resolver, material_projection)),
        areas=_area_keys(story, context, resolver, material_projection),
        intervals=_effective_intervals(story, context, material_projection),
        practical_service_pairs=_practical_service_pairs(story, context, material_projection),
    )


def _strictly_ordered_non_overlapping(
    left: tuple[tuple[dt.datetime | None, dt.datetime | None], ...],
    right: tuple[tuple[dt.datetime | None, dt.datetime | None], ...],
) -> bool:
    """Require complete intervals and strict ordering across the two reports."""
    if not left or not right:
        return False
    if any(start is None or end is None for start, end in (*left, *right)):
        return False
    left_complete = [(start, end) for start, end in left if start is not None and end is not None]
    right_complete = [(start, end) for start, end in right if start is not None and end is not None]
    try:
        if any(start >= end for start, end in (*left_complete, *right_complete)):
            return False
        left_before_right = max(end for _, end in left_complete) < min(
            start for start, _ in right_complete
        )
        right_before_left = max(end for _, end in right_complete) < min(
            start for start, _ in left_complete
        )
    except TypeError:
        return False
    return left_before_right or right_before_left


def _relation_from_features(
    left: _StoryRelationFeatures,
    right: _StoryRelationFeatures,
) -> CompositionRelation:
    left_service = left.service
    right_service = right.service
    left_state = left.state
    right_state = right.state
    if not left_service or not right_service:
        return "independent"

    left_places = left.places
    right_places = right.places
    same_service = left_service == right_service
    same_place = bool(left_places and right_places and left_places == right_places)
    if (
        same_service
        and left_state
        and right_state
        and left_places
        and right_places
        and left_places.isdisjoint(right_places)
        and left.areas
        and right.areas
        and left.areas == right.areas
        and left_state != right_state
    ):
        return "localized_contrast"

    if (
        same_service
        and same_place
        and left_state
        and right_state
        and left_state != right_state
        and _strictly_ordered_non_overlapping(left.intervals, right.intervals)
    ):
        return "temporal_progression"
    if (
        left_places
        and right_places
        and same_place
        and frozenset((left_service, right_service))
        in left.practical_service_pairs | right.practical_service_pairs
    ):
        return "practical_consequence"
    if same_service and same_place and left_state and left_state == right_state:
        return "shared_condition"
    return "independent"


def _place_resolver(context: ArticleEditorialContext) -> CityContextResolver | None:
    from src.publication.article_geography import resolve_article_place_resolver

    return resolve_article_place_resolver(context)


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
    relation_features = {
        story.story_id: _relation_features(story, context, resolver, material_projection)
        for story in visible
    }
    relation_cache: dict[tuple[str, str], CompositionRelation] = {}

    def relation_for_pair(
        left: ArticleStoryCoverage, right: ArticleStoryCoverage
    ) -> CompositionRelation:
        pair_key = (min(left.story_id, right.story_id), max(left.story_id, right.story_id))
        cached = relation_cache.get(pair_key)
        if cached is None:
            cached = _relation_from_features(
                relation_features[left.story_id], relation_features[right.story_id]
            )
            relation_cache[pair_key] = cached
        return cached

    grouped: list[tuple[list[ArticleStoryCoverage], ArticleCompositionRelation]] = []
    for story in visible:
        placed = False
        for group_index, (members, current_relation) in enumerate(grouped):
            candidate_relations = {relation_for_pair(story, member) for member in members}
            if len(candidate_relations) == 1 and "independent" not in candidate_relations:
                relation = next(iter(candidate_relations))
                if current_relation in {"independent", relation}:
                    members.append(story)
                    grouped[group_index] = (members, relation)
                    placed = True
                    break
        if not placed:
            grouped.append(([story], "independent"))

    relation_intents = {
        "localized_contrast": "Preserve the supported difference between local service states.",
        "temporal_progression": "Describe the supported change across effective periods.",
        "practical_consequence": "Connect the explicitly reported service detail and its practical effect.",
        "shared_condition": "Synthesize reports that support the same service condition.",
        "independent": (
            "This item has no supported cross-story relation; place it compactly unless "
            "the evidence provides a natural connection."
        ),
    }
    depth_order = {"BRIEF": 0, "WEAVE": 1, "DEVELOP": 2}
    section_by_story = {
        assignment.story_id: section
        for section in coverage_plan.sections
        for assignment in section.story_assignments
    }
    section_by_id = coverage_plan.by_section_id
    line_records: dict[str, _NarrativeLineDraft] = {}
    result: list[ArticleCompositionGroup] = []
    for group_index, (stories, _) in enumerate(grouped, start=1):
        ordered = sorted(stories, key=lambda item: (item.rank, item.story_id))
        relations = {
            relation_for_pair(left, right)
            for index, left in enumerate(ordered)
            for right in ordered[index + 1 :]
        }
        relations.discard("independent")
        group_relation: ArticleCompositionRelation = (
            next(iter(relations)) if len(relations) == 1 else "independent"
        )
        if len(ordered) == 1:
            group_relation = "independent"
        composition_members = tuple(
            ArticleCompositionMember(
                story_id=story.story_id,
                prominence=story.prominence,
                support_ids=_member_support_ids(story, context, material_projection),
            )
            for story in ordered
        )
        member_services = {relation_features[story.story_id].service for story in ordered}
        service = relation_features[ordered[0].story_id].service or "independent"
        lead = ordered[0]
        group_id = f"group:{group_index}:{group_relation}:{service}"
        prominence = max(
            (member.prominence for member in composition_members),
            key=lambda value: depth_order[value],
        )
        member_sections = {
            section.section_id if section and section.section_id != "city_life" else None
            for story in ordered
            if (section := section_by_story.get(story.story_id)) is not None
        }
        all_members_have_same_section = (
            len(member_sections) == 1
            and None not in member_sections
            and all(story.story_id in section_by_story for story in ordered)
        )
        member_section_id = next(iter(member_sections), None)
        section = (
            section_by_id.get(member_section_id)
            if all_members_have_same_section and member_section_id is not None
            else None
        )
        line_id: str
        heading_hint: str | None
        narrative_intent: str
        compatible_service_section = (
            section is not None
            and service != "independent"
            and member_services == {service}
            and section.section_id in _SERVICE_SECTION_IDS.get(service, frozenset())
        )
        if compatible_service_section and section is not None:
            line_id = f"line:section:{section.section_id}"
            heading_hint = section.title
            narrative_intent = section.narrative_intent
        elif service != "independent" and member_services == {service}:
            # The card's rubric is a useful broad fallback, but it can disagree
            # with the service actually named by the citable source material.
            # Keep such Stories in a compatible service lane instead of
            # inheriting an unrelated chapter heading.
            line_id = f"line:service:{service}"
            heading_hint = _SERVICE_HEADINGS.get(service)
            narrative_intent = relation_intents[group_relation]
        elif service == "independent":
            # Unknown/ambiguous theme is still publishable material. Preserve it
            # under a neutral roadmap line so coarse card tags cannot imply a
            # specific service topic that the evidence does not establish.
            neutral_section_id = section.section_id if section is not None else "city_life"
            line_id = f"line:unclassified:{neutral_section_id}"
            heading_hint = None
            narrative_intent = _UNCLASSIFIED_LINE_INTENT
        elif section is not None:
            # A supported cross-service relation may include multiple service
            # types. Retain the broad source section only when the relation
            # group itself has a compatible service lane; otherwise keep its
            # relation intent without a misleading single-service heading.
            line_id = f"line:{group_index}:{group_relation}:{service}"
            heading_hint = None
            narrative_intent = relation_intents[group_relation]
        else:
            line_id = f"line:{group_index}:{group_relation}:{service}"
            heading_hint = (
                _SERVICE_HEADINGS.get(service, service.replace("_", " ").title())
                if group_relation != "independent" and member_services == {service}
                else None
            )
            narrative_intent = relation_intents[group_relation]

        line_record = line_records.get(line_id)
        if line_record is None:
            line_records[line_id] = _NarrativeLineDraft(
                heading_hint=heading_hint,
                narrative_intent=narrative_intent,
                prominence=prominence,
                group_ids=[group_id],
            )
        else:
            line_record.group_ids.append(group_id)
            if line_record.heading_hint is not None:
                line_record.narrative_intent = _MULTI_GROUP_LINE_INTENT
            if depth_order[prominence] > depth_order[line_record.prominence]:
                line_record.prominence = prominence

        result.append(
            ArticleCompositionGroup(
                group_id=group_id,
                narrative_line_id=line_id,
                relation=group_relation,
                lead_story_id=lead.story_id,
                members=composition_members,
                theme_key=service,
            )
        )

    lines = [
        ArticleNarrativeLine(
            line_id=line_id,
            heading_hint=record.heading_hint,
            narrative_intent=record.narrative_intent,
            prominence=record.prominence,
            group_ids=tuple(record.group_ids),
        )
        for line_id, record in line_records.items()
    ]

    expected_ids = {story.story_id for story in visible}
    member_ids = [story_id for bundle in result for story_id in bundle.story_ids]
    if len(member_ids) != len(set(member_ids)) or set(member_ids) != expected_ids:
        raise ValueError(
            "article composition must preserve exactly one membership per visible Story"
        )
    if not set(suppressed).issubset(set(coverage_plan.story_ids)):
        raise ValueError("article composition suppression contains an unknown Story")
    return ArticleCompositionPlan(
        narrative_lines=tuple(lines),
        groups=tuple(result),
        suppressed_story_ids=suppressed,
    )
