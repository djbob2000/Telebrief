"""Tests for core module."""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest

from src.config_loader import ChannelConfig, FilterSpec, ForumTopicConfig, StorageConfig
from src.core import (
    _apply_filters,
    _build_digest_parts,
    _channel_config_for_name,
    _collect_messages,
    _resolve_channel,
    build_digest,
    collect_channel_messages,
    generate_and_send_digest,
    read_last_digest,
    validate_hours,
)
from src.db.pool import close_pool, open_pool
from src.db.uow import DatabaseUnitOfWork
from src.grouper import GroupedPoint
from src.runtime import install_runtime


@pytest.mark.unit
def test_channel_config_for_name_resolves_forum_topic(sample_config):
    """Collected topic names still resolve to their parent channel config."""
    sample_config.channels = [
        ChannelConfig(
            id="@source",
            name="Source",
            topics=[ForumTopicConfig(id=235525, name="Проблемы ЖКХ")],
        )
    ]

    assert (
        _channel_config_for_name(sample_config, "Source — Проблемы ЖКХ")
        is sample_config.channels[0]
    )


@pytest.mark.unit
def test_core_thematic_fallback_preserves_configured_source_roles():
    from datetime import datetime, timezone

    from src.collector import Message
    from src.core import _build_fallback_article

    official = Message(
        text="Коммунальная служба сообщила: воду отключат до 15:00",
        sender="КП",
        timestamp=datetime(2026, 8, 15, tzinfo=timezone.utc),
        link="https://t.me/utility/1",
        channel_name="Utility",
        has_media=False,
        media_type="",
        message_id=1,
    )
    title, lead, body = _build_fallback_article(
        {"Utility": [official]},
        [ChannelConfig(id="@utility", name="Utility", source_type="official")],
    )

    assert title
    assert lead
    assert "Источник сообщил" in body
    assert "Жители сообщали" not in body


@pytest.fixture(autouse=True)
def _isolate_digest_cache(tmp_path, monkeypatch):
    """Keep the digest cache out of the repo while tests run."""
    monkeypatch.setattr("src.core._DIGEST_CACHE_PATH", tmp_path / "last_digest.json")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_digest_success(sample_config, mock_logger, sample_messages):
    """build_digest returns the header and channel messages joined into one document."""
    sample_config.settings.digest_mode = "channel"

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.Summarizer") as mock_summarizer_class,
        patch("src.core.DigestFormatter") as mock_formatter_class,
    ):
        # Set up mocks
        mock_collector = MagicMock()
        mock_collector.connect = AsyncMock()
        mock_collector.fetch_messages = AsyncMock(return_value={"Test Channel": sample_messages})
        mock_collector.disconnect = AsyncMock()
        mock_collector_class.return_value = mock_collector

        mock_summarizer = MagicMock()
        mock_summarizer.summarize_all = AsyncMock(
            return_value={
                "channel_summaries": {"Test Channel": "Summary"},
                "overview": "Overview",
            }
        )
        mock_summarizer_class.return_value = mock_summarizer

        mock_formatter = MagicMock()
        mock_formatter.format_channel_message = MagicMock(return_value="Channel msg")
        mock_formatter.format_summary_message = MagicMock(return_value="Header")
        mock_formatter_class.return_value = mock_formatter

        # Run function
        digest = await build_digest(sample_config, mock_logger, hours=24)

        # Assertions
        assert digest == "Header\n\nChannel msg"
        mock_collector.connect.assert_called_once()
        mock_collector.fetch_messages.assert_called_once_with(hours=24)
        mock_collector.disconnect.assert_called_once()
        mock_summarizer.summarize_all.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_digest_caches_result(sample_config, mock_logger, sample_messages):
    """A successful build is cached so get_last_digest can serve it later."""
    sample_config.settings.digest_mode = "channel"

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.Summarizer") as mock_summarizer_class,
        patch("src.core.DigestFormatter") as mock_formatter_class,
    ):
        mock_collector_class.return_value = _make_collector_mock(sample_messages)

        mock_summarizer = MagicMock()
        mock_summarizer.summarize_all = AsyncMock(
            return_value={"channel_summaries": {"Test Channel": "Summary"}, "overview": ""}
        )
        mock_summarizer_class.return_value = mock_summarizer

        mock_formatter = MagicMock()
        mock_formatter.format_channel_message = MagicMock(return_value="Channel msg")
        mock_formatter.format_summary_message = MagicMock(return_value="Header")
        mock_formatter_class.return_value = mock_formatter

        assert read_last_digest() is None
        await build_digest(sample_config, mock_logger, hours=12)

    cached = read_last_digest()
    assert cached is not None
    assert cached["text"] == "Header\n\nChannel msg"
    assert cached["hours"] == 12
    assert cached["generated_at"]


@pytest.mark.unit
def test_read_last_digest_ignores_corrupt_cache(tmp_path, monkeypatch):
    """A truncated or non-JSON cache file reads as absent, not as an exception."""
    cache = tmp_path / "corrupt.json"
    cache.write_text('{"text": "half-writ', encoding="utf-8")
    monkeypatch.setattr("src.core._DIGEST_CACHE_PATH", cache)

    assert read_last_digest() is None


@pytest.mark.unit
@pytest.mark.parametrize("hours", [0, -1, 169, 1000, True, 2.5, "24"])
def test_validate_hours_rejects_bad_input(hours):
    """Lookback window outside 1..168 (or not an int) is rejected."""
    with pytest.raises(ValueError):
        validate_hours(hours)


