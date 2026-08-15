"""Tests for whole-source and forum-topic Telegram collection."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.collector import MessageCollector
from src.config_loader import ChannelConfig, ForumTopicConfig


def _telegram_message(message_id: int, text: str = "Новость"):
    message = MagicMock()
    message.id = message_id
    message.date = datetime.now(timezone.utc) - timedelta(minutes=5)
    message.text = text
    message.media = None
    message.sender = None
    message.reply_to = None
    return message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_to_project_message_preserves_context_metadata(sample_config, mock_logger):
    """Telegram IDs and reply metadata survive conversion to the project model."""
    collector = _collector(sample_config, mock_logger)
    message = _telegram_message(42)
    message.reply_to = SimpleNamespace(reply_to_msg_id=41)
    entity = SimpleNamespace(id=123, username="source")

    result = await collector._to_project_message(entity, "Source", message, topic_id=99)

    assert result is not None
    assert result.message_id == 42
    assert result.reply_to_id == 41
    assert result.topic_id == 99


def _collector(config, logger):
    """Construct a collector without opening the real local Telegram session."""
    with patch("src.collector.TelegramClient"):
        collector = MessageCollector(config, logger)
    return collector


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_topic_messages_uses_top_msg_id(sample_config, mock_logger):
    """Telegram receives the selected forum topic ID, not the whole group history."""
    collector = _collector(sample_config, mock_logger)
    collector.client = AsyncMock()
    collector.client.return_value = SimpleNamespace(
        messages=[_telegram_message(9001, "Отключили воду на улице")]
    )
    channel = ChannelConfig(id="@source", name="Source")
    topic = ForumTopicConfig(id=235525, name="Проблемы ЖКХ")
    entity = SimpleNamespace(id=123, username="source")

    result = await collector._fetch_topic_messages(
        entity, channel, topic, datetime.now(timezone.utc) - timedelta(hours=1)
    )

    request = collector.client.call_args.args[0]
    assert request.top_msg_id == 235525
    assert result[0].channel_name == "Source — Проблемы ЖКХ"
    assert result[0].link == "https://t.me/source/9001"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_messages_returns_one_entry_per_selected_topic(sample_config, mock_logger):
    """Two selected topics become separate logical inputs to the summarizer."""
    sample_config.channels = [
        ChannelConfig(
            id="@source",
            name="Source",
            topics=[
                ForumTopicConfig(id=235525, name="Проблемы ЖКХ"),
                ForumTopicConfig(id=43339, name="Новости Бердянска"),
            ],
        )
    ]
    collector = _collector(sample_config, mock_logger)
    collector.client.get_entity = AsyncMock(return_value=SimpleNamespace(id=123))
    collector._fetch_topic_messages = AsyncMock(
        side_effect=[[_telegram_message(1)], [_telegram_message(2)]]
    )

    result = await collector.fetch_messages(hours=24)

    assert list(result) == ["Source — Проблемы ЖКХ", "Source — Новости Бердянска"]
    assert len(result["Source — Проблемы ЖКХ"]) == 1
    assert len(result["Source — Новости Бердянска"]) == 1
    assert collector._fetch_topic_messages.await_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_messages_continues_when_one_topic_fails(sample_config, mock_logger):
    """A temporary error in one topic does not hide the other selected topic."""
    sample_config.channels = [
        ChannelConfig(
            id="@source",
            name="Source",
            topics=[
                ForumTopicConfig(id=235525, name="Проблемы ЖКХ"),
                ForumTopicConfig(id=43339, name="Новости Бердянска"),
            ],
        )
    ]
    collector = _collector(sample_config, mock_logger)
    collector.client.get_entity = AsyncMock(return_value=SimpleNamespace(id=123))
    collector._fetch_topic_messages = AsyncMock(
        side_effect=[RuntimeError("topic unavailable"), [_telegram_message(2)]]
    )

    result = await collector.fetch_messages(hours=24)

    assert list(result) == ["Source — Новости Бердянска"]
    mock_logger.error.assert_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_messages_keeps_whole_source_behavior_without_topics(
    sample_config, mock_logger
):
    """A source without topics still follows the existing whole-source path."""
    sample_config.channels = [sample_config.channels[0]]
    collector = _collector(sample_config, mock_logger)
    messages = [_telegram_message(3)]
    collector.fetch_channel_messages = AsyncMock(return_value=messages)

    result = await collector.fetch_messages(hours=24)

    assert result["Test Channel"] == messages
    collector.fetch_channel_messages.assert_awaited_once()
