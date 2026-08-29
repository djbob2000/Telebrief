"""Unit tests for the canonical EventPayload and OperationalObservationPayload."""

from __future__ import annotations

import pytest

from src.domain.event_payload import (
    OPERATIONAL_STATES,
    EventPayload,
    OperationalObservationPayload,
    parse_event_payload,
)


def test_operational_observation_valid() -> None:
    obs = OperationalObservationPayload.from_dict(
        {
            "subject_key": "water_supply",
            "subject_label": "Water supply",
            "dimension": "availability",
            "location": "Koloniya",
            "entity": "",
            "state": "UNAVAILABLE",
            "detail": "Residents report another outage",
            "source_fragment_ids": [101],
        },
        allowed_fragment_ids={101, 102},
    )
    assert obs.state == "UNAVAILABLE"
    assert obs.source_fragment_ids == (101,)
    assert obs.subject_key == "water_supply"
    assert obs.location == "Koloniya"

    d = obs.to_dict()
    assert d["state"] == "UNAVAILABLE"
    assert d["source_fragment_ids"] == [101]


def test_operational_observation_rejects_conflicting_or_invalid_state() -> None:
    with pytest.raises(ValueError, match="state"):
        OperationalObservationPayload.from_dict(
            {
                "subject_key": "water_supply",
                "subject_label": "Water supply",
                "dimension": "availability",
                "location": "Koloniya",
                "entity": "",
                "state": "CONFLICTING",  # resolver output, not valid input observation
                "detail": "...",
                "source_fragment_ids": [101],
            }
        )

    with pytest.raises(ValueError, match="state"):
        OperationalObservationPayload.from_dict(
            {
                "subject_key": "water_supply",
                "subject_label": "Water supply",
                "dimension": "availability",
                "location": "Koloniya",
                "entity": "",
                "state": "SOMETHING_ELSE",
                "detail": "...",
                "source_fragment_ids": [101],
            }
        )


def test_operational_observation_rejects_empty_or_unknown_fragment_ids() -> None:
    with pytest.raises(ValueError, match="source_fragment_ids"):
        OperationalObservationPayload.from_dict(
            {
                "subject_key": "water_supply",
                "subject_label": "Water supply",
                "dimension": "availability",
                "location": "Koloniya",
                "entity": "",
                "state": "AVAILABLE",
                "detail": "...",
                "source_fragment_ids": [],
            }
        )

    with pytest.raises(ValueError, match="allowed_fragment_ids"):
        OperationalObservationPayload.from_dict(
            {
                "subject_key": "water_supply",
                "subject_label": "Water supply",
                "dimension": "availability",
                "location": "Koloniya",
                "entity": "",
                "state": "AVAILABLE",
                "detail": "...",
                "source_fragment_ids": [999],
            },
            allowed_fragment_ids={101, 102},
        )


def test_event_payload_backward_compatibility() -> None:
    old = EventPayload.from_dict({"headline": "Old rich story", "publishability": "news"})
    assert old.enrichment_level == "analysis"
    assert old.headline == "Old rich story"
    assert isinstance(old.tags, tuple)
    assert isinstance(old.key_facts, tuple)
    assert old.operational_observations == ()


def test_event_payload_brief_roundtrip() -> None:
    data = {
        "topic": "Electricity outage",
        "tags": ["power", "center"],
        "urgency": "normal",
        "publishability": "brief",
        "headline": "Outage in Center",
        "digest_summary": "Electricity is temporarily cut in the city center.",
        "enrichment_level": "brief",
        "operational_observations": [
            {
                "subject_key": "power_supply",
                "subject_label": "Power supply",
                "dimension": "availability",
                "location": "Center",
                "entity": "grid",
                "state": "UNAVAILABLE",
                "detail": "Outage in center",
                "source_fragment_ids": [101],
            }
        ],
    }
    payload = parse_event_payload(data)
    assert payload.enrichment_level == "brief"
    assert len(payload.operational_observations) == 1
    assert payload.operational_observations[0].state == "UNAVAILABLE"

    d = payload.to_dict()
    assert d["enrichment_level"] == "brief"
    assert d["operational_observations"][0]["state"] == "UNAVAILABLE"
    assert d["operational_observations"][0]["source_fragment_ids"] == [101]


def test_all_operational_states() -> None:
    assert OPERATIONAL_STATES == {
        "AVAILABLE",
        "UNAVAILABLE",
        "DEGRADED",
        "RESTRICTED",
        "UNKNOWN",
    }