@pytest.mark.unit
@pytest.mark.parametrize("hours", [1, 24, 168])
def test_validate_hours_accepts_valid_input(hours):
    """Sane lookback windows pass validation."""
    validate_hours(hours)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_digest_collector_error(sample_config, mock_logger):
    """Test error handling in message collection."""
    with patch("src.core.MessageCollector") as mock_collector_class:
        mock_collector = MagicMock()
        mock_collector.connect = AsyncMock()
        mock_collector.fetch_messages = AsyncMock(side_effect=Exception("Collection failed"))
        mock_collector.disconnect = AsyncMock()
        mock_collector_class.return_value = mock_collector

        with pytest.raises(Exception, match="Collection failed"):
            await build_digest(sample_config, mock_logger, hours=24)

        # Should still disconnect
        mock_collector.disconnect.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_and_send_digest_success(sample_config, mock_logger, sample_messages):
    """Test successful digest generation and sending."""
    sample_config.settings.digest_mode = "channel"

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.Summarizer") as mock_summarizer_class,
        patch("src.core.DigestFormatter") as mock_formatter_class,
        patch("src.core.DigestSender") as mock_sender_class,
    ):
        # Set up mocks
        mock_collector_class.return_value = _make_collector_mock(sample_messages)

        mock_summarizer = MagicMock()
        mock_summarizer.summarize_all = AsyncMock(
            return_value={
                "channel_summaries": {"Test Channel": "Summary"},
                "overview": "Overview",
            }
        )
        mock_summarizer_class.return_value = mock_summarizer

        mock_formatter = MagicMock()
        mock_formatter.format_channel_message = MagicMock(return_value="Channel msg")
        mock_formatter.format_summary_message = MagicMock(return_value="Header")
        mock_formatter_class.return_value = mock_formatter

        mock_sender = MagicMock()
        mock_sender.cleanup_old_digests = AsyncMock()
        mock_sender.send_channel_messages_with_tracking = AsyncMock(return_value=True)
        mock_sender_class.return_value = mock_sender

        # Run function
        result = await generate_and_send_digest(
            sample_config, mock_logger, hours=24, user_id=123456789
        )

        # Assertions
        assert result is True
        mock_sender.send_channel_messages_with_tracking.assert_called_once_with(
            [("Test Channel", "Channel msg")], "Header", 123456789
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_and_send_digest_send_failure(sample_config, mock_logger, sample_messages):
    """Test handling of send failure."""
    sample_config.settings.digest_mode = "channel"

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.Summarizer") as mock_summarizer_class,
        patch("src.core.DigestFormatter") as mock_formatter_class,
        patch("src.core.DigestSender") as mock_sender_class,
    ):
        # Set up mocks (same as success case)
        mock_collector_class.return_value = _make_collector_mock(sample_messages)

        mock_summarizer = MagicMock()
        mock_summarizer.summarize_all = AsyncMock(
            return_value={
                "channel_summaries": {"Test Channel": "Summary"},
                "overview": "Overview",
            }
        )
        mock_summarizer_class.return_value = mock_summarizer

        mock_formatter = MagicMock()
        mock_formatter.format_channel_message = MagicMock(return_value="Channel msg")
        mock_formatter.format_summary_message = MagicMock(return_value="Header")
        mock_formatter_class.return_value = mock_formatter

        # Send fails
        mock_sender = MagicMock()
        mock_sender.cleanup_old_digests = AsyncMock()
        mock_sender.send_channel_messages_with_tracking = AsyncMock(return_value=False)
        mock_sender_class.return_value = mock_sender

        # Run function
        result = await generate_and_send_digest(sample_config, mock_logger, hours=24)

        # Should return False
        assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_and_send_digest_rejects_bad_hours(sample_config, mock_logger):
    """Invalid lookback windows raise instead of silently returning False."""
    with pytest.raises(ValueError):
        await generate_and_send_digest(sample_config, mock_logger, hours=0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_and_send_digest_grouped_success(
    sample_config, mock_logger, sample_messages
):
    """Test digest-grouped flow calls grouper and formatter correctly."""
    sample_config.settings.digest_mode = "digest"

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.Summarizer") as mock_summarizer_class,
        patch("src.core.DigestGrouper") as mock_grouper_class,
        patch("src.core.DigestFormatter") as mock_formatter_class,
        patch("src.core.DigestSender") as mock_sender_class,
    ):
        # Collector
        mock_collector = MagicMock()
        mock_collector.connect = AsyncMock()
        mock_collector.fetch_messages = AsyncMock(return_value={"Test Channel": sample_messages})
        mock_collector.disconnect = AsyncMock()
        mock_collector_class.return_value = mock_collector

        # Summarizer
        mock_summarizer = MagicMock()
        mock_summarizer.summarize_all = AsyncMock(
            return_value={
                "channel_summaries": {"Test Channel": "- Point A\n- Point B"},
                "overview": "Overview",
            }
        )
        mock_summarizer_class.return_value = mock_summarizer

        # Grouper
        mock_grouper = MagicMock()
        grouped_result = {
            "News": [GroupedPoint(point="Point A", source="Test Channel")],
            "Other": [GroupedPoint(point="Point B", source="Test Channel")],
        }
        mock_grouper.group_summaries = AsyncMock(return_value=grouped_result)
        mock_grouper_class.return_value = mock_grouper

        # Formatter
        mock_formatter = MagicMock()
        mock_formatter.format_group_digest = MagicMock(return_value="Combined digest")
        mock_formatter.format_group_rich_digest = MagicMock(
            return_value={"rich_message": {"blocks": []}}
        )
        mock_formatter_class.return_value = mock_formatter

        # Sender
        mock_sender = MagicMock()
        mock_sender.cleanup_old_digests = AsyncMock()
        mock_sender.send_rich_digest = AsyncMock(return_value=True)
        mock_sender.send_channel_messages_with_tracking = AsyncMock(return_value=True)
        mock_sender_class.return_value = mock_sender

        result = await generate_and_send_digest(
            sample_config, mock_logger, hours=24, user_id=123456789
        )

        assert result is True
        mock_grouper.group_summaries.assert_called_once()
        mock_formatter.format_group_digest.assert_called_once_with(
            [
                (
                    "News",
                    [GroupedPoint(point="Point A", source="Test Channel")],
                ),
                (
                    "Other",
                    [GroupedPoint(point="Point B", source="Test Channel")],
                ),
            ],
            hours=24,
        )
        mock_sender.send_rich_digest.assert_called_once_with(
            {"rich_message": {"blocks": []}},
            user_id=123456789,
            fallback_text="Combined digest",
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_and_send_digest_grouped_skips_empty_groups(
    sample_config, mock_logger, sample_messages
):
    """Test that empty groups produce no message."""
    sample_config.settings.digest_mode = "digest"

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.Summarizer") as mock_summarizer_class,
        patch("src.core.DigestGrouper") as mock_grouper_class,
        patch("src.core.DigestFormatter") as mock_formatter_class,
        patch("src.core.DigestSender") as mock_sender_class,
    ):
        mock_collector = MagicMock()
        mock_collector.connect = AsyncMock()
        mock_collector.fetch_messages = AsyncMock(return_value={"Test Channel": sample_messages})
        mock_collector.disconnect = AsyncMock()
        mock_collector_class.return_value = mock_collector

        mock_summarizer = MagicMock()
        mock_summarizer.summarize_all = AsyncMock(
            return_value={
                "channel_summaries": {"Test Channel": "- Point A"},
                "overview": "Overview",
            }
        )
        mock_summarizer_class.return_value = mock_summarizer

        # Grouper returns one group with points, one empty
        mock_grouper = MagicMock()
        grouped_result = {
            "News": [GroupedPoint(point="Point A", source="Test Channel")],
            "Sport": [],  # empty group
        }
        mock_grouper.group_summaries = AsyncMock(return_value=grouped_result)
        mock_grouper_class.return_value = mock_grouper

        mock_formatter = MagicMock()
        mock_formatter.format_group_digest = MagicMock(return_value="Combined digest")
        mock_formatter.format_group_rich_digest = MagicMock(
            return_value={"rich_message": {"blocks": []}}
        )
        mock_formatter_class.return_value = mock_formatter

        mock_sender = MagicMock()
        mock_sender.cleanup_old_digests = AsyncMock()
        mock_sender.send_rich_digest = AsyncMock(return_value=True)
        mock_sender.send_channel_messages_with_tracking = AsyncMock(return_value=True)
        mock_sender_class.return_value = mock_sender

        result = await generate_and_send_digest(
            sample_config, mock_logger, hours=24, user_id=123456789
        )

        assert result is True
        mock_formatter.format_group_digest.assert_called_once_with(
            [("News", [GroupedPoint(point="Point A", source="Test Channel")])],
            hours=24,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_digest_mode_uses_grouper(sample_config, mock_logger, sample_messages):
    """Mode 'digest' routes through the topic grouper."""
    sample_config.settings.digest_mode = "digest"

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.Summarizer") as mock_summarizer_class,
        patch("src.core.DigestGrouper") as mock_grouper_class,
        patch("src.core.DigestFormatter") as mock_formatter_class,
        patch("src.core.DigestSender") as mock_sender_class,
    ):
        mock_collector_class.return_value = _make_collector_mock(sample_messages)

        mock_summarizer = MagicMock()
        mock_summarizer.summarize_all = AsyncMock(
            return_value={
                "channel_summaries": {"Test Channel": "- Point A"},
                "overview": "Overview",
            }
        )
        mock_summarizer_class.return_value = mock_summarizer

        mock_grouper = MagicMock()
        mock_grouper.group_summaries = AsyncMock(
            return_value={"News": [GroupedPoint(point="Point A", source="Test Channel")]}
        )
        mock_grouper_class.return_value = mock_grouper

        mock_formatter = MagicMock()
        mock_formatter.format_group_digest = MagicMock(return_value="Combined digest")
        mock_formatter.format_group_rich_digest = MagicMock(
            return_value={"rich_message": {"blocks": []}}
        )
        mock_formatter_class.return_value = mock_formatter

        mock_sender = MagicMock()
        mock_sender.cleanup_old_digests = AsyncMock()
        mock_sender.send_rich_digest = AsyncMock(return_value=True)
        mock_sender.send_channel_messages_with_tracking = AsyncMock(return_value=True)
        mock_sender_class.return_value = mock_sender

        result = await generate_and_send_digest(sample_config, mock_logger, hours=12, user_id=999)

        assert result is True
        mock_grouper.group_summaries.assert_called_once()
        mock_formatter.format_channel_message.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_channel_mode_skips_grouper(sample_config, mock_logger, sample_messages):
    """Mode 'channel' formats per channel and never touches the grouper."""
    sample_config.settings.digest_mode = "channel"

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.Summarizer") as mock_summarizer_class,
        patch("src.core.DigestFormatter") as mock_formatter_class,
        patch("src.core.DigestSender") as mock_sender_class,
        patch("src.core.DigestGrouper") as mock_grouper_class,
    ):
        mock_collector_class.return_value = _make_collector_mock(sample_messages)

        mock_summarizer = MagicMock()
        mock_summarizer.summarize_all = AsyncMock(
            return_value={
                "channel_summaries": {"Test Channel": "Summary text"},
                "overview": "Overview",
            }
        )
        mock_summarizer_class.return_value = mock_summarizer

        mock_formatter = MagicMock()
        mock_formatter.format_channel_message = MagicMock(return_value="Formatted channel msg")
        mock_formatter.format_summary_message = MagicMock(return_value="Summary")
        mock_formatter_class.return_value = mock_formatter

        mock_sender = MagicMock()
        mock_sender.cleanup_old_digests = AsyncMock()
        mock_sender.send_channel_messages_with_tracking = AsyncMock(return_value=True)
        mock_sender_class.return_value = mock_sender

        result = await generate_and_send_digest(
            sample_config, mock_logger, hours=24, user_id=123456789
        )

        assert result is True
        # Should NOT have used the grouped flow
        mock_grouper_class.assert_not_called()
        # Should have used per-channel formatting
        mock_formatter.format_channel_message.assert_called_once()
        mock_sender.send_channel_messages_with_tracking.assert_called_once()


