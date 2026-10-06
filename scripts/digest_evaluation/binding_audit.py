"""Deterministic integrity checks for LLM-produced digest evidence bindings.

These checks validate IDs and exact text spans. They do not determine whether a
claim is semantically supported by the underlying source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_BINDING_STATUSES = {"fully_supported", "partially_supported", "unsupported", "unclear"}


@dataclass(frozen=True)
class DigestBindingAudit:
    invalid_structure: tuple[str, ...]
    unknown_fact_ids: tuple[str, ...]
    unknown_support_ids: tuple[str, ...]
    unknown_uncovered_fact_ids: tuple[str, ...]
    invalid_support_bindings: tuple[tuple[str, str], ...]
    invalid_status_bindings: tuple[tuple[str, str], ...]
    nonexact_paragraph_spans: tuple[str, ...]
    nonexact_unsupported_spans: tuple[str, ...]
    conflicting_fact_ids: tuple[str, ...]
    unknown_story_ids: tuple[str, ...]
    unknown_story_support_ids: tuple[str, ...]
    unknown_uncovered_story_ids: tuple[str, ...]
    invalid_story_support_bindings: tuple[tuple[str, str], ...]
    invalid_story_status_bindings: tuple[tuple[str, str], ...]
    unaccounted_story_ids: tuple[str, ...]
    unaccounted_fact_ids: tuple[str, ...]
    covered_fact_ids: tuple[str, ...]
    uncovered_fact_ids: tuple[str, ...]
    covered_story_ids: tuple[str, ...]
    uncovered_story_ids: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not any(
            (
                self.invalid_structure,
                self.unknown_fact_ids,
                self.unknown_support_ids,
                self.unknown_uncovered_fact_ids,
                self.invalid_support_bindings,
                self.invalid_status_bindings,
                self.nonexact_paragraph_spans,
                self.nonexact_unsupported_spans,
                self.conflicting_fact_ids,
                self.unknown_story_ids,
                self.unknown_story_support_ids,
                self.unknown_uncovered_story_ids,
                self.invalid_story_support_bindings,
                self.invalid_story_status_bindings,
                self.unaccounted_story_ids,
                self.unaccounted_fact_ids,
            )
        )

    @property
    def has_full_coverage(self) -> bool:
        return self.valid and not self.uncovered_fact_ids and not self.uncovered_story_ids


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _strings(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    return tuple(str(item).strip() for item in value if str(item).strip())


def audit_digest_binding_integrity(
    candidate_text: str,
    binding_result: Mapping[str, Any],
    required_facts: Sequence[Any],
    *,
    required_story_supports: Mapping[str, Sequence[str]] | None = None,
) -> DigestBindingAudit:
    """Validate exact spans, fact/support ownership, and complete ledger accounting.

    Each paragraph uses ``fact_bindings`` entries with ``fact_id``, its own
    ``support_ids``, and a support status. A fact counts as covered only when a
    fully supported binding uses supports owned by that exact fact. Facts with
    weaker statuses must also appear in ``uncovered_fact_ids``.
    """
    structure_errors: list[str] = []
    fact_supports: dict[str, set[str]] = {}
    fact_order: list[str] = []
    for index, fact in enumerate(required_facts):
        fact_id = str(_field(fact, "fact_id", "")).strip()
        support_ids = _strings(_field(fact, "support_ids", ()))
        if not fact_id:
            structure_errors.append(f"required_facts[{index}]:missing_fact_id")
            continue
        if fact_id in fact_supports:
            structure_errors.append(f"required_facts[{index}]:duplicate_fact_id:{fact_id}")
            continue
        if support_ids is None:
            structure_errors.append(f"required_facts[{index}]:invalid_support_ids:{fact_id}")
            support_ids = ()
        fact_order.append(fact_id)
        fact_supports[fact_id] = set(support_ids)

    known_fact_ids = set(fact_supports)
    known_support_ids = set().union(*fact_supports.values()) if fact_supports else set()
    story_supports = (
        {
            str(story_id): set(support_ids)
            for story_id, support_ids in required_story_supports.items()
        }
        if required_story_supports is not None
        else None
    )
    known_story_ids = set(story_supports or ())
    known_story_support_ids = set().union(*story_supports.values()) if story_supports else set()
    unknown_fact_ids: set[str] = set()
    unknown_support_ids: set[str] = set()
    unknown_story_ids: set[str] = set()
    unknown_story_support_ids: set[str] = set()
    invalid_support_bindings: set[tuple[str, str]] = set()
    invalid_status_bindings: set[tuple[str, str]] = set()
    nonexact_paragraph_spans: set[str] = set()
    covered_facts: set[str] = set()
    covered_stories: set[str] = set()
    invalid_story_support_bindings: set[tuple[str, str]] = set()
    invalid_story_status_bindings: set[tuple[str, str]] = set()

    paragraphs = binding_result.get("paragraphs")
    if not isinstance(paragraphs, list):
        structure_errors.append("paragraphs:expected_list")
        paragraphs = []

    for paragraph_index, paragraph in enumerate(paragraphs):
        if not isinstance(paragraph, Mapping):
            structure_errors.append(f"paragraphs[{paragraph_index}]:expected_object")
            continue
        exact_text = paragraph.get("exact_text")
        if not isinstance(exact_text, str) or not exact_text or exact_text not in candidate_text:
            nonexact_paragraph_spans.add(str(exact_text or ""))

        bindings = paragraph.get("fact_bindings")
        if not isinstance(bindings, list):
            structure_errors.append(f"paragraphs[{paragraph_index}]:fact_bindings_expected_list")
            continue
        for binding_index, binding in enumerate(bindings):
            where = f"paragraphs[{paragraph_index}].fact_bindings[{binding_index}]"
            if not isinstance(binding, Mapping):
                structure_errors.append(f"{where}:expected_object")
                continue
            fact_id = str(binding.get("fact_id", "")).strip()
            if not fact_id:
                structure_errors.append(f"{where}:missing_fact_id")
                continue
            if fact_id not in known_fact_ids:
                unknown_fact_ids.add(fact_id)
                continue

            support_ids = _strings(binding.get("support_ids"))
            if support_ids is None or not support_ids:
                structure_errors.append(f"{where}:missing_support_ids:{fact_id}")
                support_ids = ()
            for support_id in support_ids:
                if support_id not in known_support_ids:
                    unknown_support_ids.add(support_id)
                if support_id not in fact_supports[fact_id]:
                    invalid_support_bindings.add((fact_id, support_id))

            status = str(binding.get("status", "")).strip()
            if status not in _BINDING_STATUSES:
                invalid_status_bindings.add((fact_id, status))
            elif (
                status == "fully_supported"
                and support_ids
                and all(
                    support_id in fact_supports[fact_id] and support_id in known_support_ids
                    for support_id in support_ids
                )
            ):
                covered_facts.add(fact_id)

        if story_supports is not None:
            story_bindings = paragraph.get("story_bindings")
            if not isinstance(story_bindings, list):
                structure_errors.append(
                    f"paragraphs[{paragraph_index}]:story_bindings_expected_list"
                )
                continue
            for binding_index, binding in enumerate(story_bindings):
                where = f"paragraphs[{paragraph_index}].story_bindings[{binding_index}]"
                if not isinstance(binding, Mapping):
                    structure_errors.append(f"{where}:expected_object")
                    continue
                story_id = str(binding.get("story_id", "")).strip()
                if not story_id:
                    structure_errors.append(f"{where}:missing_story_id")
                    continue
                if story_id not in known_story_ids:
                    unknown_story_ids.add(story_id)
                    continue
                support_ids = _strings(binding.get("support_ids"))
                if support_ids is None or not support_ids:
                    structure_errors.append(f"{where}:missing_support_ids:{story_id}")
                    support_ids = ()
                for support_id in support_ids:
                    if support_id not in known_story_support_ids:
                        unknown_story_support_ids.add(support_id)
                    if support_id not in story_supports[story_id]:
                        invalid_story_support_bindings.add((story_id, support_id))
                status = str(binding.get("status", "")).strip()
                if status not in _BINDING_STATUSES:
                    invalid_story_status_bindings.add((story_id, status))
                elif (
                    status == "fully_supported"
                    and support_ids
                    and all(support_id in story_supports[story_id] for support_id in support_ids)
                ):
                    covered_stories.add(story_id)

    raw_uncovered = binding_result.get("uncovered_fact_ids")
    if not isinstance(raw_uncovered, list):
        structure_errors.append("uncovered_fact_ids:expected_list")
        raw_uncovered = []
    uncovered_all = {str(value).strip() for value in raw_uncovered if str(value).strip()}
    unknown_uncovered = uncovered_all - known_fact_ids
    conflict = covered_facts & uncovered_all
    known_uncovered = uncovered_all & known_fact_ids
    unaccounted = known_fact_ids - covered_facts - known_uncovered

    if story_supports is None:
        known_uncovered_stories: set[str] = set()
        unknown_uncovered_stories: set[str] = set()
        unaccounted_stories: set[str] = set()
    else:
        raw_uncovered_stories = binding_result.get("uncovered_story_ids")
        if not isinstance(raw_uncovered_stories, list):
            structure_errors.append("uncovered_story_ids:expected_list")
            raw_uncovered_stories = []
        uncovered_stories_all = {
            str(value).strip() for value in raw_uncovered_stories if str(value).strip()
        }
        unknown_uncovered_stories = uncovered_stories_all - known_story_ids
        known_uncovered_stories = uncovered_stories_all & known_story_ids
        unaccounted_stories = known_story_ids - covered_stories - known_uncovered_stories

    raw_unsupported = binding_result.get("unsupported_spans")
    if not isinstance(raw_unsupported, list):
        structure_errors.append("unsupported_spans:expected_list")
        raw_unsupported = []
    nonexact_unsupported = {
        str(span)
        for span in raw_unsupported
        if not isinstance(span, str) or not span or span not in candidate_text
    }

    return DigestBindingAudit(
        invalid_structure=tuple(sorted(set(structure_errors))),
        unknown_fact_ids=tuple(sorted(unknown_fact_ids)),
        unknown_support_ids=tuple(sorted(unknown_support_ids)),
        unknown_uncovered_fact_ids=tuple(sorted(unknown_uncovered)),
        invalid_support_bindings=tuple(sorted(invalid_support_bindings)),
        invalid_status_bindings=tuple(sorted(invalid_status_bindings)),
        nonexact_paragraph_spans=tuple(sorted(nonexact_paragraph_spans)),
        nonexact_unsupported_spans=tuple(sorted(nonexact_unsupported)),
        conflicting_fact_ids=tuple(sorted(conflict)),
        unknown_story_ids=tuple(sorted(unknown_story_ids)),
        unknown_story_support_ids=tuple(sorted(unknown_story_support_ids)),
        unknown_uncovered_story_ids=tuple(sorted(unknown_uncovered_stories)),
        invalid_story_support_bindings=tuple(sorted(invalid_story_support_bindings)),
        invalid_story_status_bindings=tuple(sorted(invalid_story_status_bindings)),
        unaccounted_story_ids=tuple(sorted(unaccounted_stories)),
        unaccounted_fact_ids=tuple(fact_id for fact_id in fact_order if fact_id in unaccounted),
        covered_fact_ids=tuple(fact_id for fact_id in fact_order if fact_id in covered_facts),
        uncovered_fact_ids=tuple(fact_id for fact_id in fact_order if fact_id in known_uncovered),
        covered_story_ids=tuple(
            story_id for story_id in (story_supports or {}) if story_id in covered_stories
        ),
        uncovered_story_ids=tuple(
            story_id for story_id in (story_supports or {}) if story_id in known_uncovered_stories
        ),
    )
