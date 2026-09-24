"""Validated editorial roadmap for a city-life long-read article."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, NoReturn, cast

from src.publication.article_context import ArticleEditorialContext
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_material import ArticleMaterialProjection
from src.publication.errors import PublicationGenerationError

ArticleDepth = Literal["DEVELOP", "WEAVE", "BRIEF", "OMIT"]
ArticleOmissionReason = Literal["directory_only", "no_citable_material"]
ArticleBriefRelation = Literal[
    "shared_condition",
    "localized_contrast",
    "temporal_progression",
    "practical_consequence",
    "independent",
]

_DEPTHS = {"DEVELOP", "WEAVE", "BRIEF"}
_OMISSION_REASONS = {"directory_only", "no_citable_material"}
_RELATIONS = {
    "shared_condition",
    "localized_contrast",
    "temporal_progression",
    "practical_consequence",
    "independent",
}


@dataclass(frozen=True)
class ArticleBriefLine:
    line_id: str
    editorial_intent: str
    depth: Literal["DEVELOP", "WEAVE", "BRIEF"]
    story_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    relation: ArticleBriefRelation
    salient_support_ids: tuple[str, ...]
    caveat_support_ids: tuple[str, ...]


@dataclass(frozen=True)
class ArticleStoryDisposition:
    story_id: str
    depth: ArticleDepth
    line_id: str | None
    reason_code: ArticleOmissionReason | None = None


@dataclass(frozen=True)
class ArticleEditorialBrief:
    central_line: str
    central_support_ids: tuple[str, ...]
    lines: tuple[ArticleBriefLine, ...]
    dispositions: tuple[ArticleStoryDisposition, ...]


def _fail(message: str) -> NoReturn:
    raise PublicationGenerationError(f"Invalid article editorial brief: {message}")


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a non-empty string")
    return value.strip()


def _string_tuple(value: object, field: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{field} must be an array")
    result = tuple(_string(item, field) for item in cast(list[object], value))
    if not allow_empty and not result:
        _fail(f"{field} must not be empty")
    if len(result) != len(set(result)):
        _fail(f"{field} contains duplicates")
    return result


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{field} must be an object")
    return cast(dict[str, object], value)


def _line_depth(value: object, field: str) -> Literal["DEVELOP", "WEAVE", "BRIEF"]:
    if not isinstance(value, str) or value not in _DEPTHS:
        _fail(f"{field} is invalid")
    return cast(Literal["DEVELOP", "WEAVE", "BRIEF"], value)


def _relation(value: object, field: str) -> ArticleBriefRelation:
    if not isinstance(value, str) or value not in _RELATIONS:
        _fail(f"{field} is invalid")
    return cast(ArticleBriefRelation, value)


def _omission_reason(value: object, field: str) -> ArticleOmissionReason:
    if not isinstance(value, str) or value not in _OMISSION_REASONS:
        _fail(f"{field} is invalid")
    return cast(ArticleOmissionReason, value)


def _citable_support_owners(
    *, context: ArticleEditorialContext, material_projection: ArticleMaterialProjection
) -> dict[str, str]:
    owners: dict[str, str] = {}
    for support in context.support_index:
        if support.publication_use != "PUBLISH" or support.evidence_kind == "resident_question":
            continue
        support_id = support.support_id
        projected = material_projection.text_by_support_id.get(support_id, "").strip()
        action = material_projection.actions_by_support_id.get(support_id)
        if action == "SUPPRESS_PROMOTION_ONLY" or not projected:
            continue
        if not action:
            _fail(f"PUBLISH support {support_id!r} is absent from material projection")
        if action not in {"KEEP", "TRIM_DIRECTORY"}:
            _fail(f"PUBLISH support {support_id!r} has unknown projection action {action!r}")
        story_id = support.story_id
        if not story_id:
            _fail(f"support {support_id!r} has no Story owner")
        if support_id in owners:
            _fail(f"duplicate support ID {support_id!r}")
        owners[support_id] = story_id
    return owners


def parse_article_editorial_brief(
    raw: str,
    *,
    coverage_plan: ArticleCoveragePlan,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
) -> ArticleEditorialBrief:
    """Parse and validate every Story/support reference before the writer can use it."""
    try:
        root = _mapping(json.loads(raw), "root")
    except PublicationGenerationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PublicationGenerationError(
            "Invalid article editorial brief: response is not JSON"
        ) from exc

    story_ids = tuple(story.story_id for story in coverage_plan.stories)
    if len(story_ids) != len(set(story_ids)):
        _fail("coverage plan contains duplicate Story IDs")
    known_stories = set(story_ids)
    owners = _citable_support_owners(context=context, material_projection=material_projection)

    lines_raw = root.get("lines")
    if not isinstance(lines_raw, list):
        _fail("lines must be an array")
    lines_raw = cast(list[object], lines_raw)
    lines: list[ArticleBriefLine] = []
    lines_by_id: dict[str, ArticleBriefLine] = {}
    story_to_line: dict[str, str] = {}
    for index, raw_line in enumerate(lines_raw):
        data = _mapping(raw_line, f"lines[{index}]")
        line_id = _string(data.get("line_id"), f"lines[{index}].line_id")
        if line_id in lines_by_id:
            _fail(f"duplicate line ID {line_id!r}")
        editorial_intent = _string(data.get("editorial_intent"), f"lines[{index}].editorial_intent")
        depth = _line_depth(data.get("depth"), f"lines[{index}].depth")
        relation = _relation(data.get("relation"), f"lines[{index}].relation")
        line_stories = _string_tuple(
            data.get("story_ids"), f"lines[{index}].story_ids", allow_empty=False
        )
        unknown_stories = set(line_stories) - known_stories
        if unknown_stories:
            _fail(f"line {line_id!r} references unknown Stories: {sorted(unknown_stories)}")
        for story_id in line_stories:
            if story_id in story_to_line:
                _fail(f"Story {story_id!r} is assigned to multiple lines")
            story_to_line[story_id] = line_id
        support_ids = _string_tuple(
            data.get("support_ids"), f"lines[{index}].support_ids", allow_empty=False
        )
        for support_id in support_ids:
            owner = owners.get(support_id)
            if owner is None:
                _fail(f"line {line_id!r} references unknown or non-citable support {support_id!r}")
            if owner not in line_stories:
                _fail(f"support {support_id!r} belongs to {owner!r}, not line {line_id!r}")
        supported_stories = {owners[support_id] for support_id in support_ids}
        unsupported_members = set(line_stories) - supported_stories
        if unsupported_members:
            _fail(
                f"line {line_id!r} has Stories without their own cited support: "
                f"{sorted(unsupported_members)}"
            )
        salient = _string_tuple(
            data.get("salient_support_ids", []), f"lines[{index}].salient_support_ids"
        )
        caveats = _string_tuple(
            data.get("caveat_support_ids", []), f"lines[{index}].caveat_support_ids"
        )
        if not set(salient).issubset(support_ids):
            _fail(f"line {line_id!r} has salient support outside support_ids")
        if not set(caveats).issubset(support_ids):
            _fail(f"line {line_id!r} has caveat support outside support_ids")
        line = ArticleBriefLine(
            line_id=line_id,
            editorial_intent=editorial_intent,
            depth=depth,
            story_ids=line_stories,
            support_ids=support_ids,
            relation=relation,
            salient_support_ids=salient,
            caveat_support_ids=caveats,
        )
        lines.append(line)
        lines_by_id[line_id] = line

    dispositions_raw = root.get("dispositions")
    if not isinstance(dispositions_raw, list):
        _fail("dispositions must be an array")
    dispositions_raw = cast(list[object], dispositions_raw)
    dispositions: list[ArticleStoryDisposition] = []
    dispositions_by_story: dict[str, ArticleStoryDisposition] = {}
    for index, raw_disposition in enumerate(dispositions_raw):
        data = _mapping(raw_disposition, f"dispositions[{index}]")
        story_id = _string(data.get("story_id"), f"dispositions[{index}].story_id")
        if story_id not in known_stories:
            _fail(f"disposition references unknown Story {story_id!r}")
        if story_id in dispositions_by_story:
            _fail(f"duplicate disposition for Story {story_id!r}")
        raw_depth = data.get("depth")
        if raw_depth == "OMIT":
            disposition_line_raw = data.get("line_id")
            reason_raw = data.get("reason_code")
            if disposition_line_raw is not None:
                _fail(f"omitted Story {story_id!r} must not have a line")
            if not isinstance(reason_raw, str) or reason_raw not in _OMISSION_REASONS:
                _fail(f"omitted Story {story_id!r} has invalid reason_code")
            reason = _omission_reason(reason_raw, f"omitted Story {story_id!r} reason_code")
            if story_id in owners.values():
                _fail(
                    f"Story {story_id!r} has citable projected material and cannot be omitted; "
                    "assign it BRIEF, WEAVE, or DEVELOP"
                )
            if story_id in story_to_line:
                _fail(f"omitted Story {story_id!r} appears in a narrative line")
            disposition = ArticleStoryDisposition(story_id, "OMIT", None, reason)
        elif raw_depth in _DEPTHS:
            disposition_depth = cast(Literal["DEVELOP", "WEAVE", "BRIEF"], raw_depth)
            disposition_line_raw = data.get("line_id")
            if not isinstance(disposition_line_raw, str) or disposition_line_raw not in lines_by_id:
                _fail(f"non-omitted Story {story_id!r} must reference a valid line")
            disposition_line_id = disposition_line_raw
            if story_to_line.get(story_id) != disposition_line_id:
                _fail(
                    f"Story {story_id!r} must appear in its assigned line {disposition_line_id!r}"
                )
            if data.get("reason_code") is not None:
                _fail(f"non-omitted Story {story_id!r} must not have reason_code")
            if lines_by_id[disposition_line_id].depth != disposition_depth:
                _fail(f"Story {story_id!r} depth must match line {disposition_line_id!r}")
            disposition = ArticleStoryDisposition(
                story_id, disposition_depth, disposition_line_id, None
            )
        else:
            _fail(f"disposition for Story {story_id!r} has invalid depth")
        dispositions.append(disposition)
        dispositions_by_story[story_id] = disposition

    missing_stories = known_stories - set(dispositions_by_story)
    if missing_stories:
        _fail(f"missing dispositions for Stories: {sorted(missing_stories)}")
    if len(dispositions) != len(story_ids):
        _fail("there must be exactly one disposition per coverage Story")
    for story_id in story_to_line:
        assigned_disposition = dispositions_by_story.get(story_id)
        if assigned_disposition is None or assigned_disposition.depth == "OMIT":
            _fail(f"line Story {story_id!r} has no non-omitted disposition")

    central_line = _string(root.get("central_line"), "central_line")
    central_support_ids = _string_tuple(
        root.get("central_support_ids"), "central_support_ids", allow_empty=False
    )
    for support_id in central_support_ids:
        owner = owners.get(support_id)
        if owner is None:
            _fail(f"central line references unknown or non-citable support {support_id!r}")
        if owner not in known_stories:
            _fail(f"central support {support_id!r} belongs to a Story outside the coverage plan")
        if dispositions_by_story[owner].depth == "OMIT":
            _fail(f"central support {support_id!r} belongs to omitted Story {owner!r}")

    return ArticleEditorialBrief(
        central_line=central_line,
        central_support_ids=central_support_ids,
        lines=tuple(lines),
        dispositions=tuple(dispositions),
    )
