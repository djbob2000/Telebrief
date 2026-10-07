"""Readonly owners of protected details in an existing frozen digest inventory.

This is a narrow actual-prose check, not a truth/corroboration service or a
complete semantic-entailment engine. Unresolved bindings stay explicit.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.domain.service_taxonomy import (
    SERVICE_FAMILY_STEMS,
    detect_service_families,
    matches_any_stem,
    semantic_tokens,
)
from src.publication.article_claims import _stem, extract_concrete_claims
from src.publication.digest_composition import DigestFactRecord
from src.publication.digest_reporting_context import (
    fare_comparison_context,
    source_switch_clock_context,
)

_CLOCK = re.compile(r"\d{1,2}:\d{2}")
_STREET = re.compile(r"\b(?:улиц\w*|ул\.)", re.IGNORECASE)
_PARTITION = re.compile(
    r"\b(?:на|в)\s+(?:всей\s+)?остальн\w*\s+(?:территори\w*|части|улиц\w*)", re.IGNORECASE
)
_PARTITION_SUPPORT = re.compile(
    r"\b(?:остальн\w*|других\s+улиц\w*|кроме|за\s+исключением)\b", re.IGNORECASE
)
_SEQUENCE = re.compile(r"\b(?:после\s+этого|затем|позже)\b", re.IGNORECASE)
_STATE_CHANGE = re.compile(
    r"\b(?:восстанов\w*|включ\w*|отключ\w*|появил\w*|пропал\w*|дали)\b",
    re.IGNORECASE,
)
_SOURCE_ORDER = re.compile(
    r"\b(?:после\s+этого|затем|позже|потом|сначала|вчера|сегодня|накануне|теперь)\b|"
    r"\d{1,2}[:.]\d{2}|\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)",
    re.IGNORECASE,
)
_PAID_DESTINATION = re.compile(
    r"\b(?:билет\w*|проезд|поездка)\s+(?:из\s+[А-Яа-яЁё-]+\s+)?(?:до|в)\s+"
    r"(?P<destination>[А-Яа-яЁё-]+)(?P<tail>[^,.;\n]*)",
    re.IGNORECASE,
)
_PAID_ROUTE = re.compile(
    r"\b(?:билет\w*|проезд|поездка)\s+(?:на\s+автобус\s+|по\s+маршруту\s+)?"
    r"[А-Яа-яЁё-]+\s*[-–—]\s*(?P<destination>[А-Яа-яЁё-]+)(?P<tail>[^,.;\n]*)",
    re.IGNORECASE,
)
_SUPPLY_DURATION = re.compile(
    r"\b(?:подача|свет|электричество|электроснабжение)\b[^.!?;\n]{0,70}?"
    r"\b(?:продолж\w*|был[аои]?)\s+(?:около\s+|примерно\s+)?"
    r"(?P<hours>\d{1,2})\s+час(?:а|ов)?\b",
    re.IGNORECASE,
)
_AMOUNT = re.compile(r"(?<!\d)(\d[\d\s]*?)\s+рубл(?:ей|я|ь)\b", re.IGNORECASE)
_MAJORITY_CITY_SCOPE = re.compile(
    r"\b(?:у\s+)?(?:большей\s+части|большая\s+часть|большую\s+часть)\s+"
    r"(?P<target>[А-Яа-яЁё-]+)\b",
    re.IGNORECASE,
)
_WHOLE_CITY_SCOPE = re.compile(
    r"\b(?:весь|всего|всей|всем|во\s+всём|во\s+всем|по\s+всему)\s+"
    r"(?P<target>[А-Яа-яЁё-]+)\b",
    re.IGNORECASE,
)
_GEOGRAPHIC_SCOPE_TARGET_STEMS = (
    "город",
    "район",
    "област",
    "регион",
    "территори",
    "микрорайон",
    "посел",
    "село",
)
_SCOPE_ATTRIBUTION_BRIDGE = re.compile(
    r"(?:по\s+(?:словам|сообщению|данным)\s+(?:его|ее|их|[А-Яа-яЁё-]+(?:\s+[А-Яа-яЁё-]+){0,2})"
    r"|как\s+(?:сообщает|сообщают|пишет|пишут|говорит|уточняет)\s+[А-Яа-яЁё-]+)",
    re.IGNORECASE,
)


def _clocks(text: str) -> tuple[str, ...]:
    values = []
    for risk in extract_concrete_claims(text):
        if risk.kind == "time" and _CLOCK.fullmatch(risk.raw.strip()):
            hour, minute = map(int, risk.raw.split(":"))
            if hour < 24 and minute < 60:
                values.append(f"{hour:02d}:{minute:02d}")
    return tuple(dict.fromkeys(values))


def _amount(value: str) -> str:
    return re.sub(r"\D", "", value)


def _city_scope_level(text: str) -> int:
    """Return only explicit majority/whole-city scope stated in the text."""
    for pattern, level in ((_WHOLE_CITY_SCOPE, 2), (_MAJORITY_CITY_SCOPE, 1)):
        match = next(
            (
                found
                for found in pattern.finditer(text)
                # «не во всём районе» states partial, not whole-area, scope.
                if not re.search(r"\bне\s*$", text[: found.start()], re.IGNORECASE)
            ),
            None,
        )
        if match is None:
            continue
        target = match.group("target")
        if target[:1].isupper() or any(
            target.casefold().startswith(stem) for stem in _GEOGRAPHIC_SCOPE_TARGET_STEMS
        ):
            return level
    return 0


def _entry_service_families(entry: DigestEvidenceEntry) -> frozenset[str]:
    """Resolve the fact's service without treating causal mentions as ownership."""
    canonical_service = str(entry.record.canonical_service or "").strip()
    if canonical_service and canonical_service != "local_report":
        return detect_service_families(canonical_service)

    # Some ordinary report facts have no service classification (or use the
    # generic local_report label), even when their own text explicitly names
    # one service. Let that fact own matching service-scope claims only when
    # the text names exactly one family. Ambiguous multi-service text may
    # mention a service causally and must not widen another fact's scope.
    fact_services = detect_service_families(entry.record.text)
    if len(fact_services) != 1:
        return frozenset()
    return fact_services


