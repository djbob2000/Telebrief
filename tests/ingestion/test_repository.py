"""Schema-level expectations for generic ingestion persistence.

Task 1 pins the migration's constraints directly with raw SQL: the stable
item identity, parent/root foreign keys, per-item revision numbering, and
the exact-revision asset link. Task 3 extends this module (same fixtures)
with IngestionRepository behaviour tests layered on these constraints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg
import psycopg.errors
import pytest

PUBLISHED_AT = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)

INSERT_ITEM = """
    INSERT INTO source_items (
        source_id, kind, external_id, author_name, author_external_id,
        canonical_url, published_at, first_collected_at, metadata
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
    RETURNING id
"""

INSERT_ITEM_WITH_LINKS = """
    INSERT INTO source_items (
        source_id, kind, external_id, author_name, author_external_id,
        canonical_url, published_at, first_collected_at, metadata,
        parent_item_id, root_item_id
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
    RETURNING id
"""


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_source_external_id_is_unique(conn, source):
    """UNIQUE(source_id, external_id) is the item's stable identity.

    Mirrors the planned IngestionRepository.get_or_create_item_shell flow at
    the SQL level: a plain INSERT creates the identity, a second INSERT for
    the same (source_id, external_id) is rejected, and an ON CONFLICT upsert
    resolves back to the very same row.
    """
    item_fields = {
        "external_id": "42",
        "kind": "telegram_message",
        "author_name": "Resident",
        "canonical_url": "https://t.me/example/42",
        "metadata": '{"topic_id": null}',
    }
    first = await _insert_item(conn, source, **item_fields)

    with pytest.raises(psycopg.errors.UniqueViolation):
        await _insert_item(conn, source, **item_fields)

    cursor = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'telegram_message', '42', %s)
        ON CONFLICT (source_id, external_id) DO UPDATE SET kind = EXCLUDED.kind
        RETURNING id
        """,
        (source.id, PUBLISHED_AT),
    )
    conflict_id = (await cursor.fetchone())[0]

    assert conflict_id == first


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_external_id_on_other_source_is_allowed(conn, source):
    """Identity is scoped per source: other sources may reuse external ids."""
    other_cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, name)
        VALUES ('telegram', 'channel', 'Other')
        RETURNING id
        """
    )
    other_source = SimpleNamespace(id=(await other_cursor.fetchone())[0])

    first = await _insert_item(conn, source, external_id="42", kind="telegram_message")
    second = await _insert_item(conn, other_source, external_id="42", kind="telegram_message")

    assert first != second


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_parent_item_must_reference_existing_item(conn, source):
    """parent_item_id is a real self-reference on source_items."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await _insert_item(
            conn, source, external_id="43", kind="telegram_message", parent_id=999999
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_root_item_must_reference_existing_item(conn, source):
    """root_item_id is a real self-reference on source_items."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await _insert_item(conn, source, external_id="44", kind="telegram_message", root_id=999999)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_parent_and_root_links_persist(conn, source):
    """Valid parent/root references are stored and readable back."""
    parent = await _insert_item(conn, source, external_id="10", kind="telegram_message")
    root = await _insert_item(conn, source, external_id="1", kind="telegram_message")

    reply = await _insert_item(
        conn,
        source,
        external_id="11",
        kind="telegram_message",
        parent_id=parent,
        root_id=root,
    )

    cursor = await conn.execute(
        "SELECT parent_item_id, root_item_id FROM source_items WHERE id = %s", (reply,)
    )
    stored_parent, stored_root = await cursor.fetchone()

    assert stored_parent == parent
    assert stored_root == root


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_revision_no_must_be_unique_per_item(conn, source):
    """UNIQUE(source_item_id, revision_no) backs the MAX+1 numbering."""
    item = await _insert_item(conn, source, external_id="42", kind="telegram_message")
    await _insert_revision(conn, item, revision_no=1, content_hash="hash-a")

    with pytest.raises(psycopg.errors.UniqueViolation):
        await _insert_revision(conn, item, revision_no=1, content_hash="hash-b")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_revision_no_on_other_item_is_allowed(conn, source):
    """Revision numbering is per item, not global."""
    first_item = await _insert_item(conn, source, external_id="50", kind="telegram_message")
    second_item = await _insert_item(conn, source, external_id="51", kind="telegram_message")

    await _insert_revision(conn, first_item, revision_no=1, content_hash="hash-a")
    await _insert_revision(conn, second_item, revision_no=1, content_hash="hash-c")

    cursor = await conn.execute("SELECT count(*) FROM source_item_revisions")
    assert (await cursor.fetchone())[0] == 2


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_revision_no_is_monotonic_and_reverts_create_new_revisions(conn, source):
    """A -> B -> A must yield revisions 1, 2, 3 (edit then revert).

    Uses the documented per-item MAX+1 pattern inside one transaction — the
    UNIQUE(source_item_id, revision_no) constraint is the correctness backstop.
    """
    item = await _insert_item(conn, source, external_id="60", kind="telegram_message")

    async with conn.transaction():
        await _insert_revision_max_plus_one(conn, item, content_hash="hash-a")
        await _insert_revision_max_plus_one(conn, item, content_hash="hash-b")
        await _insert_revision_max_plus_one(conn, item, content_hash="hash-a")

    cursor = await conn.execute(
        """
        SELECT revision_no, content_hash FROM source_item_revisions
        WHERE source_item_id = %s ORDER BY revision_no
        """,
        (item,),
    )
    rows = await cursor.fetchall()

    assert [row[0] for row in rows] == [1, 2, 3]
    assert [row[1] for row in rows] == ["hash-a", "hash-b", "hash-a"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_asset_requires_existing_revision(conn):
    """Assets link to an exact revision via source_item_revision_id FK."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await conn.execute(
            """
            INSERT INTO source_assets (source_item_revision_id, kind, external_url)
            VALUES (999999, 'photo', 'https://cdn.example/missing.jpg')
            """
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_asset_binds_to_exact_revision(conn, source):
    """An asset attached to revision 1 stays bound to that revision only."""
    item = await _insert_item(conn, source, external_id="70", kind="telegram_message")
    await _insert_revision(conn, item, revision_no=1, content_hash="hash-a")
    await _insert_revision(conn, item, revision_no=2, content_hash="hash-b")

    cursor = await conn.execute(
        """
        SELECT id FROM source_item_revisions
        WHERE source_item_id = %s AND revision_no = 1
        """,
        (item,),
    )
    revision_one = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO source_assets (source_item_revision_id, kind, external_url, metadata)
        VALUES (%s, 'photo', 'https://cdn.example/1.jpg', '{"width": 800}'::jsonb)
        RETURNING id
        """,
        (revision_one,),
    )
    asset_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        SELECT r.revision_no FROM source_assets a
        JOIN source_item_revisions r ON r.id = a.source_item_revision_id
        WHERE a.id = %s
        """,
        (asset_id,),
    )
    assert (await cursor.fetchone())[0] == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_state_event_insert_pins_shape_and_defaults(conn, source):
    """source_item_state_events: spec §4 shape (``type``) plus defaults."""
    item = await _insert_item(conn, source, external_id="80", kind="telegram_message")

    cursor = await conn.execute(
        """
        INSERT INTO source_item_state_events (source_item_id, type)
        VALUES (%s, 'deleted_at_source')
        RETURNING id, observed_at, reason, evidence
        """,
        (item,),
    )
    event_id, observed_at, reason, evidence = await cursor.fetchone()

    cursor = await conn.execute(
        """
        INSERT INTO source_item_state_events (
            source_item_id, type, observed_at, reason, evidence
        )
        VALUES (%s, 'restored', %s, 'reappeared in scan', '{"message_id": 80}'::jsonb)
        RETURNING id
        """,
        (item, PUBLISHED_AT),
    )
    full_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        "SELECT type FROM source_item_state_events WHERE id = %s", (full_id,)
    )

    assert (await cursor.fetchone())[0] == "restored"
    assert event_id > 0
    assert observed_at is not None
    assert reason is None
    assert evidence == {}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_management_mode_defaults_to_bootstrap_and_rejects_unknown(conn, source):
    """sources.management_mode gates Plan 2 Task 4's bootstrap upsert."""
    cursor = await conn.execute("SELECT management_mode FROM sources WHERE id = %s", (source.id,))
    assert (await cursor.fetchone())[0] == "bootstrap"

    with pytest.raises(psycopg.errors.CheckViolation):
        await conn.execute(
            """
            INSERT INTO sources (platform, kind, name, management_mode)
            VALUES ('telegram', 'channel', 'Bad', 'operator')
            """
        )


