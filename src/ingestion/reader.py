"""Persisted-source compatibility reader for the legacy Message pipeline.

``SourceRevisionReader`` is a migration adapter (Plan 2 Task 7): once
``settings.persistent_ingestion`` is true, publication builders read their
inputs from the generic source history persisted by the ingestion path
instead of collecting live over Telethon. It reconstructs the legacy
:class:`src.collector.Message` objects that summarizers, formatters and the
article pipeline already consume, so downstream code is untouched during the
transition.

Read semantics (documented choices):

* **Window membership** is decided by ``COALESCE(published_at,
  first_collected_at)`` falling inside ``[since, until]`` — the moment the
  content became publicly attributable, falling back to when it was first
  observed. Filtering on revisions' ``collected_at`` was rejected: an edit
  re-collected days later would wrongly pull the original post out of its
  publication window.
* **Revision selection** is "latest revision observed by ``until``": the
  highest ``revision_no`` whose ``collected_at <= until``. An item whose
  newest revision was only collected after ``until`` is skipped — it had not
  been materialized yet at that point on the timeline.
* **Logical channel keys** mirror the legacy collector's naming so
  downstream config resolution and filters keep working: ``source.name``,
  or ``"{source.name} — {topic_id}"`` when observation metadata carries a
  forum topic id. The separator matches ``_channel_config_for_name``, which
  resolves such keys to the parent channel by prefix; topic display names
  remain a legacy-config concern because persistence stores topic ids only.
* **Media flags** come from observation metadata (``has_media``/media_kinds)
  plus the revision's bound assets; a media-only post renders the legacy
  ``"[{media_type}]"`` placeholder text instead of an empty string.
  Media types are canonical tokens ("photo", "video", ...), not the legacy
  localized display strings — the adapter has no output-language context.
* **No fallback**: PostgreSQL failures propagate as explicit operational
  errors. Silently degrading to live Telegram would violate persistent-
  ingestion semantics (the digest must reflect the persisted snapshot).

The repository parameter follows the plan's wiring shape (callers construct a
single :class:`IngestionRepository` alongside the unit of work); the read
itself is one windowed SQL statement over caller-owned connections.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.collector import Message
from src.db.uow import DatabaseUnitOfWork
from src.ingestion.repository import IngestionRepository

PLATFORM_TELEGRAM = "telegram"

# Matches the legacy collector's logical topic key separator exactly.
TOPIC_SEPARATOR = " — "

_READ_SQL = """
    SELECT s.id AS source_id,
           s.name AS source_name,
           s.external_id AS source_external_id,
           i.id AS item_id,
           i.external_id AS item_external_id,
           i.author_name,
           i.canonical_url,
           i.published_at,
           i.first_collected_at,
           i.metadata,
           r.text_content,
           COALESCE(array_agg(a.kind) FILTER (WHERE a.id IS NOT NULL), '{}') AS asset_kinds
    FROM sources s
    JOIN editions e ON e.slug = %s AND e.enabled
    JOIN source_editions se ON se.edition_id = e.id AND se.source_id = s.id
    JOIN source_items i ON i.source_id = s.id
    JOIN LATERAL (
        SELECT rev.id AS revision_id, rev.text_content
        FROM source_item_revisions rev
        WHERE rev.source_item_id = i.id AND rev.collected_at <= %s
        ORDER BY rev.revision_no DESC
        LIMIT 1
    ) r ON true
    LEFT JOIN source_assets a ON a.source_item_revision_id = r.revision_id
    WHERE s.platform = %s
      AND s.enabled
      AND COALESCE(i.published_at, i.first_collected_at) >= %s
      AND COALESCE(i.published_at, i.first_collected_at) <= %s
    GROUP BY s.id, i.id, r.revision_id, r.text_content
    ORDER BY s.id, i.id
