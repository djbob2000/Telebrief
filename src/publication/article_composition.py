"""Presentation-level composition based on explicitly supported relations."""

from __future__ import annotations

import datetime as dt
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

ArticleCompositionRelation = Literal[
    "shared_condition",
    "localized_contrast",
    "temporal_progression",
    "practical_consequence",
    "independent",
]
# Kept as a source-compatibility alias; the public shared name is ArticleCompositionRelation.
CompositionRelation = ArticleCompositionRelation

_STORY_ID_RE = re.compile(r"story:(?:[^:]+|\d+)")
_SERVICE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("power", ("электр", "свет", "напряжен", "вольт", "энерг")),
    ("water", ("вод", "водоканал", "водовод", "насос")),
    ("internet", ("интернет", "связ", "провайдер", "роутер")),
    ("transport", ("автобус", "маршрут", "транспорт", "рейс")),
)
_SERVICE_HEADINGS = {
    "power": "Электроснабжение",
    "water": "Водоснабжение",
    "internet": "Связь и интернет",
    "transport": "Транспорт",
}


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
class ArticleCompositionPlan:
    """Narrative roadmap and relation groups over frozen coverage."""

    narrative_lines: tuple[ArticleNarrativeLine, ...]
    groups: tuple[ArticleCompositionGroup, ...]
    suppressed_story_ids: tuple[str, ...] = ()

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
    text = story.topic.casefold()
    matches = [
        key for key, markers in _SERVICE_MARKERS if any(marker in text for marker in markers)
    ]
    if matches:
        return matches[0] if len(matches) == 1 else None
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
) -> tuple[tuple[dt.datetime | None, dt.datetime | None], ...]:
    intervals = []
    for support_id in story.support_ids:
        support = context.support_by_id.get(support_id)
        if support is not None and (
            support.effective_from is not None or support.effective_until is not None
        ):
            intervals.append((support.effective_from, support.effective_until))
    return tuple(intervals)


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


def _explicit_practical_consequence(
    left: ArticleStoryCoverage,
    right: ArticleStoryCoverage,
    context: ArticleEditorialContext,
    resolver: CityContextResolver | None,
) -> bool:
    """Require one support to explicitly connect both service domains and a place."""
    left_service = _service_key(left, context)
    right_service = _service_key(right, context)
    if not left_service or not right_service or left_service == right_service:
        return False
    left_places = set(_place_keys(left, context, resolver))
    right_places = set(_place_keys(right, context, resolver))
    if not left_places or not right_places or left_places != right_places:
        return False

    service_markers = dict(_SERVICE_MARKERS)
    bridge_pattern = re.compile(
        r"\b(?:поэтому|так\s+что|из-за\s+чего|в\s+результате\s+чего|"
        r"привело\s+к\s+тому,\s+что|для\s+того,\s+чтобы|чтобы)\b"
    )
    support_ids = (*left.support_ids, *right.support_ids)
    for support_id in support_ids:
        support = context.support_by_id.get(support_id)
        if support is None:
            continue
        for source_text in (support.text, support.source_text):
            for sentence in re.split(r"(?<=[.!?])\s+", source_text.casefold()):
                for connective in bridge_pattern.finditer(sentence):
                    before = sentence[: connective.start()]
                    after = sentence[connective.end() :]
                    left_before = any(marker in before for marker in service_markers[left_service])
                    left_after = any(marker in after for marker in service_markers[left_service])
                    right_before = any(
                        marker in before for marker in service_markers[right_service]
                    )
                    right_after = any(marker in after for marker in service_markers[right_service])
                    if (left_before and right_after) or (right_before and left_after):
                        return True
    return False


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
    if not left_service or not right_service:
        return "independent"

    left_places = _place_keys(left, context, resolver)
    right_places = _place_keys(right, context, resolver)
    same_service = left_service == right_service
    same_place = bool(left_places and right_places and set(left_places) == set(right_places))
    if (
        same_service
        and left_state
        and right_state
        and left_places
        and right_places
        and set(left_places).isdisjoint(right_places)
        and left_state != right_state
    ):
        return "localized_contrast"

    left_intervals = _effective_intervals(left, context)
    right_intervals = _effective_intervals(right, context)
    if (
        same_service
        and same_place
        and left_state
        and right_state
        and left_state != right_state
        and _strictly_ordered_non_overlapping(left_intervals, right_intervals)
    ):
        return "temporal_progression"
    if _explicit_practical_consequence(left, right, context, resolver):
        return "practical_consequence"
    if same_service and same_place and left_state and left_state == right_state:
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

    grouped: list[tuple[list[ArticleStoryCoverage], ArticleCompositionRelation]] = []
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
            _relation(left, right, context, resolver)
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
                support_ids=_member_support_ids(story, context),
            )
            for story in ordered
        )
        service = _service_key(ordered[0], context) or "independent"
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
        if section is not None:
            line_id = f"line:section:{section.section_id}"
            heading_hint = section.title
            narrative_intent = section.narrative_intent
        else:
            line_id = f"line:{group_index}:{group_relation}:{service}"
            heading_hint = (
                _SERVICE_HEADINGS.get(service, service.replace("_", " ").title())
                if group_relation != "independent"
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
