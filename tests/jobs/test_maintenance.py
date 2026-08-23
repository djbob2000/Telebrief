"""Tests for periodic maintenance jobs (Plan 5 Task 10)."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.jobs.maintenance import retention_cleanup, retry_stalled_jobs


class TestMaintenanceJobs:
    """Unit tests for periodic maintenance task registration and execution."""

    def test_task_queueing_locks(self):
        assert retry_stalled_jobs.queueing_lock == "retry_stalled_jobs"
        assert retention_cleanup.queueing_lock == "retention-cleanup"

    @pytest.mark.asyncio
    async def test_retention_cleanup_triggers_service(self):
        ts = int(dt.datetime(2026, 8, 23, 3, 15, tzinfo=dt.timezone.utc).timestamp())
        mock_runtime = MagicMock()
        mock_runtime.uow = MagicMock()
        with (
            patch("src.runtime.get_runtime", return_value=mock_runtime),
            patch("src.retention.RetentionService.cleanup", new_callable=AsyncMock) as mock_cleanup,
        ):
            await retention_cleanup(timestamp=ts)
            mock_cleanup.assert_called_once()
            called_now = mock_cleanup.call_args.kwargs.get("now") or mock_cleanup.call_args[0][0]
            assert called_now.hour == 3
            assert called_now.minute == 15
