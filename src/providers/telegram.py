"""
Telegram collector adapter: Telethon messages -> provider-neutral ObservedItems.

This module is a conversion boundary only: ``TelegramCollector.scan()`` fetches
messages over the Telegram user API and returns a
:class:`~src.ingestion.models.CollectionBatch`; it never touches the database.
Session/authentication/link/sender/media helpers are shared with the legacy
:class:`src.collector.MessageCollector` (which delegates here) so the
interactive ``python -m src.collector`` authentication flow stays unchanged.

Item identity: ``external_id`` is ``str(message.id)`` scoped by Source; the
forum topic id is observation metadata, never part of identity. Checkpointing
stores the highest observed message id; incremental scans pass it as Telethon's
``min_id`` so only newer history is fetched. Without a checkpoint the adapter
fetches a recent window: ``lookback_hours`` from source collector_options or
context options, defaulting to 24 hours.

Expected source-level failures never raise out of ``scan()``: they are mapped
onto :class:`~src.ingestion.models.CollectionOutcome` values with a short typed
``error_kind`` string on the batch.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import TelegramClient, functions, types
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    FloodWaitError,
)

from src.config_loader import ChannelConfig, Config, effective_source_type
from src.domain.sources import Source
from src.ingestion.models import (
    CollectionBatch,
    CollectionCheckpoint,
    CollectionOutcome,
    JSONValue,
    ObservedAsset,
    ObservedItem,
)
from src.ingestion.protocol import CollectionContext

DEFAULT_LOOKBACK_HOURS = 24

SESSION_PATH = "sessions/user.session"

ASSET_MEDIA_KINDS = frozenset({"photo", "video", "audio", "voice", "document"})

MIME_TYPE_BY_TOKEN = {"photo": "image/jpeg"}

MEDIA_TOKENS = {
    "photo": "media_photo",
    "video": "media_video",
    "audio": "media_audio",
    "document": "media_document",
    "voice": "media_voice",
    "poll": "media_poll",
    "geo": "media_geo",
    "other": "media_other",
}


class TelegramSessionMissing(RuntimeError):
    """The local Telegram session file does not exist yet."""


class TelegramSessionUnauthorized(RuntimeError):
    """The session file exists but is not authorized for the user API."""


def build_user_client(config: Config) -> TelegramClient:
    """Build a Telethon client bound to the shared user session file."""
    return TelegramClient("sessions/user", config.telegram_api_id, config.telegram_api_hash)


async def ensure_connected(client: Any, logger: logging.Logger | None) -> None:
    """Connect using an existing user session, raising typed RuntimeErrors.

    Requires a pre-authenticated session file at sessions/user.session;
    create one by running: python -m src.collector
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    if not os.path.exists(SESSION_PATH):
        raise TelegramSessionMissing(
            f"Telegram user session not found at '{SESSION_PATH}'. "
            "Create one by running: python -m src.collector"
        )
    await client.connect()
    if not await client.is_user_authorized():
        raise TelegramSessionUnauthorized(
            "Telegram user session exists but is not authorized. "
            "Re-authenticate by running: python -m src.collector"
        )
    log.info("Connected to Telegram User API")
    try:
        dialogs = await client.get_dialogs()
        log.info(f"Cached {len(dialogs)} dialogs for entity resolution")
    except Exception as e:
        log.warning(f"Could not cache dialogs: {e}")


async def sender_display_name(message: Any) -> str:
    """Resolve the human-readable sender name, falling back to 'Unknown'."""
    try:
        if message.sender:
            sender = await message.get_sender()
            if hasattr(sender, "first_name"):
                name = str(sender.first_name)
                if hasattr(sender, "last_name") and sender.last_name:
                    name += f" {sender.last_name}"
                return name
            elif hasattr(sender, "title"):
                return str(sender.title)
            elif hasattr(sender, "username"):
                return f"@{sender.username}"
        return "Unknown"
    except Exception:
        return "Unknown"


def message_link(entity: Any, message_id: int) -> str:
    """Build a clickable t.me link for one message."""
    try:
        if hasattr(entity, "username") and entity.username:
            return f"https://t.me/{entity.username}/{message_id}"
        elif hasattr(entity, "id"):
            channel_id = str(entity.id).replace("-100", "")
            return f"https://t.me/c/{channel_id}/{message_id}"
        else:
            return "#"
    except Exception:
        return "#"


