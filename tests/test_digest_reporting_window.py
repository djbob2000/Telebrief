# ruff: noqa: S101
from __future__ import annotations

import datetime as dt
from dataclasses import replace

from test_digest_source_material import _evidence

from src.publication import digest_reporting_context

SNAPSHOT = dt.datetime(2026, 10, 6, 13, tzinfo=dt.timezone.utc)


def mark(items, *, hours=24):
    annotate = getattr(digest_reporting_context, "annotate_digest_reporting_window", None)
    assert callable(annotate), "Digest needs source window roles independent of event dates"
    return annotate(items, snapshot_at=SNAPSHOT, lookback_hours=hours)


def test_old_source_is_preserved_as_historical_without_new_event_date():
    source = replace(
        _evidence(1, "На Павлова сейчас нет света.", "На Павлова сейчас нет света."),
        observed_at=SNAPSHOT - dt.timedelta(days=6),
    )
    annotated = mark({source.evidence_id: source})
    result = annotated[source.evidence_id]
    assert result.text == source.text
    assert result.source_text == source.source_text
    assert result.publication_use == "PUBLISH"
    assert result.reporting_window_role == "historical_source"
    metadata = digest_reporting_context.publish_support_metadata(annotated)[source.source_ref]
    assert metadata["reporting_window_role"] == "historical_source"
    assert "observed_at" not in metadata


def test_current_window_source_does_not_prove_persistence_until_publication():
    source = replace(
        _evidence(1, "На Павлова нет света.", "На Павлова нет света."),
        observed_at=SNAPSHOT - dt.timedelta(hours=2),
    )
    result = mark({source.evidence_id: source})[source.evidence_id]
    assert result.reporting_window_role == "current_window_source"
    assert result.text == source.text


def test_frozen_window_length_is_used_instead_of_fixed_one_day():
    source = replace(
        _evidence(1, "На Павлова нет света.", "На Павлова нет света."),
        observed_at=SNAPSHOT - dt.timedelta(hours=30),
    )
    assert (
        mark({source.evidence_id: source})[source.evidence_id].reporting_window_role
        == "historical_source"
    )
    assert (
        mark({source.evidence_id: source}, hours=48)[source.evidence_id].reporting_window_role
        == "current_window_source"
    )
