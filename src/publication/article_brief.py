"""Validated editorial roadmap for a city-life long-read article."""

from __future__ import annotations

import json
import logging
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
logger = logging.getLogger(__name__)


class DuplicateArticleStoryAssignmentError(PublicationGenerationError):
    """The planner assigned one coverage Story to more than one line."""

    def __init__(self, story_key: str) -> None:
        self.story_key = story_key
        super().__init__(
            "Invalid article editorial brief: "
            f"Story key {story_key!r} is assigned to multiple lines"
        )


class ArticleBriefValidationError(PublicationGenerationError):
    """Planner output failed validation and carries a safe repair hint."""

    def __init__(self, message: str, repair_finding: str) -> None:
        self.repair_finding = repair_finding
        super().__init__(message)


class ArticleBriefInputInvariantError(PublicationGenerationError):
    """The frozen inputs to planning are internally inconsistent."""


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
    geographic_area_id: str | None = None
    geographic_area_name: str | None = None
    geographic_place_names: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class ArticlePlannerReferenceMap:
    story_id_by_key: dict[str, str]
    support_id_by_key: dict[str, str]


def _fail(message: str, *, repair_finding: str | None = None) -> NoReturn:
    raise ArticleBriefValidationError(
        f"Invalid article editorial brief: {message}",
        repair_finding or "The roadmap failed strict schema, evidence, or coverage validation.",
    )


def _input_fail(message: str) -> NoReturn:
    raise ArticleBriefInputInvariantError(f"Invalid article brief input: {message}")


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(
            f"{field} must be a non-empty string",
            repair_finding=f"Provide a non-empty string for {field}.",
        )
    return value.strip()


