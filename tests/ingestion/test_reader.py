"""SourceRevisionReader: persisted source history -> legacy Message adapter.

Task 7 pins the compatibility read path used when persistent_ingestion=true:
window filtering on COALESCE(published_at, first_collected_at), latest
revision wins after edits, media flags from revision metadata/assets, and
legacy logical channel naming. Env-gated on TELEBRIEF_TEST_DATABASE_URL via
tests/ingestion/conftest.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.types.json import Jsonb

from src.collector import Message
from src.ingestion.reader import SourceRevisionReader
from src.ingestion.repository import IngestionRepository

UTC = timezone.utc
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

INSERT_EDITION = "INSERT INTO editions (slug, name) VALUES (%s, %s) RETURNING id"

INSERT_SOURCE = """
    INSERT INTO sources (platform, kind, external_id, url, name, enabled)
    VALUES ('telegram', 'channel', %s, %s, %s, %s)
    RETURNING id
"""

BIND_SOURCE = "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)"

INSERT_ITEM = """
    INSERT INTO source_items (
        source_id, kind, external_id, author_name, canonical_url,
        published_at, first_collected_at, metadata
    )
    VALUES (%s, 'telegram_message', %s, %s, %s, %s, %s, %s::jsonb)
    RETURNING id
"""

INSERT_REVISION = """
    INSERT INTO source_item_revisions (
        source_item_id, revision_no, collected_at, content_hash, text_content, payload
    )
    VALUES (%s, %s, %s, %s, %s, '{}'::jsonb)
    RETURNING id
"""

INSERT_ASSET = """
    INSERT INTO source_assets (source_item_revision_id, kind, mime_type)
    VALUES (%s, %s, %s)