async def _insert_item(
    conn: psycopg.AsyncConnection,
    source: SimpleNamespace,
    *,
    external_id: str,
    kind: str,
    author_name: str | None = None,
    canonical_url: str | None = None,
    metadata: str = "{}",
    parent_id: int | None = None,
    root_id: int | None = None,
) -> int:
    if parent_id is None and root_id is None:
        statement, params = (
            INSERT_ITEM,
            (
                source.id,
                kind,
                external_id,
                author_name,
                None,
                canonical_url,
                PUBLISHED_AT,
                PUBLISHED_AT,
                metadata,
            ),
        )
    else:
        statement, params = (
            INSERT_ITEM_WITH_LINKS,
            (
                source.id,
                kind,
                external_id,
                author_name,
                None,
                canonical_url,
                PUBLISHED_AT,
                PUBLISHED_AT,
                metadata,
                parent_id,
                root_id,
            ),
        )
    cursor = await conn.execute(statement, params)
    return (await cursor.fetchone())[0]


async def _insert_revision(
    conn: psycopg.AsyncConnection,
    item_id: int,
    *,
    revision_no: int,
    content_hash: str,
) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (
            source_item_id, revision_no, collected_at, content_hash, text_content, payload
        )
        VALUES (%s, %s, %s, %s, %s, '{}'::jsonb)
        RETURNING id
        """,
        (item_id, revision_no, PUBLISHED_AT, content_hash, f"text {content_hash}"),
    )
    return (await cursor.fetchone())[0]


async def _insert_revision_max_plus_one(
    conn: psycopg.AsyncConnection,
    item_id: int,
    *,
    content_hash: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO source_item_revisions (
            source_item_id, revision_no, collected_at, content_hash, text_content, payload
        )
        VALUES (
            %s,
            (SELECT COALESCE(MAX(revision_no), 0) + 1
             FROM source_item_revisions WHERE source_item_id = %s),
            %s,
            %s,
            %s,
            '{}'::jsonb
        )
        """,
        (item_id, item_id, PUBLISHED_AT, content_hash, f"text {content_hash}"),
    )