def _string_tuple(
    value: object,
    field: str,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{field} must be an array", repair_finding=f"Provide an array for {field}.")
    result = tuple(_string(item, field) for item in cast(list[object], value))
    # These arrays contain references, so a repeated ID adds no semantic
    # information. Normalize harmless model repetition while keeping order.
    result = tuple(dict.fromkeys(result))
    if not allow_empty and not result:
        _fail(f"{field} must not be empty", repair_finding=f"Include a value in {field}.")
    return result


def _mapped_reference_tuple(
    value: object,
    field: str,
    key_to_id: dict[str, str],
    *,
    allow_empty: bool = True,
    ignore_unknown: bool = False,
) -> tuple[str, ...]:
    keys = _string_tuple(value, field)
    unknown = tuple(key for key in keys if key not in key_to_id)
    if unknown and not ignore_unknown:
        _fail(
            f"{field} contains unknown references: {list(unknown)}",
            repair_finding=(
                f"{field} contains unrecognized aliases. Use only the opaque keys listed in "
                "the dossier."
            ),
        )
    mapped = tuple(dict.fromkeys(key_to_id[key] for key in keys if key in key_to_id))
    if not allow_empty and not mapped:
        _fail(
            f"{field} must contain at least one known reference",
            repair_finding=f"Include at least one known dossier alias in {field}.",
        )
    return mapped


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{field} must be an object", repair_finding=f"Provide an object for {field}.")
    return cast(dict[str, object], value)


def _line_depth(value: object, field: str) -> Literal["DEVELOP", "WEAVE", "BRIEF"]:
    if not isinstance(value, str) or value not in _DEPTHS:
        _fail(
            f"{field} is invalid",
            repair_finding=f"Use DEVELOP, WEAVE, BRIEF, or OMIT for {field}.",
        )
    return cast(Literal["DEVELOP", "WEAVE", "BRIEF"], value)


def _relation(value: object, field: str) -> ArticleBriefRelation:
    if not isinstance(value, str) or value not in _RELATIONS:
        _fail(f"{field} is invalid", repair_finding=f"Use a supported relation for {field}.")
    return cast(ArticleBriefRelation, value)


def _omission_reason(value: object, field: str) -> ArticleOmissionReason:
    if not isinstance(value, str) or value not in _OMISSION_REASONS:
        _fail(
            f"{field} is invalid",
            repair_finding=f"Use a supported omission reason for {field}.",
        )
    return cast(ArticleOmissionReason, value)


def _citable_support_owners(
    *,
    coverage_story_ids: set[str],
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
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
            _input_fail(f"PUBLISH support {support_id!r} is absent from material projection")
        if action not in {"KEEP", "TRIM_DIRECTORY"}:
            _input_fail(f"PUBLISH support {support_id!r} has unknown projection action {action!r}")
        story_id = support.story_id
        if not story_id:
            _input_fail(f"support {support_id!r} has no Story owner")
        if story_id not in coverage_story_ids:
            continue
        if support_id in owners:
            _input_fail(f"duplicate support ID {support_id!r}")
        owners[support_id] = story_id
    return owners


def parse_article_editorial_brief(
    raw: str,
    *,
    coverage_plan: ArticleCoveragePlan,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
    reference_map: ArticlePlannerReferenceMap,
) -> ArticleEditorialBrief:
    """Parse and validate every Story/support reference before the writer can use it."""
    try:
        root = _mapping(json.loads(raw), "root")
    except PublicationGenerationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ArticleBriefValidationError(
            "Invalid article editorial brief: response is not JSON",
            "Return one valid JSON object matching the required roadmap schema.",
        ) from exc

    story_ids = tuple(story.story_id for story in coverage_plan.stories)
    if len(story_ids) != len(set(story_ids)):
        _input_fail("coverage plan contains duplicate Story IDs")
    known_stories = set(story_ids)
    story_key_by_id = {
        story_id: story_key for story_key, story_id in reference_map.story_id_by_key.items()
    }
    support_key_by_id = {
        support_id: support_key
        for support_key, support_id in reference_map.support_id_by_key.items()
    }
    owners = _citable_support_owners(
        coverage_story_ids=known_stories,
        context=context,
        material_projection=material_projection,
    )
    citable_story_ids = set(owners.values())

    lines_raw = root.get("lines")
    if not isinstance(lines_raw, list):
        _fail("lines must be an array", repair_finding="Provide an array for lines.")
    lines_raw = cast(list[object], lines_raw)
    lines: list[ArticleBriefLine] = []
    lines_by_id: dict[str, ArticleBriefLine] = {}
    story_to_line: dict[str, str] = {}
    retained_citable_story_keys: set[str] = set()

    def retain_as_brief(story_id: str) -> tuple[str, ArticleDepth]:
        story_key = story_key_by_id.get(story_id)
        if story_key is None:
            _input_fail("citable coverage Story has no planner reference key")
        existing_line_id = story_to_line.get(story_id)
        if existing_line_id is not None:
            return existing_line_id, lines_by_id[existing_line_id].depth

        support_ids = tuple(
            support_id
            for support_id, owner_story_id in owners.items()
            if owner_story_id == story_id
        )
        support_keys = tuple(
            support_key_by_id[support_id]
            for support_id in support_ids
            if support_id in support_key_by_id
        )
        if not support_ids or len(support_keys) != len(support_ids):
            _input_fail("citable coverage Story has incomplete planner support references")

        base_line_id = f"brief-{story_key.casefold()}"
        line_id = base_line_id
        suffix = 2
        while line_id in lines_by_id:
            line_id = f"{base_line_id}-{suffix}"
            suffix += 1
        line = ArticleBriefLine(
            line_id=line_id,
            editorial_intent=(
                "Retain this citable city-life detail briefly using only its listed support."
            ),
            depth="BRIEF",
            story_ids=(story_id,),
            support_ids=support_ids,
            relation="independent",
            salient_support_ids=(),
            caveat_support_ids=(),
        )
        lines.append(line)
        lines_by_id[line_id] = line
        story_to_line[story_id] = line_id
        retained_citable_story_keys.add(story_key)
        return line_id, "BRIEF"

    for index, raw_line in enumerate(lines_raw):
        data = _mapping(raw_line, f"lines[{index}]")
        raw_story_keys = data.get("story_keys")
        if isinstance(raw_story_keys, list) and all(
            isinstance(key, str) and key.strip() for key in raw_story_keys
        ):
            seen_citable_story_ids: set[str] = set()
            for raw_story_key in raw_story_keys:
                story_id = reference_map.story_id_by_key.get(raw_story_key.strip())
                if story_id is None or story_id not in citable_story_ids:
                    continue
                if story_id in seen_citable_story_ids:
                    raise DuplicateArticleStoryAssignmentError(
                        story_key_by_id.get(story_id, "<unknown>")
                    )
                seen_citable_story_ids.add(story_id)
        line_stories = _mapped_reference_tuple(
            data.get("story_keys"),
            f"lines[{index}].story_keys",
            reference_map.story_id_by_key,
            ignore_unknown=True,
        )
        support_ids = _mapped_reference_tuple(
            data.get("support_keys"),
            f"lines[{index}].support_keys",
            reference_map.support_id_by_key,
            allow_empty=False,
        )
        if not line_stories:
            for support_id in support_ids:
                if owners.get(support_id) is None:
                    _fail(
                        f"line {index} references unknown or non-citable support {support_id!r}",
                        repair_finding=(
                            f"lines[{index}].support_keys contains an unknown or non-citable "
                            "alias. Use only citable support keys from the dossier."
                        ),
                    )
            _fail(
                f"line {index} cites support without a known coverage Story",
                repair_finding=(
                    f"Line {index} cites support but has no recognized story_key. Add the "
                    "owning coverage Story from the dossier."
                ),
            )
        line_id = _string(data.get("line_id"), f"lines[{index}].line_id")
        if line_id in lines_by_id:
            _fail(
                f"duplicate line ID {line_id!r}",
                repair_finding=f"Use a unique line_id for lines[{index}].",
            )
        editorial_intent = _string(data.get("editorial_intent"), f"lines[{index}].editorial_intent")
        depth = _line_depth(data.get("depth"), f"lines[{index}].depth")
        relation = _relation(data.get("relation"), f"lines[{index}].relation")
        unknown_stories = set(line_stories) - known_stories
        if unknown_stories:
            _fail(
                f"line {line_id!r} references unknown Stories: {sorted(unknown_stories)}",
                repair_finding=(
                    f"lines[{index}].story_keys contains unrecognized aliases. Use only the "
                    "opaque story keys listed in the dossier."
                ),
            )
        for story_id in line_stories:
            if story_id in story_to_line:
                raise DuplicateArticleStoryAssignmentError(
                    story_key_by_id.get(story_id, "<unknown>")
                )
            story_to_line[story_id] = line_id
        for support_id in support_ids:
            owner = owners.get(support_id)
            if owner is None:
                _fail(
                    f"line {line_id!r} references unknown or non-citable support {support_id!r}",
                    repair_finding=(
                        f"lines[{index}].support_keys contains an unknown or non-citable alias. "
                        "Use only citable support keys from the dossier."
                    ),
                )
            if owner not in line_stories:
                _fail(
                    f"support {support_id!r} belongs to {owner!r}, not line {line_id!r}",
                    repair_finding=(
                        f"support_key {support_key_by_id.get(support_id, '<unknown>')} belongs "
                        f"to story_key {story_key_by_id.get(owner, '<unknown>')}; cite it only "
                        "in a line containing that Story."
                    ),
                )
        supported_stories = {owners[support_id] for support_id in support_ids}
        unsupported_members = set(line_stories) - supported_stories
        if unsupported_members:
            unsupported_keys = sorted(
                story_key_by_id.get(story_id, "<unknown>") for story_id in unsupported_members
            )
            _fail(
                f"line {line_id!r} has Stories without their own cited support: "
                f"{sorted(unsupported_members)}",
                repair_finding=(
                    "Each Story in a line must cite at least one of its own support keys. "
                    f"Affected story_keys: {unsupported_keys}."
                ),
            )
        salient = _mapped_reference_tuple(
            data.get("salient_support_keys", []),
            f"lines[{index}].salient_support_keys",
            reference_map.support_id_by_key,
            ignore_unknown=True,
        )
        caveats = _mapped_reference_tuple(
            data.get("caveat_support_keys", []),
            f"lines[{index}].caveat_support_keys",
            reference_map.support_id_by_key,
            ignore_unknown=True,
        )
        # These are optional ranking hints, not citations. Keep only hints that
        # are already backed by the line's authoritative support list.
        cited_supports = set(support_ids)
        salient = tuple(support_id for support_id in salient if support_id in cited_supports)
        caveats = tuple(support_id for support_id in caveats if support_id in cited_supports)
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
        _fail(
            "dispositions must be an array",
            repair_finding="Provide an array for dispositions.",
        )
    dispositions_raw = cast(list[object], dispositions_raw)
    dispositions: list[ArticleStoryDisposition] = []
    dispositions_by_story: dict[str, ArticleStoryDisposition] = {}
    for index, raw_disposition in enumerate(dispositions_raw):
        data = _mapping(raw_disposition, f"dispositions[{index}]")
        story_key = _string(data.get("story_key"), f"dispositions[{index}].story_key")
        mapped_story_id = reference_map.story_id_by_key.get(story_key)
        if mapped_story_id is None:
            continue
        story_id = mapped_story_id
        if story_id in dispositions_by_story:
            _fail(
                f"duplicate disposition for Story {story_id!r}",
                repair_finding=(
                    f"story_key {story_key_by_id.get(story_id, '<unknown>')} must have exactly "
                    "one disposition."
                ),
            )
        raw_depth = data.get("depth")
        if raw_depth == "OMIT":
            disposition_line_raw = data.get("line_id")
            reason_raw = data.get("reason_code")
            if story_id in citable_story_ids:
                retained_line_id, retained_depth = retain_as_brief(story_id)
                disposition = ArticleStoryDisposition(
                    story_id,
                    retained_depth,
                    retained_line_id,
                    None,
                )
            else:
                if disposition_line_raw is not None:
                    _fail(f"omitted Story {story_id!r} must not have a line")
                if not isinstance(reason_raw, str) or reason_raw not in _OMISSION_REASONS:
                    _fail(f"omitted Story {story_id!r} has invalid reason_code")
                reason = _omission_reason(reason_raw, f"dispositions[{index}].reason_code")
                if story_id in story_to_line:
                    _fail(f"omitted Story {story_id!r} appears in a narrative line")
                disposition = ArticleStoryDisposition(story_id, "OMIT", None, reason)
        elif raw_depth in _DEPTHS:
            disposition_depth = cast(Literal["DEVELOP", "WEAVE", "BRIEF"], raw_depth)
            disposition_line_raw = data.get("line_id")
            if not isinstance(disposition_line_raw, str) or disposition_line_raw not in lines_by_id:
                _fail(
                    f"non-omitted Story {story_id!r} must reference a valid line",
                    repair_finding=(
                        f"Assign non-OMIT story_key {story_key_by_id.get(story_id, '<unknown>')} "
                        "to a valid line_id from lines."
                    ),
                )
            disposition_line_id = disposition_line_raw
            if story_to_line.get(story_id) != disposition_line_id:
                _fail(
                    f"Story {story_id!r} must appear in its assigned line {disposition_line_id!r}",
                    repair_finding=(
                        f"story_key {story_key_by_id.get(story_id, '<unknown>')} must appear "
                        "in its assigned narrative line and have the same depth as that line."
                    ),
                )
            if data.get("reason_code") is not None:
                _fail(
                    f"non-omitted Story {story_id!r} must not have reason_code",
                    repair_finding=(
                        f"Remove reason_code from non-OMIT story_key "
                        f"{story_key_by_id.get(story_id, '<unknown>')}."
                    ),
                )
            # A line has one authoritative editorial depth. The model repeats
            # that value on every Story disposition, so use the line value as
            # canonical if the redundant per-Story field drifts.
            disposition_depth = lines_by_id[disposition_line_id].depth
            disposition = ArticleStoryDisposition(
                story_id, disposition_depth, disposition_line_id, None
            )
        else:
            _fail(
                f"disposition for Story {story_id!r} has invalid depth",
                repair_finding=(
                    f"Use DEVELOP, WEAVE, BRIEF, or OMIT for story_key "
                    f"{story_key_by_id.get(story_id, '<unknown>')}."
                ),
            )
        dispositions.append(disposition)
        dispositions_by_story[story_id] = disposition

    missing_stories = known_stories - set(dispositions_by_story)
    for story_id in sorted(missing_stories & citable_story_ids):
        retained_line_id, retained_depth = retain_as_brief(story_id)
        disposition = ArticleStoryDisposition(
            story_id,
            retained_depth,
            retained_line_id,
            None,
        )
        dispositions.append(disposition)
        dispositions_by_story[story_id] = disposition
    missing_stories = known_stories - set(dispositions_by_story)
    if missing_stories:
        missing_keys = sorted(
            story_key_by_id.get(story_id, "<unknown>") for story_id in missing_stories
        )
        _fail(
            f"missing dispositions for Stories: {sorted(missing_stories)}",
            repair_finding=(
                "Every coverage Story needs exactly one disposition. Missing story_keys: "
                f"{missing_keys}. Assign citable Stories BRIEF, WEAVE, or DEVELOP; OMIT only "
                "Stories without citable projected material."
            ),
        )
    if len(dispositions) != len(story_ids):
        _fail("there must be exactly one disposition per coverage Story")
    for story_id in story_to_line:
        assigned_disposition = dispositions_by_story.get(story_id)
        if assigned_disposition is None or assigned_disposition.depth == "OMIT":
            _fail(
                f"line Story {story_id!r} has no non-omitted disposition",
                repair_finding=(
                    f"story_key {story_key_by_id.get(story_id, '<unknown>')} appears in a line "
                    "and needs one non-OMIT disposition."
                ),
            )

    central_line = _string(root.get("central_line"), "central_line")
    central_support_ids = _mapped_reference_tuple(
        root.get("central_support_keys"),
        "central_support_keys",
        reference_map.support_id_by_key,
        allow_empty=False,
    )
    for support_id in central_support_ids:
        owner = owners.get(support_id)
        if owner is None:
            _fail(
                f"central line references unknown or non-citable support {support_id!r}",
                repair_finding=(
                    "central_support_keys contains an unknown or non-citable alias. Use only "
                    "citable support keys from the dossier."
                ),
            )
        if owner not in known_stories:
            _fail(
                f"central support {support_id!r} belongs to a Story outside the coverage plan",
                repair_finding=(
                    f"central_support_keys alias {support_key_by_id.get(support_id, '<unknown>')} "
                    "belongs to a Story outside the coverage plan; choose a citable support key "
                    "from a coverage Story."
                ),
            )
        if dispositions_by_story[owner].depth == "OMIT":
            _fail(
                f"central support {support_id!r} belongs to omitted Story {owner!r}",
                repair_finding=(
                    f"central_support_keys alias {support_key_by_id.get(support_id, '<unknown>')} "
                    "belongs to an omitted Story; choose support from a non-OMIT coverage Story."
                ),
            )

    if retained_citable_story_keys:
        logger.warning(
            "Article planner omitted citable Stories; retained as BRIEF (%s)",
            len(retained_citable_story_keys),
        )

    return ArticleEditorialBrief(
        central_line=central_line,
        central_support_ids=central_support_ids,
        lines=tuple(lines),
        dispositions=tuple(dispositions),
    )


def normalize_duplicate_citable_story_assignment(
    raw: str,
    *,
    story_key: str,
    coverage_plan: ArticleCoveragePlan,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection,
    reference_map: ArticlePlannerReferenceMap,
) -> tuple[str, bool]:
    """Repair one duplicate citable Story assignment without bypassing validation.

    Only recognized, known aliases for the reported Story are changed. The
    caller must pass the returned JSON through ``parse_article_editorial_brief``
    again before using it.
    """
    story_id = reference_map.story_id_by_key.get(story_key)
    if story_id is None:
        return raw, False
    known_story_ids = {story.story_id for story in coverage_plan.stories}
    if story_id not in known_story_ids:
        return raw, False
    owners = _citable_support_owners(
        coverage_story_ids=known_story_ids,
        context=context,
        material_projection=material_projection,
    )
    if story_id not in set(owners.values()):
        return raw, False

    try:
        root_value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw, False
    if not isinstance(root_value, dict):
        return raw, False
    root = cast(dict[str, object], root_value)
    lines_value = root.get("lines")
    if not isinstance(lines_value, list):
        return raw, False
    lines = cast(list[object], lines_value)
    dispositions_value = root.get("dispositions")
    dispositions = (
        cast(list[object], dispositions_value) if isinstance(dispositions_value, list) else None
    )

    story_keys_by_id: dict[str, set[str]] = {}
    for alias, mapped_story_id in reference_map.story_id_by_key.items():
        story_keys_by_id.setdefault(mapped_story_id, set()).add(alias)
    story_aliases = story_keys_by_id.get(story_id, set())
    if not story_aliases:
        return raw, False

    # Work only with line objects whose story membership is structurally valid.
    # If a target line is malformed, leave it for the ordinary strict repair path.
    occurrences: list[tuple[int, dict[str, object], list[str]]] = []
    for index, raw_line in enumerate(lines):
        if not isinstance(raw_line, dict):
            continue
        line = cast(dict[str, object], raw_line)
        raw_story_keys = line.get("story_keys")
        if not isinstance(raw_story_keys, list):
            continue
        if not any(isinstance(key, str) and key.strip() in story_aliases for key in raw_story_keys):
            continue
        if not all(isinstance(key, str) and key.strip() for key in raw_story_keys):
            return raw, False
        raw_support_keys = line.get("support_keys")
        if (
            not isinstance(raw_support_keys, list)
            or not raw_support_keys
            or not all(isinstance(key, str) and key.strip() for key in raw_support_keys)
        ):
            return raw, False
        for optional_field in ("salient_support_keys", "caveat_support_keys"):
            optional_value = line.get(optional_field)
            if optional_field in line and (
                not isinstance(optional_value, list)
                or not all(isinstance(key, str) and key.strip() for key in optional_value)
            ):
                return raw, False
        occurrences.append((index, line, [key.strip() for key in raw_story_keys]))

    if not occurrences:
        return raw, False
    if (
        not any(
            len([key for key in story_keys if key in story_aliases]) > 1
            for _, _, story_keys in occurrences
        )
        and len(occurrences) < 2
    ):
        return raw, False

    original_line_content = {
        index: {
            "story_keys": list(cast(list[str], line["story_keys"])),
            "support_keys": list(cast(list[str], line["support_keys"])),
            "salient_support_keys": list(cast(list[str], line.get("salient_support_keys", []))),
            "caveat_support_keys": list(cast(list[str], line.get("caveat_support_keys", []))),
        }
        for index, line, _ in occurrences
    }
    # A single valid non-OMIT disposition selects a canonical occurrence only
    # when its line_id identifies exactly one of the Story's line occurrences.
    matching_dispositions: list[tuple[int, dict[str, object]]] = []
    if dispositions is not None:
        for index, raw_disposition in enumerate(dispositions):
            if not isinstance(raw_disposition, dict):
                continue
            disposition = cast(dict[str, object], raw_disposition)
            disposition_key = disposition.get("story_key")
            if isinstance(disposition_key, str) and disposition_key.strip() in story_aliases:
                matching_dispositions.append((index, disposition))

    canonical_index: int | None = None
    canonical_line: dict[str, object] | None = None
    canonical_line_id: str | None = None
    canonical_depth: str | None = None
    if len(matching_dispositions) == 1:
        _, disposition = matching_dispositions[0]
        disposition_line_id = disposition.get("line_id")
        depth = disposition.get("depth")
        if (
            isinstance(depth, str)
            and depth in _DEPTHS
            and disposition.get("reason_code") is None
            and isinstance(disposition_line_id, str)
            and disposition_line_id.strip()
            and disposition_line_id == disposition_line_id.strip()
        ):
            matching_occurrences = [
                (index, line)
                for index, line, _ in occurrences
                if line.get("line_id") == disposition_line_id
                and line.get("line_id") == disposition_line_id.strip()
            ]
            if len(matching_occurrences) == 1:
                canonical_index, canonical_line = matching_occurrences[0]
                canonical_line_id = disposition_line_id
                canonical_depth = cast(str, canonical_line.get("depth"))
                if canonical_depth not in _DEPTHS:
                    canonical_index = None
                    canonical_line = None
                    canonical_line_id = None
                    canonical_depth = None

    support_alias_story_owner = {
        alias: owners.get(support_id)
        for alias, support_id in reference_map.support_id_by_key.items()
    }
    removed_support_aliases_by_index: dict[int, set[str]] = {}
    moved_salient: list[str] = []
    moved_caveats: list[str] = []

    # Determine a target Story's citable support aliases that were already
    # cited, and move only those aliases from non-canonical duplicate lines.
    if canonical_line is not None and canonical_index is not None:
        canonical_supports = cast(list[str], canonical_line["support_keys"])
        for index, line, _ in occurrences:
            if index == canonical_index:
                continue
            supports = cast(list[str], line["support_keys"])
            moved_aliases = {
                key.strip()
                for key in supports
                if support_alias_story_owner.get(key.strip()) == story_id
            }
            if not moved_aliases:
                continue
            removed_support_aliases_by_index[index] = moved_aliases
            canonical_supports.extend(
                key
                for key in supports
                if key.strip() in moved_aliases
                and key.strip() not in {existing.strip() for existing in canonical_supports}
            )
            for field, target in (
                ("salient_support_keys", moved_salient),
                ("caveat_support_keys", moved_caveats),
            ):
                hints = cast(list[str], line.get(field, []))
                target.extend(
                    key
                    for key in hints
                    if key.strip() in moved_aliases
                    and key.strip() not in {item.strip() for item in target}
                )

        for field, moved in (
            ("salient_support_keys", moved_salient),
            ("caveat_support_keys", moved_caveats),
        ):
            existing = cast(list[str], canonical_line.get(field, []))
            cited = {key.strip() for key in canonical_supports}
            existing.extend(
                key
                for key in moved
                if key.strip() in cited and key.strip() not in {item.strip() for item in existing}
            )
            if field in canonical_line or moved:
                canonical_line[field] = existing

    for index, line, _raw_story_keys in occurrences:
        original_story_keys = cast(list[str], line["story_keys"])
        kept_story_keys = [key for key in original_story_keys if key.strip() not in story_aliases]
        if index == canonical_index:
            first_target_key = next(
                key for key in original_story_keys if key.strip() in story_aliases
            )
            line["story_keys"] = [
                first_target_key if key.strip() in story_aliases else key
                for key in original_story_keys
                if key.strip() not in story_aliases or key == first_target_key
            ]
        else:
            line["story_keys"] = kept_story_keys
            removed_supports = removed_support_aliases_by_index.get(index, set())
            line["support_keys"] = [
                key
                for key in cast(list[str], line["support_keys"])
                if key.strip() not in removed_supports
            ]
            for field in ("salient_support_keys", "caveat_support_keys"):
                if field in line:
                    hints = cast(list[str], line[field])
                    line[field] = [key for key in hints if key.strip() not in removed_supports]

    if canonical_index is None:
        # Without a unique valid disposition, let the parser's established
        # missing-citable-Story path recreate one BRIEF with every citable support.
        for _index, line, _ in occurrences:
            line["support_keys"] = [
                key
                for key in cast(list[str], line["support_keys"])
                if support_alias_story_owner.get(key.strip()) != story_id
            ]
            for field in ("salient_support_keys", "caveat_support_keys"):
                if field in line:
                    line[field] = [
                        key
                        for key in cast(list[str], line[field])
                        if support_alias_story_owner.get(key.strip()) != story_id
                    ]
        if dispositions is not None:
            root["dispositions"] = [
                raw_disposition
                for raw_disposition in dispositions
                if not (
                    isinstance(raw_disposition, dict)
                    and isinstance(raw_disposition.get("story_key"), str)
                    and raw_disposition["story_key"].strip() in story_aliases
                )
            ]
    elif dispositions is not None:
        disposition_index, disposition = matching_dispositions[0]
        disposition["line_id"] = canonical_line_id
        disposition["depth"] = canonical_depth
        disposition["reason_code"] = None
        dispositions[disposition_index] = disposition

    safe_to_drop_indices: set[int] = set()
    allowed_fields = {
        "line_id",
        "editorial_intent",
        "depth",
        "relation",
        "story_keys",
        "support_keys",
        "salient_support_keys",
        "caveat_support_keys",
    }
    for index, line, _ in occurrences:
        original = original_line_content[index]
        original_supports = original["support_keys"]
        line_id = line.get("line_id")
        editorial_intent = line.get("editorial_intent")
        line_depth = line.get("depth")
        relation = line.get("relation")
        line_id_is_unique = (
            isinstance(line_id, str)
            and bool(line_id.strip())
            and sum(
                1
                for other_line in lines
                if isinstance(other_line, dict)
                and isinstance(other_line.get("line_id"), str)
                and other_line["line_id"].strip() == line_id.strip()
            )
            == 1
        )
        metadata_is_valid = (
            line_id_is_unique
            and isinstance(editorial_intent, str)
            and bool(editorial_intent.strip())
            and isinstance(line_depth, str)
            and line_depth in _DEPTHS
            and isinstance(relation, str)
            and relation in _RELATIONS
        )
        original_hints_valid = all(
            key.strip() in {support_key.strip() for support_key in original_supports}
            and support_alias_story_owner.get(key.strip()) == story_id
            for field in ("salient_support_keys", "caveat_support_keys")
            for key in original[field]
        )
        originally_only_target = (
            all(key.strip() in story_aliases for key in original["story_keys"])
            and bool(original_supports)
            and all(
                support_alias_story_owner.get(key.strip()) == story_id for key in original_supports
            )
            and original_hints_valid
            and metadata_is_valid
            and set(line).issubset(allowed_fields)
        )
        if (
            index != canonical_index
            and originally_only_target
            and not cast(list[str], line["story_keys"])
            and not cast(list[str], line["support_keys"])
            and not cast(list[str], line.get("salient_support_keys", []))
            and not cast(list[str], line.get("caveat_support_keys", []))
        ):
            safe_to_drop_indices.add(index)

    if safe_to_drop_indices:
        root["lines"] = [
            line for index, line in enumerate(lines) if index not in safe_to_drop_indices
        ]
    return json.dumps(root, ensure_ascii=False, separators=(",", ":")), True