def _city_scope_service_claims(text: str) -> tuple[tuple[int, frozenset[str]], ...]:
    """Bind explicit citywide scope to services in its local clause.

    A single chat message can mention several services while qualifying only
    one of them, e.g. ``нет газа, света, у большей части города воды``. A
    scope phrase must not be applied to every service found in the whole
    message. Simple comma-separated service lists remain connected.
    """
    claims: list[tuple[int, frozenset[str]]] = []
    for sentence in re.split(r"(?<=[.!?;])\s+", text):
        pending_scope = 0
        list_scope = 0
        for clause in re.split(r",\s+", sentence):
            if re.match(r"\s*(?:а|но|однако|зато)\b", clause, re.IGNORECASE):
                pending_scope = 0
                list_scope = 0
            local_scope = _city_scope_level(clause)
            if local_scope:
                pending_scope = max(pending_scope, local_scope)
            services = detect_service_families(clause)
            if services:
                is_list_continuation = _is_service_enumeration(clause)
                scope_level = max(
                    pending_scope,
                    list_scope if is_list_continuation and not local_scope else 0,
                )
                if scope_level:
                    claims.append((scope_level, services))
                pending_scope = 0
                list_scope = scope_level
            elif pending_scope:
                if not _SCOPE_ATTRIBUTION_BRIDGE.fullmatch(clause.strip()):
                    pending_scope = 0
                    list_scope = 0
            else:
                # Keep a just-stated extent only through a bare service list;
                # dates, new predicates, locations, and other prose start a
                # separate clause and cannot inherit it.
                if not _is_service_enumeration(clause):
                    list_scope = 0
    return tuple(claims)


def _is_service_enumeration(text: str) -> bool:
    tokens = semantic_tokens(text)
    if not tokens:
        return False
    return all(
        token in {"и", "или"}
        or any(matches_any_stem([token], stems) for stems in SERVICE_FAMILY_STEMS.values())
        for token in tokens
    )