def forward_origin(message: Any) -> tuple[str | None, str | None]:
    """Extract forward origin name and username if available."""
    fwd = getattr(message, "forward", None) or getattr(message, "fwd_from", None)
    if not fwd:
        return None, None

    name = getattr(fwd, "from_name", None)
    username = None

    chat = getattr(fwd, "chat", None)
    if chat:
        name = name or getattr(chat, "title", None) or getattr(chat, "first_name", None)
        username = getattr(chat, "username", None)

    sender = getattr(fwd, "sender", None)
    if sender:
        if not name:
            name = getattr(sender, "first_name", None) or getattr(sender, "title", None)
            if hasattr(sender, "last_name") and sender.last_name:
                name = f"{name} {sender.last_name}"
        username = username or getattr(sender, "username", None)

    post_author = getattr(fwd, "post_author", None)
    if post_author and not name:
        name = post_author

    return (str(name).strip() if name else None), (str(username).strip() if username else None)


def classify_media(message: Any) -> str:
    """Classify a message's media into a neutral token ('' when no media).

    Tokens map 1:1 onto ui_strings media_* keys and ObservedAsset kinds.
    """
    media = getattr(message, "media", None)
    if not media:
        return ""

    media_type = type(media).__name__

    if "Photo" in media_type:
        return "photo"
    elif "Video" in media_type or "Document" in media_type:
        if hasattr(media, "document"):
            mime = getattr(media.document, "mime_type", "") or ""
            if "video" in mime:
                return "video"
            elif "audio" in mime:
                return "audio"
            else:
                return "document"
        return "video"
    elif "Voice" in media_type or "Audio" in media_type:
        return "voice"
    elif "Poll" in media_type:
        return "poll"
    elif "Geo" in media_type or "Location" in media_type:
        return "geo"
    else:
        return "other"


def resolve_channel_config(
    channels: list[ChannelConfig], external_id: str | None
) -> ChannelConfig | None:
    """Find the legacy channel config matching a Source external id."""
    for channel in channels:
        if str(channel.id) == external_id:
            return channel
    return None


def resolve_effective_role(
    channel_config: ChannelConfig | None, source_role: str, topic_id: int | None
) -> str:
    """Editorial role per observation using existing topic > channel precedence."""
    if channel_config is not None:
        topic = None
        if topic_id is not None:
            topic = next((t for t in channel_config.topics if t.id == topic_id), None)
        return effective_source_type(channel_config, topic)
    return source_role


class _ErrorRule:
    """One row of the Telethon-exception -> outcome mapping table."""

    def __init__(
        self,
        exception_types: tuple[type[BaseException], ...],
        outcome: CollectionOutcome,
        error_kind: str,
        predicate: Callable[[BaseException], bool] | None = None,
    ):
        self.exception_types = exception_types
        self.outcome = outcome
        self.error_kind = error_kind
        self.predicate = predicate


def _is_unresolvable_entity(error: BaseException) -> bool:
    return isinstance(error, ValueError) and "Could not find the input entity" in str(error)


ERROR_RULES: tuple[_ErrorRule, ...] = (
    _ErrorRule((FloodWaitError,), CollectionOutcome.RATE_LIMITED, "flood_wait"),
    _ErrorRule(
        (TelegramSessionMissing,),
        CollectionOutcome.ACCOUNT_ACTION_REQUIRED,
        "session_missing",
    ),
    _ErrorRule(
        (TelegramSessionUnauthorized,),
        CollectionOutcome.AUTH_REQUIRED,
        "session_unauthorized",
    ),
    _ErrorRule(
        (AuthKeyUnregisteredError,),
        CollectionOutcome.AUTH_REQUIRED,
        "auth_key_unregistered",
    ),
    _ErrorRule((ChannelPrivateError,), CollectionOutcome.SOURCE_NOT_FOUND, "channel_private"),
    _ErrorRule(
        (ValueError,),
        CollectionOutcome.SOURCE_NOT_FOUND,
        "entity_not_found",
        _is_unresolvable_entity,
    ),
    _ErrorRule((TimeoutError,), CollectionOutcome.TRANSIENT, "network_timeout"),
    _ErrorRule((OSError,), CollectionOutcome.TRANSIENT, "network_error"),
)