def _make_collector_mock(sample_messages):
    mock_collector = MagicMock()
    mock_collector.connect = AsyncMock()
    mock_collector.fetch_messages = AsyncMock(return_value={"Test Channel": sample_messages})
    mock_collector.disconnect = AsyncMock()
    return mock_collector


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_messages_storage_disabled(sample_config, mock_logger, sample_messages):
    """storage.enabled=False: create_storage returns None, save_messages never called."""
    sample_config.storage = StorageConfig(enabled=False)
    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
    ):
        mock_collector_class.return_value = _make_collector_mock(sample_messages)
        mock_create.return_value = None

        result = await _collect_messages(sample_config, mock_logger, 24)

        mock_create.assert_called_once_with(sample_config.storage)
        assert result == {"Test Channel": sample_messages}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_messages_storage_enabled_saves_flat_list(
    sample_config, mock_logger, sample_messages
):
    """storage.enabled=True: save_messages called with all messages flattened."""
    sample_config.storage = StorageConfig(enabled=True, backend="sqlite", path=":memory:")
    mock_backend = MagicMock()
    mock_backend.save_messages = AsyncMock(return_value=len(sample_messages))
    mock_backend.close = AsyncMock()

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
    ):
        mock_collector_class.return_value = _make_collector_mock(sample_messages)
        mock_create.return_value = mock_backend

        result = await _collect_messages(sample_config, mock_logger, 24)

        mock_create.assert_called_once_with(sample_config.storage)
        mock_backend.save_messages.assert_called_once_with(sample_messages)
        mock_backend.close.assert_called_once()
        assert result == {"Test Channel": sample_messages}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_messages_storage_error_logged_digest_continues(
    sample_config, mock_logger, sample_messages
):
    """save_messages raises: error logged, _collect_messages still returns messages."""
    sample_config.storage = StorageConfig(enabled=True, backend="sqlite", path=":memory:")
    mock_backend = MagicMock()
    mock_backend.save_messages = AsyncMock(side_effect=RuntimeError("disk full"))
    mock_backend.close = AsyncMock()

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
    ):
        mock_collector_class.return_value = _make_collector_mock(sample_messages)
        mock_create.return_value = mock_backend

        result = await _collect_messages(sample_config, mock_logger, 24)

        mock_logger.error.assert_called_once()
        assert "RuntimeError" in mock_logger.error.call_args[0][0]
        assert result == {"Test Channel": sample_messages}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_messages_close_called_even_on_save_error(
    sample_config, mock_logger, sample_messages
):
    """close() called in finally even when save_messages raises."""
    sample_config.storage = StorageConfig(enabled=True, backend="sqlite", path=":memory:")
    mock_backend = MagicMock()
    mock_backend.save_messages = AsyncMock(side_effect=RuntimeError("oops"))
    mock_backend.close = AsyncMock()

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
    ):
        mock_collector_class.return_value = _make_collector_mock(sample_messages)
        mock_create.return_value = mock_backend

        await _collect_messages(sample_config, mock_logger, 24)

        mock_backend.close.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_messages_storage_init_failure_is_non_fatal(
    sample_config, mock_logger, sample_messages
):
    """create_storage raises: error logged, _collect_messages still returns messages."""
    sample_config.storage = StorageConfig(enabled=True, backend="sqlite", path=":memory:")
    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
    ):
        mock_collector_class.return_value = _make_collector_mock(sample_messages)
        mock_create.side_effect = RuntimeError("cannot open db")

        result = await _collect_messages(sample_config, mock_logger, 24)

        mock_logger.error.assert_called_once()
        assert "RuntimeError" in mock_logger.error.call_args[0][0]
        assert result == {"Test Channel": sample_messages}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_digest_calls_storage_when_enabled(sample_config, mock_logger, sample_messages):
    """build_digest saves messages to storage when enabled."""
    sample_config.storage = StorageConfig(enabled=True, backend="sqlite", path=":memory:")
    mock_backend = MagicMock()
    mock_backend.save_messages = AsyncMock(return_value=len(sample_messages))
    mock_backend.close = AsyncMock()

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.Summarizer") as mock_summarizer_class,
        patch("src.core.DigestFormatter") as mock_formatter_class,
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
    ):
        mock_collector_class.return_value = _make_collector_mock(sample_messages)
        mock_create.return_value = mock_backend

        mock_summarizer = MagicMock()
        mock_summarizer.summarize_all = AsyncMock(
            return_value={"channel_summaries": {}, "overview": ""}
        )
        mock_summarizer_class.return_value = mock_summarizer

        mock_formatter = MagicMock()
        mock_formatter.create_digest = MagicMock(return_value="digest")
        mock_formatter_class.return_value = mock_formatter

        await build_digest(sample_config, mock_logger, hours=24)

        mock_create.assert_called_once_with(sample_config.storage)
        mock_backend.save_messages.assert_called_once_with(sample_messages)
        mock_backend.close.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_save_to_storage_close_failure_is_non_fatal(
    sample_config, mock_logger, sample_messages
):
    """storage.close() raises: error logged, no exception propagated."""
    from src.core import _save_to_storage

    sample_config.storage = StorageConfig(enabled=True, backend="sqlite", path=":memory:")
    mock_backend = MagicMock()
    mock_backend.save_messages = AsyncMock(return_value=len(sample_messages))
    mock_backend.close = AsyncMock(side_effect=RuntimeError("close boom"))

    with patch("src.core.create_storage", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_backend
        await _save_to_storage(sample_config, {"Test Channel": sample_messages}, mock_logger)

    mock_logger.error.assert_called()
    error_calls = [str(c) for c in mock_logger.error.call_args_list]
    assert any("close" in c.lower() for c in error_calls)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_digest_storage_disabled_does_not_save(
    sample_config, mock_logger, sample_messages
):
    """build_digest skips storage when disabled."""
    sample_config.storage = StorageConfig(enabled=False)
    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.Summarizer") as mock_summarizer_class,
        patch("src.core.DigestFormatter") as mock_formatter_class,
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
    ):
        mock_collector_class.return_value = _make_collector_mock(sample_messages)
        mock_create.return_value = None

        mock_summarizer = MagicMock()
        mock_summarizer.summarize_all = AsyncMock(
            return_value={"channel_summaries": {}, "overview": ""}
        )
        mock_summarizer_class.return_value = mock_summarizer

        mock_formatter = MagicMock()
        mock_formatter.create_digest = MagicMock(return_value="digest")
        mock_formatter_class.return_value = mock_formatter

        await build_digest(sample_config, mock_logger, hours=24)

        mock_create.assert_called_once_with(sample_config.storage)