"""


async def _edition(conn: psycopg.AsyncConnection, slug: str = "berdyansk") -> int:
    cursor = await conn.execute(INSERT_EDITION, (slug, "Бердянск"))
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _source(
    conn: psycopg.AsyncConnection,
    *,
    name: str = "Example",
    external_id: str = "-1004242",
    enabled: bool = True,
) -> int:
    url = f"https://t.me/{external_id[1:]}" if external_id.startswith("@") else None
    cursor = await conn.execute(INSERT_SOURCE, (external_id, url, name, enabled))
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _item(
    conn: psycopg.AsyncConnection,
    source_id: int,
    external_id: str,
    *,
    published: datetime | None,
    first_collected: datetime | None = None,
    metadata: dict | None = None,
) -> int:
    collected = first_collected if first_collected is not None else NOW - timedelta(hours=1)
    cursor = await conn.execute(
        INSERT_ITEM,
        (
            source_id,
            external_id,
            "Resident",
            f"https://t.me/example/{external_id}",
            published,
            collected,
            Jsonb(metadata or {}),
        ),
    )
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _revision(
    conn: psycopg.AsyncConnection,
    item_id: int,
    revision_no: int,
    collected_at: datetime,
    text: str | None,
) -> int:
    cursor = await conn.execute(
        INSERT_REVISION,
        (item_id, revision_no, collected_at, f"hash-{revision_no}", text),
    )
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _asset(conn: psycopg.AsyncConnection, revision_id: int, kind: str) -> None:
    await conn.execute(INSERT_ASSET, (revision_id, kind, "image/jpeg"))


@pytest.fixture
def reader(uow) -> SourceRevisionReader:
    return SourceRevisionReader(uow, IngestionRepository())


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_read_reconstructs_legacy_message_fields(reader, conn, source):
    """Every legacy Message field survives the round-trip from source history."""
    edition_id = await _edition(conn)
    await conn.execute(BIND_SOURCE, (source.id, edition_id))
    published = NOW - timedelta(hours=1)
    metadata = {
        "topic_id": None,
        "reply_to_id": 7,
        "has_media": True,
        "media_kinds": ["photo"],
        "forward_from_name": "Original",
        "forward_from_username": "orig",
    }
    item_id = await _item(conn, source.id, "42", published=published, metadata=metadata)
    revision_id = await _revision(conn, item_id, 1, published, "Воду отключили")
    await _asset(conn, revision_id, "photo")

    result = await reader.read_telegram_messages("berdyansk", NOW - timedelta(hours=24), NOW)

    assert list(result) == ["Example"]
    (message,) = result["Example"]
    assert isinstance(message, Message)
    assert message.text == "Воду отключили"
    assert message.sender == "Resident"
    assert message.timestamp == published
    assert message.link == "https://t.me/example/42"
    assert message.channel_name == "Example"
    assert message.has_media is True
    assert message.media_type == "photo"
    assert message.message_id == 42
    assert message.reply_to_id == 7
    assert message.topic_id is None
    assert message.forward_origin_name == "Original"
    assert message.forward_origin_username == "orig"
    assert message.channel_id == 4242


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_latest_revision_wins_after_edit(reader, conn, source):
    """Two revisions for one item: the newest one is rendered."""
    edition_id = await _edition(conn)
    await conn.execute(BIND_SOURCE, (source.id, edition_id))
    published = NOW - timedelta(hours=2)
    item_id = await _item(conn, source.id, "43", published=published)
    await _revision(conn, item_id, 1, published, "initial text")
    await _revision(conn, item_id, 2, published + timedelta(minutes=5), "edited text")

    result = await reader.read_telegram_messages("berdyansk", NOW - timedelta(hours=24), NOW)

    (message,) = result["Example"]
    assert message.text == "edited text"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_window_filters_on_published_else_first_collected(reader, conn, source):
    """Window membership uses COALESCE(published_at, first_collected_at); items with no
    revision observed by ``until`` are not yet renderable and are skipped."""
    edition_id = await _edition(conn)
    await conn.execute(BIND_SOURCE, (source.id, edition_id))

    inside = await _item(conn, source.id, "1", published=NOW - timedelta(hours=1))
    await _revision(conn, inside, 1, NOW - timedelta(hours=1), "inside")
    too_old = await _item(conn, source.id, "2", published=NOW - timedelta(hours=48))
    await _revision(conn, too_old, 1, NOW - timedelta(hours=48), "too old")
    undated = await _item(
        conn, source.id, "3", published=None, first_collected=NOW - timedelta(hours=2)
    )
    await _revision(conn, undated, 1, NOW - timedelta(hours=2), "undated falls back")
    not_yet_observed = await _item(
        conn,
        source.id,
        "4",
        published=NOW - timedelta(hours=3),
        first_collected=NOW + timedelta(hours=1),
    )
    await _revision(conn, not_yet_observed, 1, NOW + timedelta(hours=1), "collected late")
    boundary = await _item(conn, source.id, "5", published=NOW)
    await _revision(conn, boundary, 1, NOW, "on the until boundary")

    result = await reader.read_telegram_messages("berdyansk", NOW - timedelta(hours=24), NOW)

    # Per-channel lists are chronological (legacy collector behavior); timestamps
    # follow the same COALESCE(published_at, first_collected_at) rule.
    texts = [message.text for message in result["Example"]]
    assert texts == ["undated falls back", "inside", "on the until boundary"]
    timestamps = [message.timestamp for message in result["Example"]]
    assert timestamps == [NOW - timedelta(hours=2), NOW - timedelta(hours=1), NOW]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_media_only_message_rebuilds_placeholder_text(reader, conn, source):
    """A photo post stored without text renders like the legacy collector would."""
    edition_id = await _edition(conn)
    await conn.execute(BIND_SOURCE, (source.id, edition_id))
    published = NOW - timedelta(hours=1)
    metadata = {"topic_id": None, "reply_to_id": None, "has_media": True, "media_kinds": []}
    item_id = await _item(conn, source.id, "44", published=published, metadata=metadata)
    revision_id = await _revision(conn, item_id, 1, published, None)
    await _asset(conn, revision_id, "photo")

    result = await reader.read_telegram_messages("berdyansk", NOW - timedelta(hours=24), NOW)

    (message,) = result["Example"]
    assert message.text == "[photo]"
    assert message.has_media is True
    assert message.media_type == "photo"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_topic_metadata_uses_legacy_logical_channel_key(reader, conn):
    """topic_id in observation metadata keys messages as '{source} — {topic}'."""
    edition_id = await _edition(conn)
    topic_source = await _source(conn, external_id="-1004243")
    await conn.execute(BIND_SOURCE, (topic_source, edition_id))
    published = NOW - timedelta(hours=1)
    metadata = {"topic_id": 235525, "reply_to_id": None, "has_media": False}
    item_id = await _item(conn, topic_source, "45", published=published, metadata=metadata)
    await _revision(conn, item_id, 1, published, "topic post")

    result = await reader.read_telegram_messages("berdyansk", NOW - timedelta(hours=24), NOW)

    assert list(result) == ["Example — 235525"]
    (message,) = result["Example — 235525"]
    assert message.topic_id == 235525


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_disabled_and_unbound_sources_are_excluded(reader, conn, source):
    """Only enabled telegram sources bound to the edition feed the digest."""
    edition_id = await _edition(conn)
    disabled = await _source(conn, external_id="-1009999", enabled=False)
    await conn.execute(BIND_SOURCE, (disabled, edition_id))
    unbound = await _source(conn, external_id="-1008888")
    await conn.execute(BIND_SOURCE, (source.id, edition_id))

    for source_id, external_id in ((disabled, "91"), (unbound, "92"), (source.id, "93")):
        item_id = await _item(conn, source_id, external_id, published=NOW - timedelta(hours=1))
        await _revision(conn, item_id, 1, NOW - timedelta(hours=1), f"text {external_id}")

    result = await reader.read_telegram_messages("berdyansk", NOW - timedelta(hours=24), NOW)

    assert list(result) == ["Example"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unknown_edition_yields_no_messages(reader, conn):
    """An unknown edition slug simply has no bound sources."""
    result = await reader.read_telegram_messages("no-such-edition", NOW - timedelta(hours=24), NOW)

    assert result == {}
