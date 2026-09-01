from __future__ import annotations

import pytest

from src.domain.event_payload import EventPayload, EvidenceItemPayload
from src.domain.service_state import ServiceStatePayload
from src.processing.operational_semantics import (
    derive_operational_observations,
    normalize_service_state_evidence,
)


def _service_item(
    *,
    text: str,
    fid: int,
    subject_key: str,
    subject_label: str,
    state: str,
    expected_now: bool | None,
    basis: str,
    kind: str = "service_access",
    publication_use: str = "PUBLISH",
    effective_from: str | None = None,
    effective_until: str | None = None,
) -> EvidenceItemPayload:
    return EvidenceItemPayload(
        text=text,
        kind=kind,  # type: ignore[arg-type]
        publication_use=publication_use,  # type: ignore[arg-type]
        source_fragment_ids=(fid,),
        service_state=ServiceStatePayload(
            subject_key=subject_key,
            subject_label=subject_label,
            dimension="availability",
            state=state,
            expected_now=expected_now,
            basis=basis,  # type: ignore[arg-type]
            effective_from=effective_from,
            effective_until=effective_until,
        ),
    )


def test_valid_water_service_state_projects_exactly_once():
    payload = EventPayload(
        evidence_items=(
            _service_item(
                text="Water is absent on upper floors",
                fid=1,
                subject_key="water_supply",
                subject_label="Water supply",
                state="UNAVAILABLE",
                expected_now=True,
                basis="direct_failure",
            ),
        )
    )

    normalized, audit = normalize_service_state_evidence(payload)
    observations = derive_operational_observations(normalized)

    assert audit.accepted_count == 1
    assert audit.rejected_count == 0
    assert len(observations) == 1
    assert observations[0].subject_key == "water_supply"
    assert observations[0].detail == "Water is absent on upper floors"
    assert observations[0].source_fragment_ids == (1,)


def test_negative_state_without_expected_now_is_not_operational():
    payload = EventPayload(
        evidence_items=(
            _service_item(
                text="Residents say central heating is absent",
                fid=2,
                subject_key="heating",
                subject_label="Heating",
                state="UNAVAILABLE",
                expected_now=None,
                basis="direct_failure",
            ),
        )
    )

    normalized, audit = normalize_service_state_evidence(payload)

    assert audit.rejected_count == 1
    assert normalized.evidence_items[0].service_state is None
    assert derive_operational_observations(normalized) == ()


@pytest.mark.parametrize(
    ("state", "basis", "expected_now"),
    [
        ("AVAILABLE", "normal_operation", True),
        ("UNAVAILABLE", "direct_failure", True),
        ("DEGRADED", "degraded_access", True),
        ("RESTRICTED", "explicit_restriction", True),
    ],
)
def test_valid_state_basis_pairs_survive(state: str, basis: str, expected_now: bool | None):
    item = _service_item(
        text=f"Service status update for {state}",
        fid=10,
        subject_key="public_transport",
        subject_label="Транспорт",
        state=state,
        expected_now=expected_now,
        basis=basis,
    )
    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))
    assert audit.accepted_count == 1
    assert audit.rejected_count == 0
    assert normalized.evidence_items[0].service_state is not None


@pytest.mark.parametrize(
    ("state", "basis"),
    [
        ("AVAILABLE", "direct_failure"),
        ("UNAVAILABLE", "normal_operation"),
        ("DEGRADED", "scheduled_change"),
    ],
)
def test_incompatible_state_basis_is_rejected(state: str, basis: str):
    item = _service_item(
        text="Service mismatch text",
        fid=11,
        subject_key="water_supply",
        subject_label="Водоснабжение",
        state=state,
        expected_now=True if state != "AVAILABLE" else None,
        basis=basis,
    )
    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))
    assert audit.rejected_count == 1
    assert normalized.evidence_items[0].service_state is None


def test_scheduled_requires_effective_from():
    item = _service_item(
        text="Scheduled maintenance",
        fid=12,
        subject_key="power_supply",
        subject_label="Электроснабжение",
        state="SCHEDULED",
        expected_now=False,
        basis="scheduled_change",
        effective_from=None,
    )
    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))
    assert audit.rejected_count == 1
    assert normalized.evidence_items[0].service_state is None


def test_private_generator_use_is_demoted_not_dropped():
    item = _service_item(
        text="Residents run their household generators at night",
        fid=3,
        subject_key="backup_power",
        subject_label="Backup power",
        state="AVAILABLE",
        expected_now=True,
        basis="normal_operation",
    )
    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))

    result = normalized.evidence_items[0]
    assert audit.rejected_count == 1
    assert result.kind == "community_report"
    assert result.publication_use == "PUBLISH"
    assert result.service_state is None
    assert result.text == item.text


def test_generator_mechanism_does_not_kill_explicit_water_outcome():
    item = _service_item(
        text="The building generator powers the pump, so water is available daily",
        fid=4,
        subject_key="water_supply",
        subject_label="Water supply",
        state="AVAILABLE",
        expected_now=True,
        basis="normal_operation",
    )
    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))

    assert audit.rejected_count == 0
    assert normalized.evidence_items[0].service_state is not None


def test_provider_connectivity_evidence_cannot_project_as_city_power_supply():
    item = _service_item(
        text="Provider equipment is offline and internet connectivity is unavailable",
        fid=5,
        subject_key="power_supply",
        subject_label="Electricity",
        state="UNAVAILABLE",
        expected_now=True,
        basis="direct_failure",
    )

    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))

    assert audit.rejected_count == 1
    assert normalized.evidence_items[0].service_state is None


def test_ambiguous_unrecognized_subject_is_not_guessed():
    item = _service_item(
        text="Service X is unavailable today",
        fid=6,
        subject_key="service_x",
        subject_label="Service X",
        state="UNAVAILABLE",
        expected_now=True,
        basis="direct_failure",
    )

    normalized, audit = normalize_service_state_evidence(EventPayload(evidence_items=(item,)))

    assert audit.rejected_count == 0
    assert normalized.evidence_items[0].service_state is not None


def test_retail_commodity_sale_is_excluded_from_operational_observations():
    from src.processing.operational_semantics import derive_operational_observations

    item = _service_item(
        text="Вода на розлив по 3 ₽/литр в киоске на Восточном",
        fid=7,
        subject_key="water",
        subject_label="Вода на розлив",
        state="AVAILABLE",
        expected_now=True,
        basis="normal_operation",
    )
    payload = EventPayload(evidence_items=(item,))
    observations = derive_operational_observations(payload)
    assert len(observations) == 0