# --- _apply_filters tests ---


def _make_filter_spec(class_path: str, **cfg) -> FilterSpec:
    return FilterSpec(class_path=class_path, config=dict(cfg))


class _BrokenFilter:
    name = "broken"

    async def filter(self, channel, messages):
        raise RuntimeError("kaboom")


class _PassFilter:
    name = "pass"

    async def filter(self, channel, messages):
        return messages[:1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_filters_empty_global_specs_returns_unchanged(
    sample_config, mock_logger, sample_messages
):
    """No global filters configured -> messages unchanged."""
    sample_config.settings.filters = []
    ch_cfg = ChannelConfig(id="@test", name="Test Channel")
    result = await _apply_filters(ch_cfg, sample_messages, sample_config, mock_logger)
    assert result == sample_messages


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_filters_channel_none_uses_global(sample_config, mock_logger, sample_messages):
    """channel.filters=None falls back to global filter list."""
    sample_config.settings.filters = [
        _make_filter_spec("src.extensions.filters.KeywordFilter", include=["message 1"])
    ]
    ch_cfg = ChannelConfig(id="@test", name="Test Channel", filters=None)
    result = await _apply_filters(ch_cfg, sample_messages, sample_config, mock_logger)
    assert len(result) == 1
    assert result[0].text == "Test message 1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_filters_channel_empty_list_overrides_global(
    sample_config, mock_logger, sample_messages
):
    """channel.filters=[] is explicit no-op even when global has filters."""
    sample_config.settings.filters = [
        _make_filter_spec("src.extensions.filters.MinLengthFilter", min_chars=9999)
    ]
    ch_cfg = ChannelConfig(id="@test", name="Test Channel", filters=[])
    result = await _apply_filters(ch_cfg, sample_messages, sample_config, mock_logger)
    assert result == sample_messages


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_filters_channel_list_overrides_global(
    sample_config, mock_logger, sample_messages
):
    """channel.filters with entries overrides global entirely."""
    sample_config.settings.filters = [
        _make_filter_spec("src.extensions.filters.MinLengthFilter", min_chars=9999)
    ]
    ch_cfg = ChannelConfig(
        id="@test",
        name="Test Channel",
        filters=[_make_filter_spec("src.extensions.filters.MinLengthFilter", min_chars=1)],
    )
    result = await _apply_filters(ch_cfg, sample_messages, sample_config, mock_logger)
    assert result == sample_messages


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_filters_chain_ordering(sample_config, mock_logger):
    """Filters apply in order: output of first feeds second."""
    from datetime import datetime, timezone

    from src.collector import Message

    msgs = [
        Message(
            "hello world job", "u", datetime(2025, 1, 1, tzinfo=timezone.utc), "#", "ch", False, ""
        ),
        Message(
            "hello world", "u", datetime(2025, 1, 1, tzinfo=timezone.utc), "#", "ch", False, ""
        ),
        Message(
            "job offer at company",
            "u",
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            "#",
            "ch",
            False,
            "",
        ),
    ]
    sample_config.settings.filters = [
        _make_filter_spec("src.extensions.filters.KeywordFilter", include=["job"]),
        _make_filter_spec("src.extensions.filters.KeywordFilter", exclude=["company"]),
    ]
    ch_cfg = ChannelConfig(id="@test", name="ch")
    result = await _apply_filters(ch_cfg, msgs, sample_config, mock_logger)
    assert len(result) == 1
    assert result[0].text == "hello world job"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_filters_unresolvable_class_path_logged_and_skipped(
    sample_config, mock_logger, sample_messages
):
    """Unresolvable class_path: error logged, filter skipped, messages preserved."""
    sample_config.settings.filters = [
        _make_filter_spec("nonexistent.module.BadFilter"),
    ]
    ch_cfg = ChannelConfig(id="@test", name="Test Channel")
    result = await _apply_filters(ch_cfg, sample_messages, sample_config, mock_logger)
    mock_logger.error.assert_called_once()
    assert "nonexistent.module.BadFilter" in mock_logger.error.call_args[0][0]
    assert result == sample_messages


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_filters_init_error_logged_and_skipped(
    sample_config, mock_logger, sample_messages
):
    """Filter instantiation error: logged, filter skipped, messages preserved."""
    sample_config.settings.filters = [
        _make_filter_spec("src.extensions.filters.MinLengthFilter", min_chars="not_an_int"),
    ]
    ch_cfg = ChannelConfig(id="@test", name="Test Channel")
    result = await _apply_filters(ch_cfg, sample_messages, sample_config, mock_logger)
    mock_logger.error.assert_called_once()
    assert result == sample_messages


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_filters_filter_exception_skips_that_filter_preserves_list(
    sample_config, mock_logger, sample_messages
):
    """filter() raises -> error logged, that step skipped, next filter still runs."""
    sample_config.settings.filters = [
        _make_filter_spec("tests.test_core._BrokenFilter"),
        _make_filter_spec("tests.test_core._PassFilter"),
    ]
    ch_cfg = ChannelConfig(id="@test", name="Test Channel")
    result = await _apply_filters(ch_cfg, sample_messages, sample_config, mock_logger)
    assert len(result) == 1
    mock_logger.error.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_messages_applies_filters_before_storage(
    sample_config, mock_logger, sample_messages
):
    """_collect_messages applies filters; only filtered messages reach storage."""
    sample_config.settings.filters = [
        _make_filter_spec("src.extensions.filters.MinLengthFilter", min_chars=99999)
    ]
    sample_config.storage = StorageConfig(enabled=True, backend="sqlite", path=":memory:")
    mock_backend = MagicMock()
    mock_backend.save_messages = AsyncMock(return_value=0)
    mock_backend.close = AsyncMock()

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
    ):
        mock_collector_class.return_value = _make_collector_mock(sample_messages)
        mock_create.return_value = mock_backend

        result = await _collect_messages(sample_config, mock_logger, 24)

    assert result == {"Test Channel": []}
    mock_backend.save_messages.assert_called_once_with([])


