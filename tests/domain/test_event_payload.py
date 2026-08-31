"""Tests for canonical EventPayload, EvidenceItemPayload, and OperationalObservationPayload."""

from __future__ import annotations

import pytest

from src.domain.event_payload import (
    OPERATIONAL_STATES,
    EventPayload,
    EvidenceItemPayload,
    OperationalObservationPayload,
    parse_event_payload,
)


def test_evidence_item_payload_valid() -> None:
    item = EvidenceItemPayload.from_dict(
        {
            "text": "Electricity cut in center",
            "kind": "established_fact",
            "publication_use": "PUBLISH",
            "source_fragment_ids": [101, 102],
        },
        allowed_fragment_ids={101, 102, 103},
    )
    assert item.text == "Electricity cut in center"
    assert item.kind == "established_fact"
    assert item.publication_use == "PUBLISH"
    assert item.source_fragment_ids == (101, 102)

    d = item.to_dict()
    assert d["text"] == "Electricity cut in center"
    assert d["source_fragment_ids"] == [101, 102]


def test_evidence_item_payload_defaults() -> None:
    item_comm = EvidenceItemPayload.from_dict(
        {
            "text": "Discount 20% on hair salon",
            "kind": "commercial_offer",
            "source_fragment_ids": [201],
        }
    )
    assert item_comm.publication_use == "EXCLUDE"

    item_fact = EvidenceItemPayload.from_dict(
        {
            "text": "Water restored",
            "kind": "community_report",
            "source_fragment_ids": [202],
        }
    )
    assert item_fact.publication_use == "PUBLISH"


def test_evidence_item_payload_validation_errors() -> None:
    # Empty text
    with pytest.raises(ValueError, match="text"):
        EvidenceItemPayload.from_dict(
            {"text": "", "kind": "established_fact", "source_fragment_ids": [101]}
        )

    # Unknown kind
    with pytest.raises(ValueError, match="kind"):
        EvidenceItemPayload.from_dict(
            {"text": "abc", "kind": "invalid_kind", "source_fragment_ids": [101]}
        )

    # Unknown publication_use
    with pytest.raises(ValueError, match="publication_use"):
        EvidenceItemPayload.from_dict(
            {
                "text": "abc",
                "kind": "established_fact",
                "publication_use": "UNKNOWN",
                "source_fragment_ids": [101],
            }
        )

    # Empty source_fragment_ids
    with pytest.raises(ValueError, match="source_fragment_ids"):
        EvidenceItemPayload.from_dict(
            {"text": "abc", "kind": "established_fact", "source_fragment_ids": []}
        )

    # Fragment ID not in allowed_fragment_ids
    with pytest.raises(ValueError, match="allowed_fragment_ids"):
        EvidenceItemPayload.from_dict(
            {"text": "abc", "kind": "established_fact", "source_fragment_ids": [999]},
            allowed_fragment_ids={101, 102},
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
                "state": "CONFLICTING",
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


def test_operational_observation_payload_effective_time() -> None:
    obs = OperationalObservationPayload.from_dict(
        {
            "subject_key": "gas_supply",
            "subject_label": "Газоснабжение",
            "dimension": "availability",
            "location": "Николаевка",
            "entity": "ГРП",
            "state": "SCHEDULED",
            "detail": "Плановое отключение 30 августа",
            "source_fragment_ids": [301],
            "effective_from": "2026-08-30T08:00:00+00:00",
            "effective_until": "2026-08-30T17:00:00+00:00",
        }
    )
    assert obs.state == "SCHEDULED"
    assert obs.effective_from == "2026-08-30T08:00:00+00:00"
    assert obs.effective_until == "2026-08-30T17:00:00+00:00"

    d = obs.to_dict()
    assert d["state"] == "SCHEDULED"
    assert d["effective_from"] == "2026-08-30T08:00:00+00:00"
    assert d["effective_until"] == "2026-08-30T17:00:00+00:00"


def test_operational_observation_rejects_invalid_time_range() -> None:
    with pytest.raises(ValueError, match="effective_until"):
        OperationalObservationPayload.from_dict(
            {
                "subject_key": "gas_supply",
                "subject_label": "Газоснабжение",
                "dimension": "availability",
                "location": "Николаевка",
                "entity": "ГРП",
                "state": "SCHEDULED",
                "detail": "Invalid time",
                "source_fragment_ids": [301],
                "effective_from": "2026-08-30T17:00:00+00:00",
                "effective_until": "2026-08-30T08:00:00+00:00",
            }
        )


def test_event_payload_backward_compatibility() -> None:
    old = EventPayload.from_dict({"headline": "Old rich story", "publishability": "news"})
    assert old.enrichment_level == "analysis"
    assert old.headline == "Old rich story"
    assert isinstance(old.tags, tuple)
    assert isinstance(old.key_facts, tuple)
    assert old.operational_observations == ()
    assert old.evidence_items == ()


def test_event_payload_with_evidence_items_roundtrip() -> None:
    data = {
        "topic": "Power outage in Center",
        "headline": "Center power outage",
        "digest_summary": "Electricity cut in center",
        "enrichment_level": "brief",
        "evidence_items": [
            {
                "text": "Center substation outage",
                "kind": "established_fact",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [101],
            }
        ],
        "operational_observations": [
            {
                "subject_key": "power_supply",
                "subject_label": "Power supply",
                "dimension": "availability",
                "location": "Center",
                "entity": "grid",
                "state": "UNAVAILABLE",
                "detail": "Substation outage",
                "source_fragment_ids": [101],
            }
        ],
    }
    payload = parse_event_payload(data)
    assert len(payload.evidence_items) == 1
    assert payload.evidence_items[0].text == "Center substation outage"
    assert payload.evidence_items[0].publication_use == "PUBLISH"

    d = payload.to_dict()
    assert len(d["evidence_items"]) == 1
    assert d["evidence_items"][0]["kind"] == "established_fact"


def test_all_operational_states() -> None:
    assert OPERATIONAL_STATES == {
        "AVAILABLE",
        "UNAVAILABLE",
        "DEGRADED",
        "RESTRICTED",
        "UNKNOWN",
        "SCHEDULED",
    }


def test_event_payload_to_dict_omits_operational_observations_but_from_dict_tolerates() -> None:
    legacy_data = {
        "headline": "Legacy story",
        "digest_summary": "Summary",
        "operational_observations": [
            {
                "subject_key": "water_supply",
                "subject_label": "Water",
                "dimension": "availability",
                "location": "City",
                "entity": "grid",
                "state": "AVAILABLE",
                "detail": "Working",
                "source_fragment_ids": [101],
            }
        ],
    }
    payload = EventPayload.from_dict(legacy_data)
    assert len(payload.operational_observations) == 1
    assert payload.operational_observations[0].subject_key == "water_supply"

    d = payload.to_dict()
    assert "operational_observations" not in d
