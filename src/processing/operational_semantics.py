"""Canonical service state normalization and derived operational observations."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from src.domain.event_payload import (
    EventPayload,
    EvidenceItemPayload,
    OperationalObservationPayload,
)

_ALLOWED_BASIS_BY_STATE: dict[str, frozenset[str]] = {
    "AVAILABLE": frozenset({"normal_operation"}),
    "UNAVAILABLE": frozenset({"direct_failure", "explicit_restriction"}),
    "DEGRADED": frozenset({"direct_failure", "degraded_access"}),
    "RESTRICTED": frozenset({"degraded_access", "explicit_restriction"}),
    "SCHEDULED": frozenset({"scheduled_change"}),
    "UNKNOWN": frozenset(
        {
            "normal_operation",
            "direct_failure",
            "degraded_access",
            "explicit_restriction",
            "scheduled_change",
        }
    ),
}

# Generic stems for private actor coping detector (language agnostic / RU + EN)
_PRIVATE_ACTOR_STEMS: frozenset[str] = frozenset(
    {
        "жител",
        "сосед",
        "горожан",
        "люд",
        "дом",
        "квартир",
        "resident",
        "neighbor",
        "household",
        "private",
        "people",
    }
)

_COPING_ACTION_STEMS: frozenset[str] = frozenset(
    {
        "скинул",
        "скидыва",
        "купил",
        "покупа",
        "запустил",
        "запуска",
        "включил",
        "включа",
        "заряжа",
        "зарядил",
        "запас",
        "запаса",
        "подключил",
        "подключа",
        "использ",
        "поставил",
        "ставят",
        "use",
        "using",
        "run",
        "running",
        "buy",
        "bought",
        "pool",
        "pooled",
        "charge",
        "charging",
        "stock",
        "stocking",
        "connect",
        "connected",
    }
)

_COPING_RESOURCE_STEMS: frozenset[str] = frozenset(
    {
        "генератор",
        "аккумулятор",
        "павербанк",
        "повербанк",
        "powerbank",
        "батаре",
        "скважин",
        "солнечн",
        "generator",
        "battery",
        "solar",
        "well",
    }
)

_SERVICE_OUTCOME_STEMS: frozenset[str] = frozenset(
    {
        "вода",
        "водн",
        "водоснабжен",
        "интернет",
        "связь",
        "провайдер",
        "банк",
        "банкомат",
        "транспорт",
        "автобус",
        "маршрутк",
        "лифт",
        "газ",
        "отоплен",
        "тепло",
        "почт",
        "доставк",
        "water",
        "internet",
        "connectivity",
        "telecom",
        "bank",
        "banking",
        "transport",
        "bus",
        "lift",
        "elevator",
        "gas",
        "heating",
        "delivery",
    }
)

# Generic service families for subject conflict detection
_SERVICE_FAMILY_STEMS: dict[str, frozenset[str]] = {
    "power": frozenset(
        {
            "свет",
            "электр",
            "энерг",
            "лэп",
            "питани",
            "power",
            "electric",
            "electricity",
            "blackout",
            "power_supply",
        }
    ),
    "water": frozenset(
        {
            "вод",
            "водопровод",
            "водоснабж",
            "скважин",
            "water",
            "aqueduct",
            "water_supply",
        }
    ),
    "gas": frozenset({"газ", "газоснабж", "газопровод", "gas", "gas_supply"}),
    "heating": frozenset({"отоплен", "тепл", "котельн", "heat", "heating"}),
    "lift": frozenset({"лифт", "подъемник", "lift", "elevator"}),
    "telecom": frozenset(
        {
            "интернет",
            "связ",
            "провайдер",
            "сеть",
            "wi-fi",
            "wifi",
            "telecom",
            "internet",
            "cellular",
            "mobile",
            "internet_connectivity",
        }
    ),
    "banking": frozenset(
        {"банк", "банкомат", "терминал", "платеж", "bank", "atm", "banking", "payment"}
    ),
    "transport": frozenset(
        {
            "транспорт",
            "автобус",
            "маршрут",
            "поезд",
            "трамвай",
            "троллейбус",
            "рейс",
            "проезд",
            "transport",
            "bus",
            "train",
            "public_transport",
        }
    ),
    "logistics": frozenset({"доставк", "почт", "курьер", "parcel", "postal", "delivery"}),
    "municipal": frozenset({"жэк", "мусор", "вывоз", "коммунал", "municipal", "waste", "garbage"}),
}


def _semantic_tokens(text: str) -> list[str]:
    clean = re.sub(r"[^\w\s-]", " ", text.casefold().replace("ё", "е"))
    return [token for token in clean.split() if token]


def _matches_any_stem(tokens: list[str], stems: frozenset[str]) -> bool:
    for token in tokens:
        for stem in stems:
            if stem in token or token.startswith(stem):
                return True
    return False


def _is_high_confidence_private_coping(text: str) -> bool:
    tokens = _semantic_tokens(text)
    return (
        _matches_any_stem(tokens, _PRIVATE_ACTOR_STEMS)
        and _matches_any_stem(tokens, _COPING_ACTION_STEMS)
        and _matches_any_stem(tokens, _COPING_RESOURCE_STEMS)
        and not _matches_any_stem(tokens, _SERVICE_OUTCOME_STEMS)
    )


def _detect_service_families(text: str) -> frozenset[str]:
    tokens = _semantic_tokens(text)
    detected: set[str] = set()
    for family, stems in _SERVICE_FAMILY_STEMS.items():
        if _matches_any_stem(tokens, stems):
            detected.add(family)
    return frozenset(detected)


@dataclass(frozen=True)
class ServiceStateAudit:
    """Audit metadata from service-state evidence normalization."""

    accepted_count: int = 0
    rejected_count: int = 0
    rejected_evidence_indexes: tuple[int, ...] = ()
    rejection_reasons: tuple[str, ...] = ()


def normalize_service_state_evidence(
    payload: EventPayload,
) -> tuple[EventPayload, ServiceStateAudit]:
    """Validate and normalize service_state projections on EvidenceItemPayloads."""
    accepted = 0
    rejected_indexes: list[int] = []
    rejection_reasons: list[str] = []
    normalized_items: list[EvidenceItemPayload] = []

    for index, item in enumerate(payload.evidence_items):
        if item.service_state is None:
            normalized_items.append(item)
            continue

        # If non-service_access carries service_state, strip it
        if item.kind != "service_access" or item.publication_use != "PUBLISH":
            normalized_items.append(replace(item, service_state=None))
            rejected_indexes.append(index)
            rejection_reasons.append("non_publish_service_access_state")
            continue

        state = item.service_state

        # Check high-confidence private coping false-positive
        if _is_high_confidence_private_coping(item.text):
            normalized_items.append(
                replace(
                    item,
                    kind="community_report",
                    service_state=None,
                )
            )
            rejected_indexes.append(index)
            rejection_reasons.append("private_coping_demoted")
            continue

        # Check negative current state requires expected_now is True
        if (
            state.state in {"UNAVAILABLE", "DEGRADED", "RESTRICTED"}
            and state.expected_now is not True
        ):
            normalized_items.append(replace(item, service_state=None))
            rejected_indexes.append(index)
            rejection_reasons.append("negative_state_not_expected_now")
            continue

        # Check SCHEDULED requires effective_from
        if state.state == "SCHEDULED" and not state.effective_from:
            normalized_items.append(replace(item, service_state=None))
            rejected_indexes.append(index)
            rejection_reasons.append("scheduled_without_effective_from")
            continue

        # Check state/basis compatibility
        allowed_bases = _ALLOWED_BASIS_BY_STATE.get(state.state, frozenset())
        if state.basis not in allowed_bases:
            normalized_items.append(replace(item, service_state=None))
            rejected_indexes.append(index)
            rejection_reasons.append("state_basis_mismatch")
            continue

        # Check subject-family conflict
        subject_families = _detect_service_families(f"{state.subject_key} {state.subject_label}")
        evidence_families = _detect_service_families(item.text)
        if (
            subject_families
            and evidence_families
            and subject_families.isdisjoint(evidence_families)
        ):
            normalized_items.append(replace(item, service_state=None))
            rejected_indexes.append(index)
            rejection_reasons.append("subject_family_conflict")
            continue

        accepted += 1
        normalized_items.append(item)

    normalized_payload = replace(payload, evidence_items=tuple(normalized_items))
    audit = ServiceStateAudit(
        accepted_count=accepted,
        rejected_count=len(rejected_indexes),
        rejected_evidence_indexes=tuple(rejected_indexes),
        rejection_reasons=tuple(rejection_reasons),
    )
    return normalized_payload, audit


def derive_operational_observations(
    payload: EventPayload,
) -> tuple[OperationalObservationPayload, ...]:
    """Deterministically map validated service_state evidence to OperationalObservationPayloads."""
    observations: list[OperationalObservationPayload] = []
    for item in payload.evidence_items:
        state = item.service_state
        if item.kind != "service_access" or item.publication_use != "PUBLISH" or state is None:
            continue
        observations.append(
            OperationalObservationPayload(
                subject_key=state.subject_key,
                subject_label=state.subject_label,
                dimension=state.dimension,
                location=state.location,
                entity=state.entity,
                state=state.state,
                detail=item.text,
                source_fragment_ids=item.source_fragment_ids,
                effective_from=state.effective_from,
                effective_until=state.effective_until,
            )
        )
    return tuple(observations)


def has_unstructured_publish_service_access(payload: EventPayload) -> bool:
    """Check if any PUBLISH service_access item lacks structured service_state."""
    return any(
        item.kind == "service_access"
        and item.publication_use == "PUBLISH"
        and item.service_state is None
        for item in payload.evidence_items
    )
