"""Concrete-value binding for article prose (article-fidelity W2.1).

A concrete value (a clock time, a day ordinal, a count, a duration) must be
owned by a citable support whose own text carries it. Before this check the
claim validator compared each word and number with the union of all cited
supports, so a service word from one message and a time from another could be
combined into a fact that no single source states.

This is a narrow deterministic check, not a semantic-entailment engine:

- a clause's value is owned by the PUBLISH, non-question supports whose own
  ``text`` or ``source_text`` carry it (reply-parent context never owns a value);
- a clause is blocked only when its single owner carries a disjoint service
  family, or a disjoint place inside a view both sides use;
- anything else (several owners, no owner, undetermined service or place) is a
  ``NOT_EVALUATED`` diagnostic. It never blocks publication by itself.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from src.domain.service_taxonomy import detect_service_families
from src.publication.article_claims import _NUMBER_WORD_DIGITS
from src.publication.article_context import ArticleSupport, article_support_theme_hints
from src.publication.article_geography import (
    resolve_article_place_area_map,
    resolve_article_place_names,
)

CONCRETE_VALUE_MISBOUND: Final = "CONCRETE_VALUE_MISBOUND"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE_SPLIT_RE = re.compile(r";\s*|,\s*(?:а|но|тогда как|однако)\s+", re.IGNORECASE)
_CLOCK_RE = re.compile(r"(?<![\d:.,])(\d{1,2})[:.](\d{2})(?![\d])")
_DIGITS_RE = re.compile(r"(?<![\w:.,])(\d{1,4})(?![\d:.,]\d)")
# A number word counts as a value only before a time or count unit («двух часов»,
# «третий день»); «одна из жительниц» is not a concrete value.
_NUMBER_WORD_RE = re.compile(
    r"\b(?P<word>"
    + "|".join(sorted(_NUMBER_WORD_DIGITS, key=len, reverse=True))
    + r")\b(?=\s+(?:час\w*|ч\b|мин\w*|дн\w*|день|сутк\w*|недел\w*|месяц\w*|утр\w*|вечер\w*|ноч\w*)\b)",
    re.IGNORECASE,
)

_WORDS_BY_VALUE: dict[str, tuple[str, ...]] = {
    digit: tuple(word for word, value in _NUMBER_WORD_DIGITS.items() if value == digit)
    for digit in set(_NUMBER_WORD_DIGITS.values())
}


@dataclass(frozen=True)
class ValueBindingFinding:
    """A blocking evidence-boundary finding, shaped like other sentence-level issues."""

    code: Literal["CONCRETE_VALUE_MISBOUND"]
    unit_id: str
    claim_text: str
    support_ids: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class ValueBindingDiagnostic:
    """A non-blocking NOT_EVALUATED record: the binding could not be decided."""

    unit_id: str
    value: str
    reason: Literal["no_owner", "several_owners", "service_undetermined", "place_unresolved"]


@dataclass(frozen=True)
class ValueBindingResult:
    findings: tuple[ValueBindingFinding, ...] = ()
    diagnostics: tuple[ValueBindingDiagnostic, ...] = ()

    def not_evaluated_codes(self) -> tuple[str, ...]:
        return tuple(
            f"CONCRETE_VALUE_NOT_EVALUATED:{d.unit_id}:{d.value}:{d.reason}"
            for d in self.diagnostics
        )


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _clauses(sentence: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_SPLIT_RE.split(sentence) if c and c.strip()]


def clause_values(clause: str) -> tuple[str, ...]:
    """Concrete values in one clause: clock times, number words before units, digits."""
    values: list[str] = []
    masked = clause
    for match in _CLOCK_RE.finditer(clause):
        hour, minute = int(match.group(1)), match.group(2)
        if hour < 24:
            values.append(f"{hour}:{minute}")
        masked = masked.replace(match.group(0), " " * len(match.group(0)))
    for match in _NUMBER_WORD_RE.finditer(masked):
        values.append(_NUMBER_WORD_DIGITS[match.group("word").lower()])
    masked = _NUMBER_WORD_RE.sub(lambda m: " " * len(m.group(0)), masked)
    for match in _DIGITS_RE.finditer(masked):
        values.append(str(int(match.group(1))))
    return tuple(dict.fromkeys(values))


def _value_in_text(value: str, text: str) -> bool:
    """Whether a source text carries the value, as digits, a clock or (for integers) a word."""
    if ":" in value:
        hour, minute = value.split(":")
        if re.search(rf"(?<![\d:.,]){hour}[:.]{minute}(?!\d)", text):
            return True
        # «в 10 утра» carries 10:00; a non-zero minute needs the exact clock.
        return minute == "00" and re.search(rf"(?<![\d:.,]){hour}(?![\d:.,])", text) is not None
    if re.search(rf"(?<![\d:.,]){value}(?![\d:.,]\d)", text):
        return True
    words = _WORDS_BY_VALUE.get(value, ())
    if not words:
        return False
    alternation = "|".join(re.escape(word) for word in words)
    return re.search(rf"\b(?:{alternation})\b", text.casefold().replace("ё", "е")) is not None


def _owner_texts(support: ArticleSupport) -> tuple[str, ...]:
    """Own text only: a reply parent is name/context and never owns a value."""
    return tuple(text for text in (support.text, support.source_text) if text)


def _owner_families(support: ArticleSupport) -> frozenset[str]:
    families = set(article_support_theme_hints(support))
    if support.source_text:
        families.update(detect_service_families(support.source_text))
    return frozenset(families)


def _place_relation(
    clause: str, owner_texts: Sequence[str], place_resolver: Any | None
) -> Literal["compatible", "mismatch", "unknown"]:
    if place_resolver is None:
        return "unknown"
    clause_places = resolve_article_place_names(clause, place_resolver)
    owner_places: set[str] = set()
    for text in owner_texts:
        owner_places |= resolve_article_place_names(text, place_resolver)
    if not clause_places or not owner_places:
        return "unknown"
    if not clause_places.isdisjoint(owner_places):
        return "compatible"

    # Distinct names can still belong to one area. Compare verified area groups
    # only inside a view that both sides use: a municipal view and a colloquial
    # view do not prove different physical places.
    clause_areas: set[str] = set()
    for areas in resolve_article_place_area_map(clause, place_resolver).values():
        clause_areas |= areas
    owner_areas: set[str] = set()
    for text in owner_texts:
        for areas in resolve_article_place_area_map(text, place_resolver).values():
            owner_areas |= areas
    shared_views = {a.split(":", 1)[0] for a in clause_areas} & {
        a.split(":", 1)[0] for a in owner_areas
    }
    if not shared_views:
        return "unknown"
    clause_scoped = {a for a in clause_areas if a.split(":", 1)[0] in shared_views}
    owner_scoped = {a for a in owner_areas if a.split(":", 1)[0] in shared_views}
    return "mismatch" if clause_scoped.isdisjoint(owner_scoped) else "compatible"


def check_concrete_value_binding(
    unit_id: str,
    unit_text: str,
    unit_supports: Sequence[ArticleSupport],
    *,
    place_resolver: Any | None = None,
) -> ValueBindingResult:
    """Bind every concrete clause value of a unit to its own owning support."""
    owners_pool = [
        s
        for s in unit_supports
        if s.publication_use == "PUBLISH" and s.evidence_kind != "resident_question"
    ]
    findings: dict[str, ValueBindingFinding] = {}
    diagnostics: dict[tuple[str, str], ValueBindingDiagnostic] = {}

    def record(
        value: str,
        reason: Literal["no_owner", "several_owners", "service_undetermined", "place_unresolved"],
    ) -> None:
        diagnostics.setdefault((value, reason), ValueBindingDiagnostic(unit_id, value, reason))

    for sentence in _sentences(unit_text):
        if sentence in findings:
            continue
        for clause in _clauses(sentence):
            for value in clause_values(clause):
                owners = [
                    s
                    for s in owners_pool
                    if any(_value_in_text(value, text) for text in _owner_texts(s))
                ]
                if not owners:
                    record(value, "no_owner")
                    continue
                if len(owners) > 1:
                    record(value, "several_owners")
                    continue
                owner = owners[0]
                clause_families = frozenset(detect_service_families(clause))
                owner_families = _owner_families(owner)
                if clause_families and owner_families:
                    service = "compatible" if clause_families & owner_families else "mismatch"
                else:
                    service = "unknown"
                place = _place_relation(clause, _owner_texts(owner), place_resolver)
                if service == "mismatch" or place == "mismatch":
                    findings[sentence] = ValueBindingFinding(
                        code=CONCRETE_VALUE_MISBOUND,
                        unit_id=unit_id,
                        claim_text=sentence,
                        support_ids=(owner.support_id,),
                        message=(
                            f"Unit {unit_id} binds concrete value '{value}' to a support that names "
                            "a different service or place than the clause does"
                        ),
                    )
                    break
                if service == "unknown":
                    record(value, "service_undetermined")
                if (
                    place == "unknown"
                    and place_resolver is not None
                    and (resolve_article_place_names(clause, place_resolver))
                ):
                    record(value, "place_unresolved")
            if sentence in findings:
                break

    return ValueBindingResult(
        findings=tuple(findings.values()),
        diagnostics=tuple(diagnostics.values()),
    )
