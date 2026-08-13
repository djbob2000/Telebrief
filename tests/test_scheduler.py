"""Tests for scheduled digest jobs."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scheduler import DigestScheduler


@pytest.mark.unit
def test_start_registers_one_daily_job(sample_config, mock_logger):
    """The scheduler registers only the configured daily digest when article is disabled."""
    sample_config.settings.article.enabled = False
    scheduler = DigestScheduler(sample_config, mock_logger)
    scheduler.scheduler = MagicMock()
    scheduler.scheduler.get_job.return_value = MagicMock(
        next_run_time=datetime(2026, 8, 7, 9, tzinfo=timezone.utc)
    )

    scheduler.start()

    assert scheduler.scheduler.add_job.call_count == 1
    calls = scheduler.scheduler.add_job.call_args_list
    assert [call.kwargs["id"] for call in calls] == ["daily_digest"]
    assert "args" not in calls[0].kwargs


@pytest.mark.asyncio
async def test_scheduled_job_uses_global_lookback(sample_config, mock_logger):
    """The scheduled job uses the global lookback window."""
    scheduler = DigestScheduler(sample_config, mock_logger)

    with patch(
        "src.scheduler.generate_and_send_digest", new=AsyncMock(return_value=True)
    ) as generate:
        await scheduler._scheduled_digest_job()

    generate.assert_awaited_once_with(config=sample_config, logger=mock_logger, hours=24)


@pytest.mark.unit
def test_start_registers_article_job_when_enabled(sample_config, mock_logger):
    """The scheduler registers daily_article job when article.enabled is True."""
    sample_config.settings.article.enabled = True
    sample_config.settings.article.schedule_time = "20:00"

    scheduler = DigestScheduler(sample_config, mock_logger)
    scheduler.scheduler = MagicMock()
    scheduler.scheduler.get_job.return_value = MagicMock(
        next_run_time=datetime(2026, 8, 7, 20, tzinfo=timezone.utc)
    )

    scheduler.start()

    assert scheduler.scheduler.add_job.call_count == 2
    calls = scheduler.scheduler.add_job.call_args_list
    job_ids = [call.kwargs["id"] for call in calls]
    assert "daily_digest" in job_ids
    assert "daily_article" in job_ids


@pytest.mark.asyncio
async def test_scheduled_article_job_runs_workflow(sample_config, mock_logger):
    """The scheduled article job triggers generate_and_publish_article."""
    scheduler = DigestScheduler(sample_config, mock_logger)

    with patch(
        "src.core.generate_and_publish_article", new=AsyncMock(return_value=True)
    ) as generate_art:
        await scheduler._scheduled_article_job()

    generate_art.assert_awaited_once_with(config=sample_config, logger=mock_logger, hours=24)