@pytest.mark.unit
def test_order_groups_pushes_literal_other_last_when_locale_differs(sample_config):
    """Literal "Other" must sort last even when output_language localizes the bucket name."""
    from src.core import _order_groups

    sample_config.settings.output_language = "Russian"
    grouped = {"News": [], "Other": [], "Sport": []}
    order = _order_groups(grouped, sample_config)
    assert order[-1] == "Other"
    assert set(order) == {"News", "Other", "Sport"}


@pytest.mark.unit
def test_order_groups_pushes_literal_other_case_insensitive(sample_config):
    """Case variants of "Other" (e.g. "OTHER", "other") all push to the end."""
    from src.core import _order_groups

    sample_config.settings.output_language = "English"
    grouped = {"News": [], "OTHER": [], "Sport": []}
    order = _order_groups(grouped, sample_config)
    assert order[-1] == "OTHER"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_summary_message_dedupes_split_group_names(
    sample_config, mock_logger, sample_messages
):
    """When a group's formatted message is split into >1 chunk, the summary header
    must list that group exactly once — not once per chunk."""
    long_news = "a" * 4500  # exceeds split_message default max_length=4000
    sample_config.settings.digest_mode = "digest"

    with (
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core.Summarizer") as mock_summarizer_class,
        patch("src.core.DigestGrouper") as mock_grouper_class,
        patch("src.core.DigestFormatter") as mock_formatter_class,
        patch("src.core.DigestSender") as mock_sender_class,
    ):
        mock_collector = MagicMock()
        mock_collector.connect = AsyncMock()
        mock_collector.fetch_messages = AsyncMock(return_value={"Test Channel": sample_messages})
        mock_collector.disconnect = AsyncMock()
        mock_collector_class.return_value = mock_collector

        mock_summarizer = MagicMock()
        mock_summarizer.summarize_all = AsyncMock(
            return_value={
                "channel_summaries": {"Test Channel": "- Long news"},
                "overview": "",
            }
        )
        mock_summarizer_class.return_value = mock_summarizer

        mock_grouper = MagicMock()
        mock_grouper.group_summaries = AsyncMock(
            return_value={"News": [GroupedPoint(point="X", source="Test Channel")]}
        )
        mock_grouper_class.return_value = mock_grouper

        mock_formatter = MagicMock()
        mock_formatter.format_group_digest = MagicMock(return_value=long_news)
        mock_formatter.format_group_rich_digest = MagicMock(
            return_value={"rich_message": {"blocks": []}}
        )
        mock_formatter_class.return_value = mock_formatter

        mock_sender = MagicMock()
        mock_sender.cleanup_old_digests = AsyncMock()
        mock_sender.send_rich_digest = AsyncMock(return_value=True)
        mock_sender.send_channel_messages_with_tracking = AsyncMock(return_value=True)
        mock_sender_class.return_value = mock_sender

        await generate_and_send_digest(sample_config, mock_logger, hours=24, user_id=123456789)

        mock_formatter.format_group_digest.assert_called_once()
        mock_sender.send_rich_digest.assert_called_once_with(
            {"rich_message": {"blocks": []}},
            user_id=123456789,
            fallback_text=long_news,
        )