@dataclass(frozen=True)
class DigestEvidenceEntry:
    record: DigestFactRecord
    source_texts: tuple[str, ...]
    complete: bool

    @property
    def clocks(self) -> tuple[str, ...]:
        return _clocks(self.record.text)

    def writer_details(self) -> dict[str, Any]:
        """Keep values attached to this record, not a block-wide bag of numbers."""
        return {
            "clock_observations": list(self.clocks),
            "switch_clock_roles": source_switch_clock_context(self.source_texts),
            "fare_comparisons": fare_comparison_context(self.source_texts),
            "scope": {
                "original_location": self.record.original_location,
                "canonical_area": self.record.canonical_area,
                "canonical_place": list(self.record.canonical_place),
                "service_state": self.record.service_state,
                "epistemic_kind": self.record.epistemic_kind,
            },
        }


@dataclass(frozen=True)
class DigestBindingFinding:
    code: str
    fact_ids: tuple[str, ...]
    field: str
    reason: str

    def diagnostic(self) -> str:
        return f"{self.code}:{self.field}:{','.join(self.fact_ids)}: {self.reason}"


@dataclass(frozen=True)
class DigestBindingAssessment:
    violations: tuple[DigestBindingFinding, ...]
    not_evaluated: tuple[str, ...]


@dataclass(frozen=True)
class DigestEvidenceLedger:
    entries: tuple[DigestEvidenceEntry, ...]

    @classmethod
    def from_records(
        cls, records: Sequence[DigestFactRecord], support_text_by_id: Mapping[str, str]
    ) -> DigestEvidenceLedger:
        entries = []
        seen = set()
        for record in records:
            if record.fact_id in seen:
                raise ValueError("duplicate fact identity in digest evidence ledger")
            seen.add(record.fact_id)
            texts = tuple(
                dict.fromkeys(
                    support_text_by_id[sid]
                    for sid in record.support_ids
                    if support_text_by_id.get(sid, "").strip()
                )
            )
            complete = bool(record.support_ids) and all(
                support_text_by_id.get(sid, "").strip() for sid in record.support_ids
            )
            entries.append(DigestEvidenceEntry(record, texts, complete))
        return cls(tuple(entries))

    def check_visible(self, text: str, *, resolver: Any = None) -> DigestBindingAssessment:
        violations = []
        unresolved = []
        complete = [entry for entry in self.entries if entry.complete]
        if len(complete) != len(self.entries):
            unresolved.append("DIGEST_LEDGER_SUPPORT_INCOMPLETE")

        def add(
            field: str,
            entries: Sequence[DigestEvidenceEntry],
            reason: str,
            *,
            relation: bool = False,
        ) -> None:
            violations.append(
                DigestBindingFinding(
                    "UNSUPPORTED_DIGEST_RELATION" if relation else "DIGEST_FACT_BINDING_MISMATCH",
                    tuple(entry.record.fact_id for entry in entries),
                    field,
                    reason,
                )
            )

        def scope(value: str) -> tuple[frozenset[str], frozenset[str]]:
            from src.publication.digest_presentation import _resolved_geographic_scopes

            if resolver is None:
                return frozenset(), frozenset()
            annotation = resolver.resolve(value)
            places = frozenset(
                str(entity.entity_id)
                for entity in getattr(annotation, "entities", ())
                if entity.kind == "place" and entity.confidence == "high"
            )
            return places, frozenset(_resolved_geographic_scopes(value, resolver))

        def entry_scope(entry: DigestEvidenceEntry) -> tuple[frozenset[str], frozenset[str]]:
            places, areas = scope(entry.record.original_location or entry.record.text)
            return frozenset(entry.record.canonical_place) or places, areas

        # A supplied clock hour must not become a duration of supply. This
        # closed grammar checks an explicit switch-time role, not all numbers.
        for claim in _SUPPLY_DURATION.finditer(text):
            owners = [
                entry
                for entry in complete
                if any(
                    row["hour"] == claim["hours"]
                    for row in source_switch_clock_context(entry.source_texts)
                )
            ]
            if not owners or len(complete) != len(self.entries):
                continue
            supported_duration = any(
                match["hours"] == claim["hours"]
                for entry in complete
                for source in entry.source_texts
                for match in _SUPPLY_DURATION.finditer(source)
            )
            if not supported_duration:
                add(
                    "clock_duration",
                    owners,
                    "a supplied switch time was rewritten as a supply duration",
                )

        # Fare destination is a role, not merely the presence of both city names.
        # Accept a real alternative price record rather than borrowing another
        # record's same-value/different-destination comparison.
        for claim in _PAID_DESTINATION.finditer(text):
            amount_match = _AMOUNT.search(claim["tail"])
            if amount_match is None:
                continue
            value = _amount(amount_match[1])
            comparisons = [
                (entry, comparison)
                for entry in complete
                for comparison in fare_comparison_context(entry.source_texts)
                if value
                in {
                    _amount(comparison["ordinary_fare"]),
                    _amount(comparison["passing_bus_fare_for_same_leg"]),
                }
            ]
            if not comparisons or len(complete) != len(self.entries):
                continue
            destination = _stem(claim["destination"].casefold())
            allowed = {
                _stem(re.split(r"\s*[-–—]\s*", comparison["paid_leg"])[-1].casefold())
                for _, comparison in comparisons
            }
            if destination in allowed:
                continue
            # Another source may explicitly contain this paid destination and
            # amount. In that case the conservative parser cannot disprove it.
            other_support = any(
                _stem(m["destination"].casefold()) == destination
                and (price := _AMOUNT.search(m["tail"])) is not None
                and _amount(price[1]) == value
                for entry in complete
                for source in entry.source_texts
                for pattern in (_PAID_DESTINATION, _PAID_ROUTE)
                for m in pattern.finditer(source)
            )
            if not other_support:
                add(
                    "fare_destination",
                    [entry for entry, _ in comparisons],
                    "The supplied price belongs to a different paid journey leg; "
                    "passing-bus destinations do not change that leg.",
                )

        for sentence in re.split(r"(?<=[.!?;])\s+", text):
            # Never borrow a place from a contrast clause after the clock.
            for clause in re.split(
                r",\s*(?:а|но|тогда как|однако)\s+", sentence, flags=re.IGNORECASE
            ):
                for value in _clocks(clause):
                    owners = [entry for entry in self.entries if value in entry.clocks]
                    if len(owners) != 1 or not owners[0].complete:
                        unresolved.append(f"DIGEST_LEDGER_CLOCK_OWNER_NOT_EVALUATED:{value}")
                        continue
                    owner = owners[0]
                    owner_places, owner_areas = entry_scope(owner)
                    places, areas = scope(clause)
                    # Different municipal/colloquial views cannot establish a
                    # geographic contradiction merely through disjoint IDs.
                    shared_views = {area.split(":", 1)[0] for area in areas}.intersection(
                        area.split(":", 1)[0] for area in owner_areas
                    )
                    comparable_areas = {
                        area for area in areas if area.split(":", 1)[0] in shared_views
                    }
                    comparable_owner_areas = {
                        area for area in owner_areas if area.split(":", 1)[0] in shared_views
                    }
                    if len(places) > 1 or len(areas) > 1:
                        unresolved.append(f"DIGEST_LEDGER_CLOCK_LOCATION_NOT_EVALUATED:{value}")
                        continue
                    if (places and owner_places and places.isdisjoint(owner_places)) or (
                        comparable_areas
                        and comparable_owner_areas
                        and comparable_areas.isdisjoint(comparable_owner_areas)
                    ):
                        add(
                            "clock_location",
                            [owner],
                            "The clock observation belongs to another known location.",
                        )
                    elif (
                        (places or _STREET.search(clause))
                        and owner_areas
                        and not owner_places
                        and not any(
                            _STREET.search(s)
                            for s in (*owner.source_texts, owner.record.original_location)
                        )
                        and not any(scope(source)[0] for source in owner.source_texts)
                    ):
                        add(
                            "clock_street_scope",
                            [owner],
                            "The clock report names an area, not a particular street; remove the invented street scope.",
                        )
                    services = detect_service_families(clause)
                    owner_services = detect_service_families(owner.record.text)
                    if len(services) == len(owner_services) == 1 and services != owner_services:
                        add(
                            "clock_service",
                            [owner],
                            "The clock observation belongs to a different service.",
                        )

        # Closed relation cases only: incomplete context never proves absence.
        if self.entries and len(complete) == len(self.entries):
            texts = [source for entry in complete for source in entry.source_texts]

            # Scope words belong to a service-specific report. A citywide
            # water statement cannot widen a separate gas or power observation
            # merely because both facts share one reader-facing item.
            for sentence in re.split(r"(?<=[.!?;])\s+", text):
                for claim_scope, services in _city_scope_service_claims(sentence):
                    for service in services:
                        owners = [
                            entry for entry in complete if service in _entry_service_families(entry)
                        ]
                        if not owners:
                            unresolved.append(
                                f"DIGEST_LEDGER_SERVICE_SCOPE_NOT_EVALUATED:{service}"
                            )
                            continue
                        # The clause-level parser already binds a source's scope to
                        # the service it names. A fact whose text mentions two
                        # services (run 315: «пропала и вода. Значит весь город
                        # обесточен») can still state citywide power itself.
                        source_scope_supported = any(
                            service in source_services and source_scope >= claim_scope
                            for entry in complete
                            for source in entry.source_texts
                            for source_scope, source_services in _city_scope_service_claims(source)
                        )
                        if not source_scope_supported:
                            add(
                                "service_extent",
                                owners,
                                "The source does not state this citywide extent for the named "
                                f"service ({service}). Keep the citywide scope only for services "
                                "the cited source wording itself names; state this service "
                                "with its own reported places.",
                            )

            for sentence in re.split(r"(?<=[.!?;])\s+", text):
                if not _PARTITION.search(sentence):
                    continue
                services = detect_service_families(sentence)
                if len(services) != 1:
                    unresolved.append("DIGEST_LEDGER_PARTITION_NOT_EVALUATED")
                    continue
                relevant = [
                    entry
                    for entry in complete
                    if detect_service_families(entry.record.text) == services
                ]
                relevant_texts = [
                    source
                    for entry in relevant
                    for source in entry.source_texts
                    if detect_service_families(source) == services
                ]
                if (
                    len(relevant) > 1
                    and any(_STREET.search(source) for source in relevant_texts)
                    and not any(_PARTITION_SUPPORT.search(source) for source in relevant_texts)
                ):
                    add(
                        "area_partition",
                        relevant,
                        "Separate area/street reports do not establish conditions on the rest of the area.",
                        relation=True,
                    )
            if (
                len(complete) == 2
                and _SEQUENCE.search(text)
                and {entry.record.service_state for entry in complete}
                == {"AVAILABLE", "UNAVAILABLE"}
                and all(entry.record.effective_time is None for entry in complete)
                and not any(_SOURCE_ORDER.search(source) for source in texts)
            ):
                scopes = [entry_scope(entry) for entry in complete]
                sequence_services = [
                    detect_service_families(entry.record.text) for entry in complete
                ]
                if (
                    scopes[0] == scopes[1]
                    and any(scopes[0])
                    and len(sequence_services[0]) == 1
                    and sequence_services[0] == sequence_services[1]
                    and any(
                        _STATE_CHANGE.search(clause := re.split(r"[.!?;]", text[m.end() :])[0])
                        and detect_service_families(clause) == sequence_services[0]
                        for m in _SEQUENCE.finditer(text)
                    )
                ):
                    add(
                        "unproven_sequence",
                        complete,
                        "Conflicting untimed reports do not prove a restoration sequence; keep their uncertainty.",
                        relation=True,
                    )
        return DigestBindingAssessment(
            tuple(dict.fromkeys(violations)), tuple(dict.fromkeys(unresolved))
        )
