"""Tests for the publication scheduler facade and schedule calculation."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from src.jobs.schedules import due_publication_actions
from src.scheduler import DigestScheduler, format_next_configured_digest_time


@pytest.mark.unit
def test_digest_scheduler_lifecycle(sample_config, mock_logger):
    """Test start, stop and status representation of the DigestScheduler facade."""
    scheduler = DigestScheduler(sample_config, mock_logger)
    assert not scheduler.is_running
    assert scheduler.get_next_run_time() == "Scheduler not running"

    scheduler.start()
    assert scheduler.is_running
    next_time = scheduler.get_next_run_time()
    assert next_time != "Scheduler not running"

    # Repeated start does not raise
    scheduler.start()
    assert scheduler.is_running

    scheduler.stop()
    assert not scheduler.is_running
    assert scheduler.get_next_run_time() == "Scheduler not running"


@pytest.mark.unit
def test_format_next_configured_digest_time(sample_config):
    """Next configured digest slot formats reliably."""
    sample_config.settings.schedule_time = "08:30"
    sample_config.settings.timezone = "Europe/Kyiv"

    tz = ZoneInfo("Europe/Kyiv")
    now_before = dt.datetime(2026, 8, 23, 6, 0, tzinfo=tz)
    result = format_next_configured_digest_time(sample_config, now=now_before)
    assert "08:30:00" in result

    now_after = dt.datetime(2026, 8, 23, 10, 0, tzinfo=tz)
    result_tomorrow = format_next_configured_digest_time(sample_config, now=now_after)
    assert "2026-08-24 08:30:00" in result_tomorrow


@pytest.mark.unit
def test_due_publication_actions_dispatch(sample_config):
    """due_publication_actions generates correct publication actions at matching slots."""
    sample_config.settings.schedule_time = "09:00"
    sample_config.settings.timezone = "UTC"
    sample_config.settings.article.enabled = True
    sample_config.settings.article.schedule_time = "20:00"
    sample_config.settings.pre_publish_lead_minutes = 15

    # At 09:00 UTC -> digest due
    tick_9 = dt.datetime(2026, 8, 23, 9, 0, tzinfo=dt.timezone.utc)
    actions = due_publication_actions(sample_config, tick_9)
    assert any(
        a.kind == "publish" and a.task_kwargs.get("publication_type") == "digest_grouped"
        for a in actions
    )

    # At 08:45 UTC -> pre-publish due
    tick_845 = dt.datetime(2026, 8, 23, 8, 45, tzinfo=dt.timezone.utc)
    actions_pre = due_publication_actions(sample_config, tick_845)
    assert any(a.kind == "pre_publish" for a in actions_pre)

    # At 20:00 UTC -> article due
    tick_20 = dt.datetime(2026, 8, 23, 20, 0, tzinfo=dt.timezone.utc)
    actions_art = due_publication_actions(sample_config, tick_20)
    assert any(
        a.kind == "publish" and a.task_kwargs.get("publication_type") == "daily_article"
        for a in actions_art
    )