def _make_single_channel_collector(messages):
    """Collector mock for the single-channel path."""
    mock_collector = MagicMock()
    mock_collector.connect = AsyncMock()
    mock_collector.fetch_channel_messages = AsyncMock(return_value=messages)
    mock_collector.disconnect = AsyncMock()
    return mock_collector


@pytest.mark.unit
@pytest.mark.parametrize(
    "wanted", ["Test Channel", "test channel", "@test_channel", " @TEST_CHANNEL "]
)
def test_resolve_channel_matches_name_or_id(sample_config, wanted):
    """A channel is addressable by its config name or its id, case-insensitively."""
    assert _resolve_channel(sample_config, wanted).name == "Test Channel"


@pytest.mark.unit
def test_resolve_channel_lists_known_names(sample_config):
    """An unknown channel fails with the configured names, so the caller can retry."""
    with pytest.raises(ValueError, match="Test Channel, Private Group"):
        _resolve_channel(sample_config, "Nope")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_channel_messages_prefers_storage(
    sample_config, mock_logger, sample_messages
):
    """Stored messages are served without touching Telegram, newest-first flipped to chronological."""
    mock_backend = MagicMock()
    mock_backend.query_messages = AsyncMock(return_value=list(reversed(sample_messages)))
    mock_backend.close = AsyncMock()

    with (
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
        patch("src.core.MessageCollector") as mock_collector_class,
    ):
        mock_create.return_value = mock_backend

        messages, source = await collect_channel_messages(
            sample_config, mock_logger, "Test Channel", hours=12, limit=50
        )

        assert source == "storage"
        assert messages == sample_messages
        assert mock_backend.query_messages.call_args.kwargs["channel_name"] == "Test Channel"
        assert mock_backend.query_messages.call_args.kwargs["limit"] == 50
        mock_backend.close.assert_called_once()
        mock_collector_class.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "storage_result",
    [None, []],
    ids=["storage_disabled", "storage_empty"],
)
async def test_collect_channel_messages_falls_back_to_telegram(
    sample_config, mock_logger, sample_messages, storage_result
):
    """No usable stored data means a live read, with the config filters applied."""
    mock_backend = MagicMock()
    mock_backend.query_messages = AsyncMock(return_value=[])
    mock_backend.close = AsyncMock()

    with (
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core._apply_filters", new_callable=AsyncMock) as mock_filters,
    ):
        mock_create.return_value = None if storage_result is None else mock_backend
        collector = _make_single_channel_collector(sample_messages)
        mock_collector_class.return_value = collector
        mock_filters.return_value = sample_messages

        messages, source = await collect_channel_messages(
            sample_config, mock_logger, "@test_channel", hours=6
        )

        assert source == "telegram"
        assert messages == sample_messages
        collector.disconnect.assert_called_once()
        mock_filters.assert_called_once()
        assert mock_filters.call_args[0][0].name == "Test Channel"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_channel_messages_falls_back_when_storage_raises(
    sample_config, mock_logger, sample_messages
):
    """A broken store logs and degrades to Telegram instead of failing the call."""
    with (
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core._apply_filters", new_callable=AsyncMock) as mock_filters,
    ):
        mock_create.side_effect = RuntimeError("db down")
        mock_collector_class.return_value = _make_single_channel_collector(sample_messages)
        mock_filters.return_value = sample_messages

        _, source = await collect_channel_messages(sample_config, mock_logger, "Test Channel")

        assert source == "telegram"
        assert "RuntimeError" in mock_logger.error.call_args[0][0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_channel_messages_keeps_newest_within_limit(
    sample_config, mock_logger, sample_messages
):
    """The limit trims the oldest messages, since the newest ones matter most."""
    with (
        patch("src.core.create_storage", new_callable=AsyncMock) as mock_create,
        patch("src.core.MessageCollector") as mock_collector_class,
        patch("src.core._apply_filters", new_callable=AsyncMock) as mock_filters,
    ):
        mock_create.return_value = None
        mock_collector_class.return_value = _make_single_channel_collector(sample_messages)
        mock_filters.return_value = sample_messages

        messages, _ = await collect_channel_messages(
            sample_config, mock_logger, "Test Channel", limit=1
        )

        assert messages == sample_messages[-1:]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 5001, True, 2.5, "10"])