def _map_error(
    error: BaseException,
) -> tuple[CollectionOutcome, str, dict[str, JSONValue]]:
    """Map an exception onto (outcome, error_kind, extra adapter_state).

    Unexpected exceptions degrade to TRANSIENT so one broken source cannot
    stall the collection loop; the exception class name rides along as
    ``adapter_state['exception_type']`` for diagnosis.
    """
    for rule in ERROR_RULES:
        if isinstance(error, rule.exception_types) and (
            rule.predicate is None or rule.predicate(error)
        ):
            extra_state: dict[str, JSONValue] = {}
            if isinstance(error, FloodWaitError):
                extra_state["retry_after_seconds"] = int(getattr(error, "seconds", 0) or 0)
            return rule.outcome, rule.error_kind, extra_state
    return CollectionOutcome.TRANSIENT, "unexpected", {"exception_type": type(error).__name__}


class TelegramCollector:
    """Scans one Telegram Source into a CollectionBatch of ObservedItems."""

    def __init__(
        self,
        config: Config,
        logger: logging.Logger | None = None,
        client: TelegramClient | None = None,
    ):
        self.config = config
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self.client = client if client is not None else build_user_client(config)

    async def scan(
        self,
        source: Source,
        checkpoint: CollectionCheckpoint | None,
        context: CollectionContext,
    ) -> CollectionBatch:
        """Fetch new messages for one source and convert them; never raises."""
        started_at = context.now
        try:
            await ensure_connected(self.client, self.logger)
            channel_config = resolve_channel_config(self.config.channels, source.external_id)
            entity, entries = await self._fetch_incremental(
                source, checkpoint, context, channel_config
            )
        except Exception as error:
            outcome, error_kind, extra_state = _map_error(error)
            self.logger.warning(
                f"✗ {source.name}: collection failed ({outcome.value}, {error_kind}): {error}"
            )
            return CollectionBatch(
                outcome=outcome,
                items=(),
                assets=(),
                state_events=(),
                adapter_state=dict(extra_state),
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                error_kind=error_kind,
            )

        observed_at = datetime.now(timezone.utc)
        min_id = self._min_id(checkpoint)
        items: list[ObservedItem] = []
        assets: list[ObservedAsset] = []
        seen_external_ids: set[str] = set()
        fetched_ids: list[int] = []

        for message, topic_id in sorted(entries, key=lambda entry: entry[0].date):
            fetched_ids.append(int(message.id))
            if min_id is not None and int(message.id) <= min_id:
                continue
            external_id = str(message.id)
            if external_id in seen_external_ids:
                continue
            seen_external_ids.add(external_id)
            item = await self._to_observed_item(
                source, channel_config, entity, message, topic_id, observed_at=observed_at
            )
            if item is None:
                continue
            items.append(item)
            asset = self._asset_for(message)
            if asset is not None:
                assets.append(asset)

        completed_at = datetime.now(timezone.utc)
        prior_watermark = (
            checkpoint.adapter_state.get("high_watermark_message_id") if checkpoint else None
        )
        watermark_candidates = fetched_ids + (
            [int(prior_watermark)] if isinstance(prior_watermark, int) else []
        )
        adapter_state: dict[str, JSONValue] = {
            "high_watermark_message_id": max(watermark_candidates)
            if watermark_candidates
            else None,
            "last_success_at": completed_at.isoformat(),
        }
        return CollectionBatch(
            outcome=CollectionOutcome.SUCCESS,
            items=tuple(items),
            assets=tuple(assets),
            state_events=(),
            adapter_state=adapter_state,
            started_at=started_at,
            completed_at=completed_at,
        )

    async def aclose(self) -> None:
        """Release the underlying Telethon client."""
        await self.client.disconnect()

    async def _fetch_incremental(
        self,
        source: Source,
        checkpoint: CollectionCheckpoint | None,
        context: CollectionContext,
        channel_config: ChannelConfig | None,
    ) -> tuple[Any, list[tuple[Any, int | None]]]:
        """Fetch raw Telethon messages as (entity, [(message, topic_id), ...]).

        With a checkpoint high-watermark the scan passes it as Telethon's
        min_id; without one it fetches a recent lookback window (24h default).
        Forum sources fetch per selected topic via top_msg_id, mirroring the
        legacy collector.
        """
        options: dict[str, Any] = dict(context.options or {})
        options.update(source.collector_options or {})
        lookback_hours = int(options.get("lookback_hours") or DEFAULT_LOOKBACK_HOURS)
        limit = int(
            options.get("max_messages_per_channel") or self.config.settings.max_messages_per_channel
        )
        now = datetime.now(timezone.utc)
        lookback_time = now - timedelta(hours=lookback_hours)
        min_id = self._min_id(checkpoint)

        entity = await self.client.get_entity(source.external_id)
        entries: list[tuple[Any, int | None]] = []

        if channel_config is not None and channel_config.topics:
            for topic in channel_config.topics:
                response = await self.client(
                    functions.messages.SearchRequest(
                        peer=entity,
                        q="",
                        filter=types.InputMessagesFilterEmpty(),
                        min_date=lookback_time,
                        max_date=now,
                        offset_id=0,
                        add_offset=0,
                        limit=limit,
                        max_id=0,
                        min_id=min_id or 0,
                        hash=0,
                        top_msg_id=topic.id,
                    )
                )
                entries.extend((message, topic.id) for message in response.messages)
        else:
            kwargs: dict[str, Any] = {"limit": limit, "offset_date": now}
            if min_id:
                kwargs["min_id"] = min_id
            async for message in self.client.iter_messages(entity, **kwargs):
                if message.date < lookback_time:
                    break
                entries.append((message, None))

        return entity, entries

    @staticmethod
    def _min_id(checkpoint: CollectionCheckpoint | None) -> int | None:
        """Resume point as integer message id, preferring the explicit cursor."""
        if checkpoint is None:
            return None
        candidates = (checkpoint.cursor, checkpoint.adapter_state.get("high_watermark_message_id"))
        for candidate in candidates:
            if isinstance(candidate, int):
                return candidate
        return None

    async def _to_observed_item(
        self,
        source: Source,
        channel_config: ChannelConfig | None,
        entity: Any,
        message: Any,
        topic_id: int | None,
        *,
        observed_at: datetime,
    ) -> ObservedItem | None:
        """Convert one Telethon message; service posts without text/media are skipped."""
        text = message.text or ""
        media_token = classify_media(message)
        if not text and not media_token:
            return None

        reply_to = getattr(message, "reply_to", None)
        reply_to_msg_id = getattr(reply_to, "reply_to_msg_id", None) if reply_to else None
        reply_to_top_id = getattr(reply_to, "reply_to_top_id", None) if reply_to else None
        fwd_name, fwd_username = forward_origin(message)
        fwd = getattr(message, "forward", None) or getattr(message, "fwd_from", None)
        fwd_date = getattr(fwd, "date", None) if fwd else None
        effective_topic_id = topic_id if topic_id is not None else reply_to_top_id

        metadata: dict[str, JSONValue] = {
            "topic_id": effective_topic_id,
            "reply_to_id": reply_to_msg_id,
            "has_media": message.media is not None,
            "media_kinds": [token for token in [media_token] if token],
            "effective_source_role": resolve_effective_role(
                channel_config, source.role, effective_topic_id
            ),
        }
        if fwd_name:
            metadata["forward_from_name"] = fwd_name
        if fwd_username:
            metadata["forward_from_username"] = fwd_username
        if fwd_date is not None:
            metadata["forward_date"] = fwd_date.isoformat()
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id is not None:
            metadata["grouped_id"] = str(grouped_id)

        return ObservedItem(
            kind="telegram_message",
            external_id=str(message.id),
            text=text,
            author_name=await sender_display_name(message),
            published_at=message.date,
            canonical_url=message_link(entity, message.id),
            metadata=metadata,
            observed_at=observed_at,
            parent_external_id=str(reply_to_msg_id) if reply_to_msg_id else None,
            root_external_id=str(reply_to_top_id) if reply_to_top_id else None,
        )

    def _asset_for(self, message: Any) -> ObservedAsset | None:
        """Map downloadable media (photo/video/audio/voice/document) to an asset."""
        token = classify_media(message)
        if token not in ASSET_MEDIA_KINDS:
            return None

        mime_type: str | None = MIME_TYPE_BY_TOKEN.get(token)
        asset_metadata: dict[str, JSONValue] = {}
        if mime_type is None:
            document = getattr(message.media, "document", None)
            if document is not None:
                mime_type = getattr(document, "mime_type", None)
                size = getattr(document, "size", None)
                unique_id = getattr(document, "id", None)
                if size is not None:
                    asset_metadata["size"] = int(size)
                if unique_id is not None:
                    asset_metadata["unique_id"] = int(unique_id)
                attributes = getattr(document, "attributes", None) or ()
                for attribute in attributes:
                    file_name = getattr(attribute, "file_name", None)
                    if file_name:
                        asset_metadata["file_name"] = file_name
                        break

        return ObservedAsset(
            item_external_id=str(message.id),
            kind=token,
            external_url=None,
            mime_type=mime_type,
            content_hash=None,
            metadata=asset_metadata,
        )
