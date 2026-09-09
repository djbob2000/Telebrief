"""Tests for durable Event-First source-revision processing state."""

from __future__ import annotations

import pytest

from src.repositories.event_revision_processing import EventRevisionProcessingRepository


@pytest.mark.postgres
async def test_revision_processing_state_is_idempotent_and_ordered(repo_conn):
    cursor = await repo_conn.execute(
        """
        INSERT INTO editions (slug, name) VALUES ('processing-state', 'Processing State')
        RETURNING id
        """
    )
    edition_id = int((await cursor.fetchone())[0])
    cursor = await repo_conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, name)
        VALUES ('telegram', 'channel', 'processing-state-source', 'Processing State Source')
        RETURNING id
        """
    )
    source_id = int((await cursor.fetchone())[0])
    revision_ids: list[int] = []
    for index in range(3):
        cursor = await repo_conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
            VALUES (%s, 'message', %s, now()) RETURNING id
            """,
            (source_id, f"processing-state-item-{index}"),
        )
        item_id = int((await cursor.fetchone())[0])
        cursor = await repo_conn.execute(
            """
            INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
            VALUES (%s, 1, %s, 'processing text') RETURNING id
            """,
            (item_id, f"processing-state-revision-{index}"),
        )
        revision_ids.append(int((await cursor.fetchone())[0]))

    del edition_id  # The source/revision seed only needs a valid foundation row.
    repository = EventRevisionProcessingRepository()
    await repository.mark_pending(repo_conn, revision_ids)
    await repository.mark_pending(repo_conn, revision_ids)
    assert await repository.list_incomplete(repo_conn, [revision_ids[2], revision_ids[0]]) == [
        revision_ids[2],
        revision_ids[0],
    ]

    await repository.mark_running(repo_conn, [revision_ids[0]])
    await repository.mark_succeeded(repo_conn, [revision_ids[0]])
    assert await repository.list_incomplete(repo_conn, revision_ids) == revision_ids[1:]


@pytest.mark.postgres
async def test_mark_running_supports_replayed_revision_and_failed_state(repo_conn):
    cursor = await repo_conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('processing-replay', 'Processing Replay') RETURNING id"
    )
    edition_id = int((await cursor.fetchone())[0])
    cursor = await repo_conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, name)
        VALUES ('telegram', 'channel', 'processing-replay-source', 'Processing Replay Source')
        RETURNING id
        """
    )
    source_id = int((await cursor.fetchone())[0])
    cursor = await repo_conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', 'processing-replay-item', now()) RETURNING id
        """,
        (source_id,),
    )
    item_id = int((await cursor.fetchone())[0])
    cursor = await repo_conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'processing-replay-revision', 'processing text') RETURNING id
        """,
        (item_id,),
    )
    revision_id = int((await cursor.fetchone())[0])

    repository = EventRevisionProcessingRepository()
    await repository.mark_running(repo_conn, [revision_id])
    await repository.mark_failed(repo_conn, [revision_id], error_kind="server")
    cursor = await repo_conn.execute(
        """
        SELECT status, attempt_count, last_error_kind
        FROM event_revision_processing_state
        WHERE source_item_revision_id = %s
        """,
        (revision_id,),
    )
    assert await cursor.fetchone() == ("failed", 1, "server")
    del edition_id


@pytest.mark.postgres
async def test_mark_reused_is_idempotent_and_normal_processing_clears_provenance(repo_conn):
    cursor = await repo_conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('processing-reuse', 'Processing Reuse') RETURNING id"
    )
    edition_id = int((await cursor.fetchone())[0])
    cursor = await repo_conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, name)
        VALUES ('telegram', 'channel', 'processing-reuse-source', 'Processing Reuse Source')
        RETURNING id
        """
    )
    source_id = int((await cursor.fetchone())[0])
    revision_ids: list[int] = []
    for index in range(2):
        cursor = await repo_conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
            VALUES (%s, 'message', %s, now()) RETURNING id
            """,
            (source_id, f"processing-reuse-item-{index}"),
        )
        item_id = int((await cursor.fetchone())[0])
        cursor = await repo_conn.execute(
            """
            INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
            VALUES (%s, 1, %s, 'processing text') RETURNING id
            """,
            (item_id, f"processing-reuse-revision-{index}"),
        )
        revision_ids.append(int((await cursor.fetchone())[0]))

    repository = EventRevisionProcessingRepository()
    await repository.mark_reused(
        repo_conn,
        revision_id=revision_ids[1],
        reused_from_revision_id=revision_ids[0],
    )
    await repository.mark_reused(
        repo_conn,
        revision_id=revision_ids[1],
        reused_from_revision_id=revision_ids[0],
    )
    state = await repository.get_state(repo_conn, revision_ids[1])
    assert state is not None
    assert state.status == "succeeded"
    assert state.processing_mode == "reused"
    assert state.reused_from_revision_id == revision_ids[0]

    await repository.mark_running(repo_conn, [revision_ids[1]])
    await repository.mark_succeeded(repo_conn, [revision_ids[1]])
    state = await repository.get_state(repo_conn, revision_ids[1])
    assert state is not None
    assert state.processing_mode == "full"
    assert state.reused_from_revision_id is None
    del edition_id
