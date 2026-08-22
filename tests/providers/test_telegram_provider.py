"""Tests for the Telegram ObservedItem collector adapter.

Mock Telethon message objects mirror the attribute surface the adapter reads
(id, date, text, media, sender, reply_to, forward, document) — no network.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon import errors as tg_errors

from src.config_loader import ChannelConfig, ForumTopicConfig
from src.domain.sources import Source
from src.ingestion.models import CollectionCheckpoint, CollectionOutcome
from src.ingestion.protocol import CollectionContext
from src.providers.telegram import TelegramCollector


class FakeMessageMediaPhoto:
    """Stands in for telethon MessageMediaPhoto (class name carries 'Photo')."""


class FakeMessageMediaDocument(SimpleNamespace):
    """Stands in for telethon MessageMediaDocument (name carries 'Document')."""


NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)


def _telegram_message(message_id: int, text: str = "Новость") -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        date=datetime.now(timezone.utc) - timedelta(minutes=5),
        text=text,
        media=None,
        sender=None,
        reply_to=None,
        forward=None,
        post=False,
        grouped_id=None,
    )


def _source(**overrides) -> Source:
    values = {
        "id": 7,
        "platform": "telegram",
        "kind": "channel",
        "external_id": "@test_channel",
        "url": None,
        "name": "Test Channel",
        "role": "other",
        "enabled": True,
        "collector_options": {},
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return Source(**values)


def _context() -> CollectionContext:
    return CollectionContext(now=datetime.now(timezone.utc))


def _collector(config, logger):
    with patch("src.providers.telegram.TelegramClient"):
        collector = TelegramCollector(config, logger=logger)
    collector.client = AsyncMock()
    collector.client.get_entity = AsyncMock(
        return_value=SimpleNamespace(id=-1001234567890, username="test_channel")
    )
    return collector


def _iter_messages_result(messages):
    async def _gen(*args, **kwargs):
        for message in messages:
            yield message

    return MagicMock(side_effect=lambda *a, **kw: _gen())


# --- Step 1: conversion tests (brief verbatim assertions) -------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scan_converts_message_to_observed_item(sample_config, mock_logger):
    """Core conversion: stable external id, kind, published_at, topic + reply metadata."""
    sample_config.channels[0].topics = [ForumTopicConfig(id=123, name="ЖКХ")]
    collector = _collector(sample_config, mock_logger)
    message = _telegram_message(42)
    message.reply_to = SimpleNamespace(reply_to_msg_id=77, reply_to_top_id=None)
    collector.client.return_value = SimpleNamespace(messages=[message])

    batch = await collector.scan(_source(), None, _context())

    assert batch.outcome == CollectionOutcome.SUCCESS
    observed = batch.items[0]
    assert observed.external_id == str(message.id)
    assert observed.kind == "telegram_message"
    assert observed.published_at == message.date
    assert observed.metadata["topic_id"] == 123
    assert observed.metadata["reply_to_id"] == 77
    assert observed.parent_external_id == "77"
    assert observed.root_external_id is None
    assert observed.canonical_url == "https://t.me/test_channel/42"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_topic_root_becomes_root_external_id(sample_config, mock_logger):
    """reply_to_top_id maps to root_external_id (topic root threading)."""
    sample_config.channels[0].topics = [ForumTopicConfig(id=123, name="ЖКХ")]
    collector = _collector(sample_config, mock_logger)
    message = _telegram_message(43)
    message.reply_to = SimpleNamespace(reply_to_msg_id=42, reply_to_top_id=100)
    collector.client.return_value = SimpleNamespace(messages=[message])

    batch = await collector.scan(_source(), None, _context())

    observed = batch.items[0]
    assert observed.parent_external_id == "42"
    assert observed.root_external_id == "100"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_service_post_without_text_or_media_is_skipped(sample_config, mock_logger):
    """Empty service posts never become ObservedItems (legacy conversion parity)."""
    collector = _collector(sample_config, mock_logger)
    service_post = _telegram_message(9, text=None)
    collector.client.iter_messages = _iter_messages_result([service_post])

    batch = await collector.scan(_source(), None, _context())

    assert batch.items == ()
    assert batch.outcome == CollectionOutcome.SUCCESS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forward_origin_and_media_metadata_retained(sample_config, mock_logger):
    """Forward origin and media flags survive into observation metadata."""
    collector = _collector(sample_config, mock_logger)
    message = _telegram_message(50, "Официальное сообщение")
    fwd_chat = SimpleNamespace(title="Запорожгаз", username="zaporozhgaz_official")
    message.forward = SimpleNamespace(
        from_name=None,
        chat=fwd_chat,
        sender=None,
        post_author=None,
        date=datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc),
    )
    message.media = FakeMessageMediaPhoto()
    collector.client.iter_messages = _iter_messages_result([message])

    batch = await collector.scan(_source(), None, _context())

    metadata = batch.items[0].metadata
    assert metadata["forward_from_name"] == "Запорожгаз"
    assert metadata["forward_from_username"] == "zaporozhgaz_official"
    assert metadata["forward_date"] == "2026-08-20T09:30:00+00:00"
    assert metadata["has_media"] is True
    assert metadata["media_kinds"] == ["photo"]
    assert len(batch.assets) == 1
    asset = batch.assets[0]
    assert asset.item_external_id == "50"
    assert asset.kind == "photo"
    assert asset.mime_type == "image/jpeg"
    assert asset.external_url is None
    assert asset.content_hash is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_document_asset_metadata(sample_config, mock_logger):
    """Document assets carry mime type and cheap file metadata."""
    collector = _collector(sample_config, mock_logger)
    message = _telegram_message(60)
    document = SimpleNamespace(
        mime_type="application/pdf",
        size=2048,
        id=999888,
        attributes=[SimpleNamespace(file_name="report.pdf")],
    )
    message.media = FakeMessageMediaDocument(document=document)
    collector.client.iter_messages = _iter_messages_result([message])

    batch = await collector.scan(_source(), None, _context())

    asset = batch.assets[0]
    assert asset.item_external_id == "60"
    assert asset.kind == "document"
    assert asset.mime_type == "application/pdf"
    assert asset.metadata["file_name"] == "report.pdf"
    assert asset.metadata["size"] == 2048
    assert asset.metadata["unique_id"] == 999888


# --- Effective editorial role (topic > channel precedence) ------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_effective_source_role_topic_overrides_channel(sample_config, mock_logger):
    """Topic-level source_type wins over channel role in observation metadata."""
    sample_config.channels[0] = ChannelConfig(
        id="@test_channel",
        name="Test Channel",
        source_type="community",
        topics=[
            ForumTopicConfig(id=235525, name="Проблемы ЖКХ", source_type="news"),
            ForumTopicConfig(id=43339, name="Новости Бердянска"),
        ],
    )
    collector = _collector(sample_config, mock_logger)
    topic_message = _telegram_message(1)
    plain_topic_message = _telegram_message(2)
    collector.client.side_effect = [
        SimpleNamespace(messages=[topic_message]),
        SimpleNamespace(messages=[plain_topic_message]),
    ]

    batch = await collector.scan(_source(), None, _context())

    by_id = {item.external_id: item for item in batch.items}
    assert by_id["1"].metadata["effective_source_role"] == "news"
    assert by_id["2"].metadata["effective_source_role"] == "community"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_effective_source_role_falls_back_to_source_role(sample_config, mock_logger):
    """Without a matching channel config the neutral Source role is used."""
    collector = _collector(sample_config, mock_logger)
    message = _telegram_message(3)
    collector.client.iter_messages = _iter_messages_result([message])

    batch = await collector.scan(
        _source(external_id="@not_in_config", role="local_media"), None, _context()
    )

    assert batch.items[0].metadata["effective_source_role"] == "local_media"


# --- Incremental checkpoint filtering ---------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_incremental_scan_applies_min_id_from_checkpoint(sample_config, mock_logger):
    """Checkpoint high-watermark becomes min_id and old messages are dropped."""
    collector = _collector(sample_config, mock_logger)
    old_message = _telegram_message(90)
    new_message = _telegram_message(110)
    collector.client.iter_messages = _iter_messages_result([old_message, new_message])
    checkpoint = CollectionCheckpoint(
        adapter_state={"high_watermark_message_id": 100},
        last_success_at=NOW - timedelta(minutes=45),
    )

    batch = await collector.scan(_source(), checkpoint, _context())

    kwargs = collector.client.iter_messages.call_args.kwargs
    assert kwargs["min_id"] == 100
    assert [item.external_id for item in batch.items] == ["110"]
    assert batch.adapter_state["high_watermark_message_id"] == 110


@pytest.mark.unit
@pytest.mark.asyncio
async def test_initial_scan_without_checkpoint_uses_lookback_window(sample_config, mock_logger):
    """No checkpoint means a recent-window fetch without min_id."""
    collector = _collector(sample_config, mock_logger)
    message = _telegram_message(5)
    collector.client.iter_messages = _iter_messages_result([message])

    batch = await collector.scan(_source(), None, _context())

    kwargs = collector.client.iter_messages.call_args.kwargs
    assert not kwargs.get("min_id")
    assert batch.outcome == CollectionOutcome.SUCCESS
    assert len(batch.items) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_checkpointed_scan_ignores_lookback_boundary(sample_config, mock_logger):
    """After downtime longer than lookback, the watermark cursor still catches up."""
    collector = _collector(sample_config, mock_logger)
    stale_but_unseen = _telegram_message(200, text="Позднее сообщение")
    stale_but_unseen.date = datetime.now(timezone.utc) - timedelta(hours=72)
    collector.client.iter_messages = _iter_messages_result([stale_but_unseen])
    checkpoint = CollectionCheckpoint(adapter_state={"high_watermark_message_id": 100})

    batch = await collector.scan(_source(), checkpoint, _context())

    assert [item.external_id for item in batch.items] == ["200"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_checkpointed_topic_fetch_drops_min_date_bound(sample_config, mock_logger):
    """Checkpointed forum scans bound by min_id only — no lookback cutoff."""
    sample_config.channels[0] = ChannelConfig(
        id="@test_channel",
        name="Test Channel",
        topics=[ForumTopicConfig(id=235525, name="ЖКХ")],
    )
    collector = _collector(sample_config, mock_logger)
    collector.client.return_value = SimpleNamespace(messages=[_telegram_message(9001)])
    checkpoint = CollectionCheckpoint(adapter_state={"high_watermark_message_id": 8000})

    await collector.scan(_source(), checkpoint, _context())

    request = collector.client.call_args.args[0]
    assert request.min_date is None
    assert request.min_id == 8000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_initial_scan_stops_at_lookback_boundary(sample_config, mock_logger):
    """Without a checkpoint the recent-window bound still applies."""
    collector = _collector(sample_config, mock_logger)
    fresh = _telegram_message(2)
    ancient = _telegram_message(1)
    ancient.date = datetime.now(timezone.utc) - timedelta(days=30)
    collector.client.iter_messages = _iter_messages_result([fresh, ancient])

    batch = await collector.scan(_source(), None, _context())

    assert [item.external_id for item in batch.items] == ["2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conversion_failure_fails_soft_transient(sample_config, mock_logger):
    """Unexpected conversion errors become TRANSIENT batches, never raise."""

    class ExplodingMessage:
        id = 7
        date = NOW

        @property
        def text(self):
            raise RuntimeError("decode failed")

    collector = _collector(sample_config, mock_logger)
    collector.client.iter_messages = _iter_messages_result([ExplodingMessage()])

    batch = await collector.scan(_source(), None, _context())

    assert batch.outcome == CollectionOutcome.TRANSIENT
    assert batch.error_kind == "unexpected"
    assert batch.adapter_state["exception_type"] == "RuntimeError"
    assert batch.items == ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_batch_carries_prior_high_watermark_forward(sample_config, mock_logger):
    """A scan returning nothing must not erase the stored high-watermark."""
    collector = _collector(sample_config, mock_logger)
    collector.client.iter_messages = _iter_messages_result([])
    checkpoint = CollectionCheckpoint(adapter_state={"high_watermark_message_id": 55})

    batch = await collector.scan(_source(), checkpoint, _context())

    assert batch.items == ()
    assert batch.adapter_state["high_watermark_message_id"] == 55


@pytest.mark.unit
@pytest.mark.asyncio
async def test_topic_fetch_passes_top_msg_id_and_min_id(sample_config, mock_logger):
    """Forum sources fetch per topic with top_msg_id plus checkpoint min_id."""
    sample_config.channels[0] = ChannelConfig(
        id="@test_channel",
        name="Test Channel",
        topics=[ForumTopicConfig(id=235525, name="ЖКХ")],
    )
    collector = _collector(sample_config, mock_logger)
    response = SimpleNamespace(messages=[_telegram_message(9001)])
    collector.client.return_value = response
    checkpoint = CollectionCheckpoint(adapter_state={"high_watermark_message_id": 8000})

    batch = await collector.scan(_source(), checkpoint, _context())

    request = collector.client.call_args.args[0]
    assert request.top_msg_id == 235525
    assert request.min_id == 8000
    assert [item.external_id for item in batch.items] == ["9001"]
    assert batch.items[0].metadata["topic_id"] == 235525


# --- Outcome mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    "error,expected_outcome,expected_kind",
    [
        (
            tg_errors.FloodWaitError(request=None, capture=30),
            CollectionOutcome.RATE_LIMITED,
            "flood_wait",
        ),
        (
            ValueError("Could not find the input entity for PeerChannel"),
            CollectionOutcome.SOURCE_NOT_FOUND,
            "entity_not_found",
        ),
        (
            tg_errors.ChannelPrivateError(request=None),
            CollectionOutcome.SOURCE_NOT_FOUND,
            "channel_private",
        ),
        (
            tg_errors.AuthKeyUnregisteredError(request=None),
            CollectionOutcome.AUTH_REQUIRED,
            "auth_key_unregistered",
        ),
        (TimeoutError(), CollectionOutcome.TRANSIENT, "network_timeout"),
        (ConnectionError("reset"), CollectionOutcome.TRANSIENT, "network_error"),
        (RuntimeError("boom"), CollectionOutcome.TRANSIENT, "unexpected"),
    ],
)
@pytest.mark.unit
@pytest.mark.asyncio
async def test_scan_maps_errors_to_outcomes(
    sample_config, mock_logger, error, expected_outcome, expected_kind
):
    """Expected source-level failures become outcome batches, never raises."""
    collector = _collector(sample_config, mock_logger)
    collector.client.get_entity.side_effect = error

    batch = await collector.scan(_source(), None, _context())

    assert batch.outcome == expected_outcome
    assert batch.error_kind == expected_kind
    assert batch.items == ()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rate_limited_batch_reports_retry_after(sample_config, mock_logger):
    """Flood wait seconds ride along so the scheduler can back off precisely."""
    collector = _collector(sample_config, mock_logger)
    collector.client.get_entity.side_effect = tg_errors.FloodWaitError(request=None, capture=120)

    batch = await collector.scan(_source(), None, _context())

    assert batch.outcome == CollectionOutcome.RATE_LIMITED
    assert batch.adapter_state.get("retry_after_seconds") == 120


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unauthorized_session_maps_to_auth_required(sample_config, mock_logger):
    """An existing-but-unauthorized session fails soft as AUTH_REQUIRED."""
    collector = _collector(sample_config, mock_logger)
    collector.client.connect = AsyncMock(return_value=True)
    collector.client.is_user_authorized = AsyncMock(return_value=False)

    with patch("src.providers.telegram.os.path.exists", return_value=True):
        batch = await collector.scan(_source(), None, _context())

    assert batch.outcome == CollectionOutcome.AUTH_REQUIRED
    assert batch.error_kind == "session_unauthorized"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_session_file_maps_to_account_action_required(sample_config, mock_logger):
    """A missing session file means a human must authenticate once."""
    collector = _collector(sample_config, mock_logger)

    with patch("src.providers.telegram.os.path.exists", return_value=False):
        batch = await collector.scan(_source(), None, _context())

    assert batch.outcome == CollectionOutcome.ACCOUNT_ACTION_REQUIRED
    assert batch.error_kind == "session_missing"
