"""Pure CollectionSchedulePolicy tests: no database, no queue, no clock.

The policy decides whether an interval-collected source is due at a given
instant. Rules under test (Plan 2):

* a source with no checkpoint is due immediately;
* a checkpoint without a successful scan yet is due;
* backoff suppression wins BEFORE interval evaluation:
  ``backoff_until > scheduled_at`` means not due even when stale;
* otherwise due iff ``last_success_at + interval <= scheduled_at``;
* the interval comes from ``source.collector_options["schedule"]["interval_minutes"]``
  and falls back to 45 minutes when absent or malformed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.domain.sources import Source
from src.ingestion.models import CollectionCheckpoint

UTC = timezone.utc

SUCCESS_AT = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


def _source(interval_minutes: int | None = None) -> Source:
    collector_options = (
        {} if interval_minutes is None else {"schedule": {"interval_minutes": interval_minutes}}
    )
    return Source(
        id=1,
        platform="telegram",
        kind="channel",
        external_id="@example",
        url=None,
        name="Example",
        role="local_media",
        enabled=True,
        collector_options=collector_options,
        created_at=SUCCESS_AT,
        updated_at=SUCCESS_AT,
    )


def _checkpoint(
    *,
    last_success_at: datetime | None = SUCCESS_AT,
    backoff_until: datetime | None = None,
) -> CollectionCheckpoint:
    return CollectionCheckpoint(last_success_at=last_success_at, backoff_until=backoff_until)


def _scheduled(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 20, hour, minute, tzinfo=UTC)


@pytest.mark.unit
def test_source_without_checkpoint_is_due_immediately():
    from src.ingestion.schedule import CollectionSchedulePolicy

    assert CollectionSchedulePolicy().is_due(_source(45), None, _scheduled(10, 0))


@pytest.mark.unit
def test_checkpoint_without_success_is_due():
    from src.ingestion.schedule import CollectionSchedulePolicy

    checkpoint = _checkpoint(last_success_at=None)
    assert CollectionSchedulePolicy().is_due(_source(45), checkpoint, _scheduled(10, 0))


@pytest.mark.unit
def test_interval_boundary_one_minute_before_is_not_due():
    """Success at 10:00 with a 45-minute interval: 10:44 is still inside."""
    from src.ingestion.schedule import CollectionSchedulePolicy

    assert not CollectionSchedulePolicy().is_due(_source(45), _checkpoint(), _scheduled(10, 44))


@pytest.mark.unit
def test_interval_boundary_at_interval_is_due():
    """Success at 10:00 with a 45-minute interval: due exactly at 10:45."""
    from src.ingestion.schedule import CollectionSchedulePolicy

    assert CollectionSchedulePolicy().is_due(_source(45), _checkpoint(), _scheduled(10, 45))


@pytest.mark.unit
def test_active_backoff_suppresses_before_policy_evaluation():
    """backoff_until in the future wins even when the interval long elapsed."""
    from src.ingestion.schedule import CollectionSchedulePolicy

    checkpoint = _checkpoint(backoff_until=datetime(2026, 8, 21, 11, 0, tzinfo=UTC))
    assert not CollectionSchedulePolicy().is_due(_source(45), checkpoint, _scheduled(10, 30))


@pytest.mark.unit
def test_expired_backoff_falls_through_to_policy():
    from src.ingestion.schedule import CollectionSchedulePolicy

    checkpoint = _checkpoint(backoff_until=_scheduled(10, 0))
    assert CollectionSchedulePolicy().is_due(_source(45), checkpoint, _scheduled(10, 45))


@pytest.mark.unit
def test_missing_collector_options_fall_back_to_default_45_minutes():
    from src.ingestion.schedule import CollectionSchedulePolicy

    source = _source(None)
    assert not CollectionSchedulePolicy().is_due(source, _checkpoint(), _scheduled(10, 44))
    assert CollectionSchedulePolicy().is_due(source, _checkpoint(), _scheduled(10, 45))


@pytest.mark.unit
def test_custom_interval_from_collector_options_is_honored():
    from src.ingestion.schedule import CollectionSchedulePolicy

    source = _source(30)
    assert not CollectionSchedulePolicy().is_due(source, _checkpoint(), _scheduled(10, 29))
    assert CollectionSchedulePolicy().is_due(source, _checkpoint(), _scheduled(10, 30))


@pytest.mark.unit
def test_malformed_interval_falls_back_to_default():
    from src.ingestion.schedule import CollectionSchedulePolicy

    source = Source(
        id=1,
        platform="telegram",
        kind="channel",
        external_id="@example",
        url=None,
        name="Example",
        role="local_media",
        enabled=True,
        collector_options={"schedule": {"interval_minutes": "soon"}},
        created_at=SUCCESS_AT,
        updated_at=SUCCESS_AT,
    )
    # Default 45 minutes applies: 10:44 not due, 10:45 due.
    assert not CollectionSchedulePolicy().is_due(source, _checkpoint(), _scheduled(10, 44))
    assert CollectionSchedulePolicy().is_due(source, _checkpoint(), _scheduled(10, 45))


@pytest.mark.unit
def test_naive_datetimes_are_treated_as_utc():
    from src.ingestion.schedule import CollectionSchedulePolicy

    naive_success = SUCCESS_AT.replace(tzinfo=None)
    naive_scheduled = _scheduled(10, 45).replace(tzinfo=None)
    checkpoint = _checkpoint(last_success_at=naive_success)
    assert CollectionSchedulePolicy().is_due(_source(45), checkpoint, naive_scheduled)


@pytest.mark.unit
def test_default_interval_constant_matches_configured_default():
    from src.ingestion.schedule import DEFAULT_INTERVAL_MINUTES

    assert DEFAULT_INTERVAL_MINUTES == 45


@pytest.mark.unit
def test_schedule_interval_timedelta_math_is_exact():
    """Guard against second/microsecond drift around the boundary."""
    from src.ingestion.schedule import CollectionSchedulePolicy

    just_under = SUCCESS_AT + timedelta(minutes=45) - timedelta(seconds=1)
    exact = SUCCESS_AT + timedelta(minutes=45)
    checkpoint = _checkpoint()
    policy = CollectionSchedulePolicy()
    assert not policy.is_due(_source(45), checkpoint, just_under)
    assert policy.is_due(_source(45), checkpoint, exact)