async def test_collect_channel_messages_rejects_bad_limit(sample_config, mock_logger, limit):
    """An out-of-range limit fails before any storage or Telegram work happens."""
    with patch("src.core.create_storage", new_callable=AsyncMock) as mock_create:
        with pytest.raises(ValueError, match="limit must be between"):
            await collect_channel_messages(sample_config, mock_logger, "Test Channel", limit=limit)

        mock_create.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_and_publish_article_workflow(sample_config, mock_logger, tmp_path):
    """Test the complete generate_and_publish_article orchestration workflow."""
    from src.core import generate_and_publish_article

    sample_config.settings.article.fallback_save_dir = str(tmp_path)

    with (
        patch("src.core._collect_messages", new_callable=AsyncMock) as mock_collect,
        patch(
            "src.article_generator.ArticleGenerator.generate_article", new_callable=AsyncMock
        ) as mock_gen,
        patch("src.telegraph.TelegraphPublisher.create_page", new_callable=AsyncMock) as mock_page,
        patch(
            "src.sender.DigestSender.send_article_instant_view", new_callable=AsyncMock
        ) as mock_send,
    ):
        mock_collect.return_value = {"Test Channel": [MagicMock()]}
        mock_gen.return_value = ("Заголовок", "Лид", "# Заголовок\n\nТекст статьи.")
        mock_page.return_value = "https://telegra.ph/Sample-08-14"
        mock_send.return_value = True

        success = await generate_and_publish_article(sample_config, mock_logger, hours=24)
        assert success is True
        mock_collect.assert_called_once()
        mock_gen.assert_called_once()
        mock_page.assert_called_once()
        mock_send.assert_called_once()
        assert len(list(tmp_path.glob("*_editorial.md"))) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_and_publish_article_dry_run_saves_timestamped_preview(
    sample_config, mock_logger, tmp_path
):
    """Dry-run mode saves timestamped preview file (preview_<timestamp>_editorial.md)."""
    from src.core import generate_and_publish_article

    sample_config.settings.article.fallback_save_dir = str(tmp_path)

    with (
        patch("src.core._collect_messages", new_callable=AsyncMock) as mock_collect,
        patch(
            "src.article_generator.ArticleGenerator.generate_article", new_callable=AsyncMock
        ) as mock_gen,
        patch("src.telegraph.TelegraphPublisher.create_page", new_callable=AsyncMock) as mock_page,
        patch(
            "src.sender.DigestSender.send_article_instant_view", new_callable=AsyncMock
        ) as mock_send,
    ):
        mock_collect.return_value = {"Test Channel": [MagicMock()]}
        mock_gen.return_value = (
            "Превью Заголовок",
            "Превью Лид",
            "# Превью Заголовок\n\nТекст превью.",
        )

        success = await generate_and_publish_article(
            sample_config, mock_logger, hours=24, dry_run=True
        )
        assert success is True
        mock_page.assert_not_called()
        mock_send.assert_not_called()

        preview_files = list(tmp_path.glob("preview_*_editorial.md"))
        assert len(preview_files) == 1
        assert "Текст превью." in preview_files[0].read_text(encoding="utf-8")
        assert not (tmp_path / "preview_editorial.md").exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_and_publish_article_uses_fallback_when_model_fails(
    sample_config, mock_logger, tmp_path
):
    """A model failure still yields a source-based article for publication."""
    from datetime import datetime, timezone

    from src.collector import Message
    from src.core import generate_and_publish_article

    sample_config.settings.article.fallback_save_dir = str(tmp_path)
    fallback_message = Message(
        text="Коммунальная служба сообщила о временном отключении воды",
        sender="Редакция",
        timestamp=datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc),
        link="https://t.me/test/1",
        channel_name="Test Channel",
        has_media=False,
        media_type="",
    )

    with (
        patch("src.core._collect_messages", new_callable=AsyncMock) as mock_collect,
        patch(
            "src.article_generator.ArticleGenerator.generate_article", new_callable=AsyncMock
        ) as mock_gen,
        patch("src.telegraph.TelegraphPublisher.create_page", new_callable=AsyncMock) as mock_page,
        patch(
            "src.sender.DigestSender.send_article_instant_view", new_callable=AsyncMock
        ) as mock_send,
    ):
        mock_collect.return_value = {"Test Channel": [fallback_message]}
        mock_gen.side_effect = RuntimeError("provider unavailable")
        mock_page.return_value = "https://telegra.ph/Sample-08-15"
        mock_send.return_value = True

        success = await generate_and_publish_article(sample_config, mock_logger, hours=24)

        assert success is True
        mock_page.assert_called_once()
        page_content = mock_page.call_args.kwargs["content_markdown"]
        assert "Жители сообщали о перебоях с водоснабжением" in page_content
        assert "временном отключении воды" not in page_content
        mock_send.assert_called_once()
        assert list(tmp_path.glob("*_editorial.md"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zero_card_editorial_outcome_does_not_trigger_core_fallback(
    sample_config, mock_logger, tmp_path
):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, patch

    from src.article_generator import NoSubstantiveEditorialError
    from src.collector import Message
    from src.core import generate_and_publish_article

    sample_config.settings.article.fallback_save_dir = str(tmp_path)
    message = Message(
        text="Сообщение",
        sender="Житель",
        timestamp=datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc),
        link="https://t.me/test/1",
        channel_name="Test Channel",
        has_media=False,
        media_type="",
    )

    with (
        patch("src.core._collect_messages", new_callable=AsyncMock) as mock_collect,
        patch(
            "src.article_generator.ArticleGenerator.generate_article", new_callable=AsyncMock
        ) as mock_gen,
        patch("src.core._build_fallback_article") as mock_fallback,
        patch("src.telegraph.TelegraphPublisher.create_page", new_callable=AsyncMock) as mock_page,
    ):
        mock_collect.return_value = {"Test Channel": [message]}
        mock_gen.side_effect = NoSubstantiveEditorialError("no publishable local stories")

        success = await generate_and_publish_article(sample_config, mock_logger, hours=24)

        assert success is False
        mock_fallback.assert_not_called()
        mock_page.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extract_candidate_photo_bytes_success(sample_config, mock_logger):
    from datetime import datetime, timezone

    from src.collector import Message
    from src.core import _extract_candidate_photo_bytes

    photo_msg = Message(
        text="Фото с места ремонта",
        sender="Корреспондент",
        timestamp=datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc),
        link="https://t.me/test/100",
        channel_name="Test Channel",
        has_media=True,
        media_type="Фото",
        message_id=100,
        channel_id=-100123456789,
    )

    fake_bytes = b"fake_source_photo_bytes"
    mock_collector = MagicMock()
    mock_collector.connect = AsyncMock()
    mock_collector.disconnect = AsyncMock()
    mock_collector.download_message_photo = AsyncMock(return_value=fake_bytes)

    with patch("src.core.MessageCollector", return_value=mock_collector):
        res = await _extract_candidate_photo_bytes(
            {"Test Channel": [photo_msg]}, sample_config, mock_logger
        )

    assert res == fake_bytes
    mock_collector.download_message_photo.assert_called_once_with(
        channel_identifier=-100123456789,
        message_id=100,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_extract_candidate_photo_bytes_no_photo(sample_config, mock_logger):
    from datetime import datetime, timezone

    from src.collector import Message
    from src.core import _extract_candidate_photo_bytes

    text_msg = Message(
        text="Текстовое сообщение без фото",
        sender="Корреспондент",
        timestamp=datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc),
        link="https://t.me/test/101",
        channel_name="Test Channel",
        has_media=False,
        media_type="",
        message_id=101,
        channel_id=-100123456789,
    )

    res = await _extract_candidate_photo_bytes(
        {"Test Channel": [text_msg]}, sample_config, mock_logger
    )
    assert res is None


