"""Schema-level expectations for generic ingestion persistence.

Task 1 pins the migration's constraints directly with raw SQL: the stable
item identity, parent/root foreign keys, per-item revision numbering, and
the exact-revision asset link. Task 3 extends this module (same fixtures)
with IngestionRepository behaviour tests layered on these constraints.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg
import psycopg.errors
import pytest

from src.ingestion.models import ObservedAsset, ObservedItem, ObservedStateEvent
from src.ingestion.repository import IngestionRepository

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


def _observation(
    *,
    external_id: str = "42",
    text: str = "hello",
    parent_external_id: str | None = None,
    root_external_id: str | None = None,
) -> ObservedItem:
    return ObservedItem(
        kind="telegram_message",
        external_id=external_id,
        text=text,
        author_name="Resident",
        published_at=PUBLISHED_AT,
        canonical_url=f"https://t.me/example/{external_id}",
        metadata={"topic": 7},
        observed_at=PUBLISHED_AT,
        parent_external_id=parent_external_id,
        root_external_id=root_external_id,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_get_or_create_item_shell_creates_identity_once(conn, source):
    """First observation creates the shell; re-observation resolves same row."""
    repo = IngestionRepository()

    item, created = await repo.get_or_create_item_shell(conn, source.id, _observation())

    assert created is True
    assert item.id > 0
    assert item.external_id == "42"
    assert item.kind == "telegram_message"
    assert item.author_name == "Resident"
    assert item.first_collected_at == PUBLISHED_AT

    again, created_again = await repo.get_or_create_item_shell(
        conn, source.id, _observation(text="hello edited")
    )

    assert created_again is False
    assert again.id == item.id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_ensure_relationships_links_known_and_skips_unknown_references(conn, source):
    """Resolved parent/root ids are written; unresolved references stay NULL.

    Out-of-order batches self-heal: every later ingest of the item retries
    ensure_relationships once the missing shell finally exists.
    """
    repo = IngestionRepository()
    parent, _ = await repo.get_or_create_item_shell(conn, source.id, _observation(external_id="10"))
    reply, _ = await repo.get_or_create_item_shell(conn, source.id, _observation(external_id="11"))

    await repo.ensure_relationships(
        conn,
        source_id=source.id,
        item_id=reply.id,
        parent_external_id="10",
        root_external_id="10",
    )
    cursor = await conn.execute(
        "SELECT parent_item_id, root_item_id FROM source_items WHERE id = %s", (reply.id,)
    )
    stored_parent, stored_root = await cursor.fetchone()
    assert stored_parent == parent.id
    assert stored_root == parent.id

    orphan, _ = await repo.get_or_create_item_shell(
        conn, source.id, _observation(external_id="12", parent_external_id="ghost")
    )
    await repo.ensure_relationships(
        conn,
        source_id=source.id,
        item_id=orphan.id,
        parent_external_id="ghost",
        root_external_id=None,
    )
    cursor = await conn.execute(
        "SELECT parent_item_id FROM source_items WHERE id = %s", (orphan.id,)
    )
    assert (await cursor.fetchone())[0] is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_insert_revision_if_changed_compares_latest_hash_only(conn, source):
    """Dedup is against the latest revision only; A -> B -> A makes revision 3."""
    repo = IngestionRepository()
    item, _ = await repo.get_or_create_item_shell(conn, source.id, _observation())

    first = await repo.insert_revision_if_changed(
        conn, item.id, _observation(), collected_at=PUBLISHED_AT
    )
    assert first is not None
    assert first.revision_no == 1

    duplicate = await repo.insert_revision_if_changed(
        conn, item.id, _observation(), collected_at=PUBLISHED_AT
    )
    assert duplicate is None

    edited = await repo.insert_revision_if_changed(
        conn, item.id, _observation(text="hello edited"), collected_at=PUBLISHED_AT
    )
    assert edited is not None
    assert edited.revision_no == 2

    reverted = await repo.insert_revision_if_changed(
        conn, item.id, _observation(), collected_at=PUBLISHED_AT
    )
    assert reverted is not None
    assert reverted.revision_no == 3

    cursor = await conn.execute(
        "SELECT count(*) FROM source_item_revisions WHERE source_item_id = %s", (item.id,)
    )
    assert (await cursor.fetchone())[0] == 3


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_get_latest_revision_returns_none_or_highest(conn, source):
    """get_latest_revision orders by revision_no descending."""
    repo = IngestionRepository()
    item, _ = await repo.get_or_create_item_shell(conn, source.id, _observation())
    assert await repo.get_latest_revision(conn, item.id) is None

    await repo.insert_revision_if_changed(conn, item.id, _observation(), collected_at=PUBLISHED_AT)
    latest = await repo.insert_revision_if_changed(
        conn, item.id, _observation(text="hello edited"), collected_at=PUBLISHED_AT
    )

    current = await repo.get_latest_revision(conn, item.id)
    assert current is not None
    assert current.id == latest.id
    assert current.revision_no == 2
    assert current.text_content == "hello edited"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_upsert_asset_for_revision_is_idempotent_per_identity(conn, source):
    """Asset identity is (revision, kind, url, hash): re-ingest refreshes in place."""
    repo = IngestionRepository()
    item, _ = await repo.get_or_create_item_shell(conn, source.id, _observation())
    revision = await repo.insert_revision_if_changed(
        conn, item.id, _observation(), collected_at=PUBLISHED_AT
    )
    assert revision is not None
    asset = ObservedAsset(
        item_external_id="42",
        kind="photo",
        external_url="https://cdn.example/1.jpg",
        mime_type="image/jpeg",
        content_hash="hash-a",
        metadata={"width": 800},
    )

    await repo.upsert_asset_for_revision(conn, revision.id, asset)
    await repo.upsert_asset_for_revision(conn, revision.id, asset)

    cursor = await conn.execute(
        "SELECT count(*) FROM source_assets WHERE source_item_revision_id = %s", (revision.id,)
    )
    assert (await cursor.fetchone())[0] == 1

    refreshed = replace(asset, mime_type="image/webp", metadata={"width": 1024})
    await repo.upsert_asset_for_revision(conn, revision.id, refreshed)

    cursor = await conn.execute(
        """
        SELECT mime_type, metadata FROM source_assets
        WHERE source_item_revision_id = %s
        """,
        (revision.id,),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "image/webp"
    assert rows[0][1] == {"width": 1024}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_replaced_media_with_same_url_adds_new_asset_row(conn, source):
    """Same URL but new content hash is a distinct asset version, not a clobber."""
    repo = IngestionRepository()
    item, _ = await repo.get_or_create_item_shell(conn, source.id, _observation())
    revision = await repo.insert_revision_if_changed(
        conn, item.id, _observation(), collected_at=PUBLISHED_AT
    )
    assert revision is not None
    asset = ObservedAsset(
        item_external_id="42",
        kind="photo",
        external_url="https://cdn.example/1.jpg",
        mime_type="image/jpeg",
        content_hash="hash-a",
        metadata={},
    )
    replaced = replace(asset, content_hash="hash-b", mime_type="image/webp")

    await repo.upsert_asset_for_revision(conn, revision.id, asset)
    await repo.upsert_asset_for_revision(conn, revision.id, replaced)

    cursor = await conn.execute(
        """
        SELECT content_hash, mime_type FROM source_assets
        WHERE source_item_revision_id = %s ORDER BY content_hash
        """,
        (revision.id,),
    )
    rows = await cursor.fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("hash-a", "image/jpeg"),
        ("hash-b", "image/webp"),
    ]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_album_assets_without_urls_stay_distinct(conn, source):
    """Telegram album photos have no per-photo URLs; hashes keep them apart."""
    repo = IngestionRepository()
    item, _ = await repo.get_or_create_item_shell(conn, source.id, _observation())
    revision = await repo.insert_revision_if_changed(
        conn, item.id, _observation(), collected_at=PUBLISHED_AT
    )
    assert revision is not None
    photo_a = ObservedAsset(
        item_external_id="42",
        kind="photo",
        external_url=None,
        mime_type="image/jpeg",
        content_hash="hash-a",
        metadata={"seq": 1},
    )
    photo_b = replace(photo_a, content_hash="hash-b", metadata={"seq": 2})

    await repo.upsert_asset_for_revision(conn, revision.id, photo_a)
    await repo.upsert_asset_for_revision(conn, revision.id, photo_b)

    cursor = await conn.execute(
        "SELECT count(*) FROM source_assets WHERE source_item_revision_id = %s", (revision.id,)
    )
    assert (await cursor.fetchone())[0] == 2

    await repo.upsert_asset_for_revision(conn, revision.id, photo_a)
    await repo.upsert_asset_for_revision(
        conn, revision.id, replace(photo_b, mime_type="image/webp")
    )

    cursor = await conn.execute(
        """
        SELECT content_hash, mime_type, metadata FROM source_assets
        WHERE source_item_revision_id = %s ORDER BY content_hash
        """,
        (revision.id,),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "hash-a"
    assert rows[0][2] == {"seq": 1}
    assert rows[1][0] == "hash-b"
    assert rows[1][1] == "image/webp"
    assert rows[1][2] == {"seq": 2}


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_insert_state_event_appends_and_skips_unknown_items(conn, source):
    """Events append for known items; unknown anchors are skipped."""
    repo = IngestionRepository()
    item, _ = await repo.get_or_create_item_shell(conn, source.id, _observation())
    event = ObservedStateEvent(
        item_external_id="42",
        type="deleted_at_source",
        observed_at=PUBLISHED_AT,
        reason="missing from full rescan",
        evidence={"message_id": 42},
    )

    event_id = await repo.insert_state_event(conn, source.id, event)
    assert event_id is not None and event_id > 0

    cursor = await conn.execute(
        """
        SELECT source_item_id, type, observed_at, reason, evidence
        FROM source_item_state_events WHERE id = %s
        """,
        (event_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == item.id
    assert row[1] == "deleted_at_source"
    assert row[2] == PUBLISHED_AT
    assert row[3] == "missing from full rescan"
    assert row[4] == {"message_id": 42}

    skipped = await repo.insert_state_event(
        conn, source.id, replace(event, item_external_id="999999")
    )
    assert skipped is None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_run_lifecycle_records_outcome_error_kind_and_completion(conn, source):
    """start_run opens a running row; finish_run closes it with the outcome."""
    repo = IngestionRepository()
    run = await repo.start_run(
        conn, source_id=source.id, trigger="scheduled", started_at=PUBLISHED_AT
    )
    assert run.id > 0
    assert run.status == "running"
    assert run.trigger == "scheduled"

    await repo.finish_run(
        conn,
        run_id=run.id,
        outcome="rate_limited",
        completed_at=PUBLISHED_AT,
        error_kind="flood_wait",
        seen_count=5,
        new_count=2,
        updated_count=1,
    )

    cursor = await conn.execute(
        """
        SELECT status, completed_at, error_kind, seen_count, new_count, updated_count
        FROM collection_runs WHERE id = %s
        """,
        (run.id,),
    )
    status, completed_at, error_kind, seen_count, new_count, updated_count = await cursor.fetchone()
    assert status == "rate_limited"
    assert completed_at == PUBLISHED_AT
    assert error_kind == "flood_wait"
    assert (seen_count, new_count, updated_count) == (5, 2, 1)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_update_checkpoint_upserts_and_preserves_last_success(conn, source):
    """adapter_state/last_scan_at always refresh; last_success_at survives failures."""
    repo = IngestionRepository()
    assert await repo.get_checkpoint(conn, source.id) is None

    await repo.update_checkpoint(
        conn,
        source_id=source.id,
        adapter_state={"cursor": 12},
        last_scan_at=PUBLISHED_AT,
        last_success_at=PUBLISHED_AT,
        success=True,
    )
    checkpoint = await repo.get_checkpoint(conn, source.id)
    assert checkpoint is not None
    assert checkpoint.adapter_state == {"cursor": 12}
    assert checkpoint.last_success_at == PUBLISHED_AT
    assert checkpoint.last_scan_at == PUBLISHED_AT
    assert checkpoint.consecutive_failures == 0
    # The explicit provider cursor rides in adapter_state["cursor"] when given.
    assert checkpoint.cursor == 12

    failed_scan_at = datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc)
    await repo.update_checkpoint(
        conn,
        source_id=source.id,
        adapter_state={"cursor": 13},
        last_scan_at=failed_scan_at,
        last_success_at=None,
        success=False,
    )
    checkpoint = await repo.get_checkpoint(conn, source.id)
    assert checkpoint is not None
    assert checkpoint.adapter_state == {"cursor": 13}
    assert checkpoint.last_success_at == PUBLISHED_AT
    assert checkpoint.last_scan_at == failed_scan_at


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_update_checkpoint_counts_consecutive_failures_and_resets(conn, source):
    """Failed scans accumulate consecutive_failures; the next success resets."""
    repo = IngestionRepository()
    first_failure = datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc)
    second_failure = datetime(2026, 8, 22, 11, 5, tzinfo=timezone.utc)
    recovery = datetime(2026, 8, 22, 11, 9, tzinfo=timezone.utc)

    await repo.update_checkpoint(
        conn,
        source_id=source.id,
        adapter_state={},
        last_scan_at=first_failure,
        last_success_at=None,
        success=False,
    )
    await repo.update_checkpoint(
        conn,
        source_id=source.id,
        adapter_state={},
        last_scan_at=second_failure,
        last_success_at=None,
        success=False,
    )
    checkpoint = await repo.get_checkpoint(conn, source.id)
    assert checkpoint is not None
    assert checkpoint.consecutive_failures == 2

    await repo.update_checkpoint(
        conn,
        source_id=source.id,
        adapter_state={},
        last_scan_at=recovery,
        last_success_at=recovery,
        success=True,
    )
    checkpoint = await repo.get_checkpoint(conn, source.id)
    assert checkpoint is not None
    assert checkpoint.consecutive_failures == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_update_checkpoint_without_cursor_keeps_previous_cursor(conn, source):
    """Adapters that publish no cursor never erase an earlier one."""
    repo = IngestionRepository()
    await repo.update_checkpoint(
        conn,
        source_id=source.id,
        adapter_state={"cursor": {"topic": 7}},
        last_scan_at=PUBLISHED_AT,
        last_success_at=PUBLISHED_AT,
        success=True,
    )
    await repo.update_checkpoint(
        conn,
        source_id=source.id,
        adapter_state={},
        last_scan_at=PUBLISHED_AT,
        last_success_at=PUBLISHED_AT,
        success=True,
    )
    checkpoint = await repo.get_checkpoint(conn, source.id)
    assert checkpoint is not None
    assert checkpoint.cursor == {"topic": 7}
