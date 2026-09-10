from unittest.mock import AsyncMock, patch

import pytest

from src.jobs.event_processing import coalesce_dirty_stories_task


@pytest.mark.asyncio
async def test_registered_legacy_coalesce_task_delegates_to_bounded_authority():
    bounded_batch = AsyncMock()
    with patch(
        "src.jobs.event_authority.process_background_authority_batch.func",
        bounded_batch,
    ):
        result = await coalesce_dirty_stories_task.func(edition_id=7)

    assert result == {"delegated": 1}
    bounded_batch.assert_awaited_once_with(7)