# --- persistent_ingestion (transitional DB-read cutover) ---


def _forbid_telethon(mock_collector_class) -> None:
    """Any MessageCollector construction means the flag leaked to the legacy path."""
    mock_collector_class.side_effect = AssertionError("Telethon must not be used")


class _BrokenUow:
    @asynccontextmanager
    async def transaction(self):
        raise RuntimeError("database unavailable")
        yield


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_messages_persistent_surfaces_db_errors_without_telegram_fallback(
    sample_config, mock_logger
):
    """persistent_ingestion=true: a DB failure raises explicitly and never falls back."""
    sample_config.settings.persistent_ingestion = True

    with patch("src.core.MessageCollector") as mock_collector_class:
        _forbid_telethon(mock_collector_class)
        install_runtime(SimpleNamespace(uow=_BrokenUow()))

        with pytest.raises(RuntimeError, match="database unavailable"):
            await _collect_messages(sample_config, mock_logger, 24)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_channel_messages_persistent_reads_history_without_telegram(
    sample_config, mock_logger, sample_messages, monkeypatch
):
    """Single-channel reads route through the reader; config filters still apply."""

    class _StubReader:
        def __init__(self, uow, repository):
            del uow, repository

        async def read_telegram_messages(self, edition_slug, since, until):
            assert edition_slug == "berdyansk"
            return {
                "Test Channel": sample_messages,
                "Unconfigured Source": [object()],
            }

    monkeypatch.setattr("src.core.SourceRevisionReader", _StubReader)
    sample_config.settings.persistent_ingestion = True
    sample_config.settings.filters = [
        FilterSpec(
            class_path="src.extensions.filters.KeywordFilter",
            config={"include": ["Test message 2"]},
        )
    ]

    with patch("src.core.MessageCollector") as mock_collector_class:
        _forbid_telethon(mock_collector_class)
        install_runtime(SimpleNamespace(uow=object()))
        messages, source = await collect_channel_messages(
            sample_config, mock_logger, "Test Channel", hours=6
        )

    assert (messages, source) == ([sample_messages[1]], "persistent")
    mock_collector_class.assert_not_called()


requires_postgres = pytest.mark.skipif(
    "TELEBRIEF_TEST_DATABASE_URL" not in os.environ,
    reason="TELEBRIEF_TEST_DATABASE_URL is not set",
)

_TRUNCATE = """
    TRUNCATE source_items, source_item_revisions, source_assets,
             source_item_state_events, collection_checkpoints,
             collection_runs, source_editions, sources, editions
    RESTART IDENTITY CASCADE
"""


@requires_postgres
@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_digest_parts_persistent_reaches_summarization_from_db(
    sample_config, mock_logger, database_config
):
    """Flag=true: digest inputs come from PostgreSQL; Telethon.connect must never run."""
    from src.collector import Message

    sample_config.settings.persistent_ingestion = True
    published = datetime.now(timezone.utc) - timedelta(hours=1)

    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        database_config.url, autocommit=True
    )
    pool = await open_pool(database_config)
    try:
        await conn.execute(_TRUNCATE)
        cursor = await conn.execute(
            "INSERT INTO editions (slug, name) VALUES ('berdyansk', 'Бердянск') RETURNING id"
        )
        edition_id = (await cursor.fetchone())[0]
        cursor = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, name)
            VALUES ('telegram', 'channel', '@test_channel', 'Test Channel')
            RETURNING id
            """
        )
        source_id = (await cursor.fetchone())[0]
        await conn.execute(
            "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
            (source_id, edition_id),
        )
        cursor = await conn.execute(
            """
            INSERT INTO source_items (
                source_id, kind, external_id, author_name, canonical_url,
                published_at, first_collected_at, metadata
            )
            VALUES (
                %s, 'telegram_message', '42', 'Редакция', 'https://t.me/test_channel/42',
                %s, %s, '{"topic_id": null, "reply_to_id": null, "has_media": false}'::jsonb
            )
            RETURNING id
            """,
            (source_id, published, published),
        )
        item_id = (await cursor.fetchone())[0]
        await conn.execute(
            """
            INSERT INTO source_item_revisions (
                source_item_id, revision_no, collected_at, content_hash, text_content, payload
            )
            VALUES (%s, 1, %s, 'hash-1', 'Вода отключена на центральной улице', '{}'::jsonb)
            """,
            (item_id, published),
        )

        install_runtime(SimpleNamespace(uow=DatabaseUnitOfWork(pool)))

        with (
            patch("src.core.MessageCollector") as mock_collector_class,
            patch("src.core.Summarizer") as mock_summarizer_class,
            patch("src.core.DigestFormatter") as mock_formatter_class,
        ):
            _forbid_telethon(mock_collector_class)

            captured = {}

            async def _capture_summarize(messages_by_channel):
                captured["messages"] = messages_by_channel
                return {"channel_summaries": {"Test Channel": "Summary"}, "overview": ""}

            mock_summarizer = MagicMock()
            mock_summarizer.summarize_all = AsyncMock(side_effect=_capture_summarize)
            mock_summarizer_class.return_value = mock_summarizer

            mock_formatter = MagicMock()
            mock_formatter.format_channel_message = MagicMock(return_value="Channel msg")
            mock_formatter.format_summary_message = MagicMock(return_value="Header")
            mock_formatter_class.return_value = mock_formatter

            built = await _build_digest_parts(sample_config, mock_logger, hours=24)

        assert built is not None
        parts, summary_message, rich_document = built
        assert parts == [("Test Channel", "Channel msg")]
        assert summary_message == "Header"
        assert rich_document is None

        persisted = captured["messages"]["Test Channel"]
        assert len(persisted) == 1
        assert isinstance(persisted[0], Message)
        assert persisted[0].text == "Вода отключена на центральной улице"
        assert persisted[0].channel_name == "Test Channel"
        assert persisted[0].message_id == 42
    finally:
        await conn.execute(_TRUNCATE)
        await conn.close()
        await close_pool(pool)
