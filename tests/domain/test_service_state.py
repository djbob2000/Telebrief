from __future__ import annotations

import pytest

from src.domain.event_payload import EvidenceItemPayload
from src.domain.service_state import ServiceStatePayload


def test_service_state_round_trips_current_water_outage():
    raw = {
        "subject_key": "water_supply",
        "subject_label": "Водоснабжение",
        "dimension": "availability",
        "state": "UNAVAILABLE",
        "location": "верхние этажи",
        "entity": "",
        "expected_now": True,
        "basis": "direct_failure",
    }

    state = ServiceStatePayload.from_dict(raw)

    assert state.subject_key == "water_supply"
    assert state.state == "UNAVAILABLE"
    assert state.expected_now is True
    assert state.basis == "direct_failure"
    assert state.to_dict() == raw


def test_expected_now_is_not_implicitly_true():
    state = ServiceStatePayload.from_dict(
        {
            "subject_key": "heating",
            "subject_label": "Отопление",
            "dimension": "availability",
            "state": "UNKNOWN",
            "basis": "direct_failure",
        }
    )

    assert state.expected_now is None


def test_scheduled_state_requires_valid_timestamp_when_provided():
    with pytest.raises(ValueError, match="effective_from"):
        ServiceStatePayload.from_dict(
            {
                "subject_key": "transport",
                "subject_label": "Транспорт",
                "dimension": "availability",
                "state": "SCHEDULED",
                "basis": "scheduled_change",
                "expected_now": False,
                "effective_from": "not-a-date",
            }
        )


def test_effective_until_cannot_be_earlier_than_effective_from():
    with pytest.raises(ValueError, match="effective_until"):
        ServiceStatePayload.from_dict(
            {
                "subject_key": "transport",
                "subject_label": "Транспорт",
                "dimension": "availability",
                "state": "SCHEDULED",
                "basis": "scheduled_change",
                "effective_from": "2026-08-31T12:00:00+03:00",
                "effective_until": "2026-08-31T10:00:00+03:00",
            }
        )


def test_evidence_item_round_trips_nested_service_state():
    item = EvidenceItemPayload.from_dict(
        {
            "text": "Water does not reach upper floors",
            "kind": "service_access",
            "publication_use": "PUBLISH",
            "source_fragment_ids": [11],
            "service_state": {
                "subject_key": "water_supply",
                "subject_label": "Водоснабжение",
                "dimension": "availability",
                "state": "UNAVAILABLE",
                "location": "upper floors",
                "entity": "",
                "expected_now": True,
                "basis": "direct_failure",
            },
        }
    )

    assert item.service_state is not None
    assert item.service_state.subject_key == "water_supply"
    assert item.to_dict()["service_state"]["state"] == "UNAVAILABLE"
