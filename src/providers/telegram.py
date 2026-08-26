"""
Telegram collector adapter: Telethon messages -> provider-neutral ObservedItems.

This module is a conversion boundary only: ``TelegramCollector.scan()`` fetches
messages over the Telegram user API and returns a
:class:`~src.ingestion.models.CollectionBatch`; it never touches the database.
Session/authentication/link/sender/media helpers are shared with the legacy
:class:`src.collector.MessageCollector` (which delegates here) so the
interactive ``python -m src.collector`` authentication flow stays unchanged.

Item identity: ``external_id`` is ``str(message.id)`` scoped by Source; the
forum topic id is observation metadata, never part of identity.

Scan strategy (two passes over read-only provider data):

* **Window pass** — every scan fetches the newest ``limit`` messages inside a
  ``lookback_hours`` window WITHOUT any id filter. Edited messages keep their
  Telegram id, so re-observing them inside the window is what makes revision 2+
  reachable at all; unchanged content dedupes to nothing at the DB layer.
* **Catch-up pass** — when the window's oldest id sits above
  ``watermark + 1``, messages between the watermark and the window may exist
  (burst longer than the window, downtime). The gap is paged backwards in
  bounded chunks until the watermark is reached or ``MAX_CATCHUP_PAGES`` full
  pages were consumed. In the capped case the checkpoint only advances to the
  oldest fetched id minus one, so the remaining gap survives into the next
  scan instead of being silently skipped.

Correctness never depends on any of this bookkeeping: identity stays
``UNIQUE(source_id, external_id)`` and revisions dedupe by content hash.

Expected source-level failures never raise out of ``scan()``: they are mapped
onto :class:`~src.ingestion.models.CollectionOutcome` values with a short typed
``error_kind`` string on the batch.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from telethon import TelegramClient, functions, types
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    UserBannedInChannelError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
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

# Upper bound on watermark-catch-up paging per scan. When hit, the checkpoint
# advances only to the oldest fetched id minus one, so the next scan resumes
# the remaining gap instead of skipping it.
MAX_CATCHUP_PAGES = 3

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
    _ErrorRule((ChatAdminRequiredError,), CollectionOutcome.AUTH_REQUIRED, "chat_admin_required"),
    _ErrorRule(
        (ChannelPrivateError, UserBannedInChannelError),
        CollectionOutcome.SOURCE_NOT_FOUND,
        "channel_private",
    ),
    _ErrorRule(
        (ChannelInvalidError, UsernameInvalidError, UsernameNotOccupiedError),
        CollectionOutcome.SOURCE_NOT_FOUND,
        "channel_invalid",
    ),
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

    _scan_semaphore: asyncio.Semaphore | None = None

    @classmethod
    def _get_semaphore(cls) -> asyncio.Semaphore:
        if cls._scan_semaphore is None:
            cls._scan_semaphore = asyncio.Semaphore(1)
        return cls._scan_semaphore

    def __init__(
        self,
        config: Config,
        logger: logging.Logger | None = None,
        client: TelegramClient | None = None,
    ):
        self.config = config
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self.client = client if client is not None else build_user_client(config)
        # Long-lived worker collectors connect once per process, not per scan;
        # resolved entities are cached per source external id.
        self._connected = False
        self._entities: dict[str, Any] = {}

    async def scan(
        self,
        source: Source,
        checkpoint: CollectionCheckpoint | None,
        context: CollectionContext,
    ) -> CollectionBatch:
        """Fetch new messages for one source and convert them; never raises."""
        async with self._get_semaphore():
            started_at = context.now
            max_attempts = 3
            last_error: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    await self._ensure_ready()
                    channel_config = resolve_channel_config(
                        self.config.channels, source.external_id
                    )
                    entity = await self._resolve_entity(source)
                    entries, gap_resolved, lowest_fetched_id = await self._fetch_incremental(
                        source, entity, checkpoint, context, channel_config
                    )
                    return await self._convert_entries(
                        source,
                        channel_config,
                        entity,
                        entries,
                        checkpoint=checkpoint,
                        started_at=started_at,
                        gap_resolved=gap_resolved,
                        lowest_fetched_id=lowest_fetched_id,
                    )
                except Exception as error:
                    last_error = error
                    outcome, error_kind, extra_state = _map_error(error)
                    if outcome == CollectionOutcome.TRANSIENT and attempt < max_attempts:
                        self.logger.warning(
                            "Transient collection error for %s (attempt %d/%d): %s; retrying in %.1fs...",
                            source.name,
                            attempt,
                            max_attempts,
                            error,
                            attempt * 1.0,
                        )
                        await asyncio.sleep(attempt * 1.0)
                        continue

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
            outcome, error_kind, extra_state = _map_error(
                last_error or RuntimeError("collection failed")
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

    async def _ensure_ready(self) -> None:
        """Connect (once per collector lifetime) and reconnect if socket dropped."""
        if self._connected:
            is_conn = getattr(self.client, "is_connected", None)
            if callable(is_conn):
                with suppress(Exception):
                    res = is_conn()
                    if inspect.isawaitable(res):
                        res = await res
                    if res:
                        return
            elif is_conn is True:
                return
        await ensure_connected(self.client, self.logger)
        self._connected = True

    async def _resolve_entity(self, source: Source) -> Any:
        """Resolve the channel entity once per source; fallback to URL if external_id fails."""
        if source.external_id is None and not source.url:
            raise ValueError(f"Source {source.id} has no external_id or url")
        cache_key = source.external_id or source.url or str(source.id)
        entity = self._entities.get(cache_key)
        if entity is None:
            peer: Any = source.external_id
            if isinstance(peer, str) and (peer.startswith("-") or peer.isdigit()):
                try:
                    peer = int(peer)
                except ValueError:
                    pass
            try:
                entity = await self.client.get_entity(peer)
            except Exception:
                if source.url:
                    username = source.url.rstrip("/").split("/")[-1]
                    if username and not username.startswith("-") and not username.isdigit():
                        entity = await self.client.get_entity(username)
                    else:
                        raise
                else:
                    raise
            self._entities[cache_key] = entity
        return entity

    async def _convert_entries(
        self,
        source: Source,
        channel_config: ChannelConfig | None,
        entity: Any,
        entries: list[tuple[Any, int | None]],
        *,
        checkpoint: CollectionCheckpoint | None,
        started_at: datetime,
        gap_resolved: bool = True,
        lowest_fetched_id: int | None = None,
    ) -> CollectionBatch:
        """Convert fetched Telethon messages into one successful CollectionBatch."""
        observed_at = datetime.now(timezone.utc)
        items: list[ObservedItem] = []
        assets: list[ObservedAsset] = []
        seen_external_ids: set[str] = set()
        fetched_ids: list[int] = []

        for message, topic_id in sorted(entries, key=lambda entry: entry[0].date):
            fetched_ids.append(int(message.id))
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
        high_watermark: int | None
        if not gap_resolved and lowest_fetched_id is not None:
            high_watermark = lowest_fetched_id - 1
        else:
            watermark_candidates = fetched_ids + (
                [int(prior_watermark)] if isinstance(prior_watermark, int) else []
            )
            high_watermark = max(watermark_candidates) if watermark_candidates else None

        adapter_state: dict[str, JSONValue] = {
            "high_watermark_message_id": high_watermark,
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
        entity: Any,
        checkpoint: CollectionCheckpoint | None,
        context: CollectionContext,
        channel_config: ChannelConfig | None,
    ) -> tuple[list[tuple[Any, int | None]], bool, int | None]:
        """Fetch raw Telethon messages as ([(message, topic_id), ...], gap_resolved, lowest_fetched_id).

        Scan strategy:
        1. Window pass: fetch newest messages inside lookback window without id filter.
        2. Catch-up pass: if watermark exists and oldest message > watermark + 1, page
           backwards down to the watermark up to MAX_CATCHUP_PAGES.
        """
        options: dict[str, Any] = dict(context.options or {})
        options.update(source.collector_options or {})
        lookback_hours = int(options.get("lookback_hours") or DEFAULT_LOOKBACK_HOURS)
        limit = int(
            options.get("max_messages_per_channel") or self.config.settings.max_messages_per_channel
        )
        now = datetime.now(timezone.utc)
        watermark = self._min_id(checkpoint)
        lookback_time = now - timedelta(hours=lookback_hours)

        entries: list[tuple[Any, int | None]] = []
        gap_resolved = True
        lowest_fetched_id: int | None = None

        if channel_config is not None and channel_config.topics:
            for topic in channel_config.topics:
                # Window pass
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
                        min_id=0,
                        hash=0,
                        top_msg_id=topic.id,
                    )
                )
                topic_messages = list(response.messages)
                entries.extend((message, topic.id) for message in topic_messages)

                oldest_id = min((m.id for m in topic_messages), default=None)
                if oldest_id is not None:
                    lowest_fetched_id = (
                        oldest_id
                        if lowest_fetched_id is None
                        else min(lowest_fetched_id, oldest_id)
                    )

                # Catch-up pass if needed; pages backwards down to the
                # watermark (bounded) exactly like the non-topic branch, so a
                # gap larger than one page is not partially skipped.
                if watermark is not None and (oldest_id is None or oldest_id > watermark + 1):
                    current_offset_id = oldest_id if oldest_id is not None else 0
                    pages = 0
                    while pages < MAX_CATCHUP_PAGES:
                        pages += 1
                        catchup_response = await self.client(
                            functions.messages.SearchRequest(
                                peer=entity,
                                q="",
                                filter=types.InputMessagesFilterEmpty(),
                                min_date=None,
                                max_date=now,
                                offset_id=current_offset_id,
                                add_offset=0,
                                limit=limit,
                                max_id=0,
                                min_id=watermark,
                                hash=0,
                                top_msg_id=topic.id,
                            )
                        )
                        catchup_msgs = list(catchup_response.messages)
                        if not catchup_msgs:
                            break
                        entries.extend((message, topic.id) for message in catchup_msgs)
                        catchup_oldest = min(m.id for m in catchup_msgs)
                        lowest_fetched_id = (
                            catchup_oldest
                            if lowest_fetched_id is None
                            else min(lowest_fetched_id, catchup_oldest)
                        )
                        if catchup_oldest <= watermark + 1 or len(catchup_msgs) < limit:
                            break
                        current_offset_id = catchup_oldest
                    # Budget exhausted with the gap still open: the unfetched
                    # span between the stop point and the watermark must NOT
                    # be jumped over by a watermark advance (same contract as
                    # the non-topic branch below).
                    if current_offset_id > watermark + 1 and pages >= MAX_CATCHUP_PAGES:
                        gap_resolved = False
        else:
            # 1. Window pass
            window_messages: list[tuple[Any, int | None]] = []
            async for message in self.client.iter_messages(entity, limit=limit, offset_date=now):
                if message.date < lookback_time:
                    break
                window_messages.append((message, None))

            entries.extend(window_messages)
            oldest_id = min((m.id for m, _ in window_messages), default=None)
            if oldest_id is not None:
                lowest_fetched_id = oldest_id

            # 2. Catch-up pass
            if watermark is not None and (oldest_id is None or oldest_id > watermark + 1):
                current_offset_id = oldest_id if oldest_id is not None else 0
                page_size = len(window_messages) if len(window_messages) > 0 else limit
                pages = 0
                gap_closed = False
                while pages < MAX_CATCHUP_PAGES and not gap_closed:
                    pages += 1
                    page_messages: list[tuple[Any, int | None]] = []
                    kwargs: dict[str, Any] = {
                        "limit": limit,
                        "min_id": watermark,
                        "offset_id": current_offset_id,
                    }
                    async for message in self.client.iter_messages(entity, **kwargs):
                        page_messages.append((message, None))

                    if not page_messages:
                        gap_closed = True
                        break

                    entries.extend(page_messages)
                    page_oldest = min(m.id for m, _ in page_messages)
                    lowest_fetched_id = (
                        page_oldest
                        if lowest_fetched_id is None
                        else min(lowest_fetched_id, page_oldest)
                    )
                    if page_oldest <= watermark + 1 or len(page_messages) < page_size:
                        gap_closed = True
                        break
                    current_offset_id = page_oldest

                if not gap_closed and pages >= MAX_CATCHUP_PAGES:
                    gap_resolved = False

        return entries, gap_resolved, lowest_fetched_id

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
        media_kind = classify_media(message)
        if not text and not media_kind:
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
            "media_kinds": [media_kind] if media_kind else [],
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
        media_kind = classify_media(message)
        if media_kind not in ASSET_MEDIA_KINDS:
            return None

        mime_type: str | None = MIME_TYPE_BY_TOKEN.get(media_kind)
        asset_metadata: dict[str, JSONValue] = {}
        content_hash: str | None = None
        if media_kind == "photo":
            photo = getattr(message.media, "photo", None)
            photo_id = getattr(photo, "id", None)
            if photo_id is not None:
                content_hash = str(photo_id)
        elif media_kind == "document" or mime_type is None:
            document = getattr(message.media, "document", None)
            if document is not None:
                mime_type = getattr(document, "mime_type", None) or mime_type
                size = getattr(document, "size", None)
                unique_id = getattr(document, "id", None)
                if size is not None:
                    asset_metadata["size"] = int(size)
                if unique_id is not None:
                    asset_metadata["unique_id"] = int(unique_id)
                    content_hash = str(unique_id)
                attributes = getattr(document, "attributes", None) or ()
                for attribute in attributes:
                    file_name = getattr(attribute, "file_name", None)
                    if file_name:
                        asset_metadata["file_name"] = file_name
                        break

        return ObservedAsset(
            item_external_id=str(message.id),
            kind=media_kind,
            external_url=None,
            mime_type=mime_type,
            content_hash=content_hash,
            metadata=asset_metadata,
        )