"""


def _logical_channel_key(source_name: str, metadata: dict) -> str:
    """Legacy key shape: plain channel name, or 'channel — topic' for forums."""
    topic_id = metadata.get("topic_id")
    if isinstance(topic_id, int):
        return f"{source_name}{TOPIC_SEPARATOR}{topic_id}"
    return source_name


def _channel_identifier(external_id: str | None) -> int | str | None:
    """Best-effort Telethon-resolvable id from the Source external id."""
    if external_id is None:
        return None
    if external_id.startswith("@"):
        return external_id
    bare = external_id[len("-100") :] if external_id.startswith("-100") else external_id
    try:
        return int(bare)
    except ValueError:
        return external_id


def _fallback_link(external_id: str | None, message_id: int) -> str:
    """Rebuild the t.me link the way the legacy collector does."""
    if external_id is None:
        return "#"
    if external_id.startswith("@"):
        return f"https://t.me/{external_id[1:]}/{message_id}"
    if external_id.startswith("-100"):
        return f"https://t.me/c/{external_id[len('-100') :]}/{message_id}"
    return "#"


def _first_media_token(metadata: dict, asset_kinds: list[str]) -> str:
    """Canonical media token from observation metadata, else revision assets."""
    kinds = metadata.get("media_kinds")
    if isinstance(kinds, list):
        for kind in kinds:
            if isinstance(kind, str) and kind:
                return kind
    return asset_kinds[0] if asset_kinds else ""


def _to_message(row: tuple) -> Message | None:
    """Reconstruct one legacy Message from a joined history row."""
    (
        _source_id,
        source_name,
        source_external_id,
        _item_id,
        item_external_id,
        author_name,
        canonical_url,
        published_at,
        first_collected_at,
        metadata,
        text_content,
        asset_kinds,
    ) = row

    # psycopg hands JSONB back as a dict and text[] as a list.
    metadata = metadata or {}
    asset_kinds = list(asset_kinds or [])
    message_id = int(item_external_id)

    has_media = bool(metadata.get("has_media")) or bool(asset_kinds)
    media_type = _first_media_token(metadata, asset_kinds)
    text = text_content or ""
    if not text and not has_media:
        # Service posts never entered history via the ingestion adapters either.
        return None
    if not text:
        text = f"[{media_type}]"

    reply_to_id = metadata.get("reply_to_id")
    topic_id = metadata.get("topic_id")
    return Message(
        text=text,
        sender=author_name or "Unknown",
        timestamp=published_at or first_collected_at,
        link=canonical_url or _fallback_link(source_external_id, message_id),
        channel_name=_logical_channel_key(source_name, metadata),
        has_media=has_media,
        media_type=media_type,
        message_id=message_id,
        reply_to_id=reply_to_id if isinstance(reply_to_id, int) else None,
        topic_id=topic_id if isinstance(topic_id, int) else None,
        forward_origin_name=metadata.get("forward_from_name"),
        forward_origin_username=metadata.get("forward_from_username"),
        channel_id=_channel_identifier(source_external_id),
    )


class SourceRevisionReader:
    """Migration adapter: rebuild legacy Messages from persisted source history."""

    def __init__(self, uow: DatabaseUnitOfWork, repository: IngestionRepository) -> None:
        self.uow = uow
        self.repository = repository

    async def read_telegram_messages(
        self, edition_slug: str, since: datetime, until: datetime
    ) -> dict[str, list[Message]]:
        """Read one edition's telegram history as legacy per-channel messages.

        Args:
            edition_slug: Edition whose bound sources feed the result
            since: Window start (inclusive), compared against the item's
                published time falling back to its first collection time
            until: Window end (inclusive); also bounds revision visibility

        Returns:
            Legacy ``dict[str, list[Message]]`` keyed by logical channel name,
            each list chronological. Empty dict when nothing matches.

        Raises:
            Exception: PostgreSQL failures propagate explicitly; callers must
                never fall back to live collection while the flag is enabled.
        """
        async with self.uow.transaction() as conn:
            cursor = await conn.execute(
                _READ_SQL,
                (edition_slug, until, PLATFORM_TELEGRAM, since, until),
            )
            rows = await cursor.fetchall()

        messages_by_channel: dict[str, list[Message]] = {}
        for row in rows:
            message = _to_message(row)
            if message is None:
                continue
            messages_by_channel.setdefault(message.channel_name, []).append(message)
        for messages in messages_by_channel.values():
            messages.sort(key=lambda m: m.timestamp or datetime.min.replace(tzinfo=timezone.utc))
        return messages_by_channel
