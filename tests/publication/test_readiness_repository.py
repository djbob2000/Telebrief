"""Database tests for freshness evidence used by publication readiness."""

from __future__ import annotations

import datetime as dt

import pytest

from src.publication.readiness_repository import PublicationReadinessRepository

UTC = dt.timezone.utc
CUTOFF = dt.datetime(2026, 9, 9, 8, 30, tzinfo=UTC)
TARGET = dt.datetime(2026, 9, 9, 9, 0, tzinfo=UTC)


async def _source(conn, external_id: str) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, name)
        VALUES ('telegram', 'channel', %s, %s)
        RETURNING id
        """,
        (external_id, external_id),
    )
    return int((await cursor.fetchone())[0])


async def _collection_run(
    conn,
    source_id: int,
    *,
    started_at: dt.datetime,
    completed_at: dt.datetime,
    status: str,
) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO collection_runs (source_id, trigger, started_at, completed_at, status)
        VALUES (%s, 'pre_publish', %s, %s, %s)
        RETURNING id
        """,
        (source_id, started_at, completed_at, status),
    )
    return int((await cursor.fetchone())[0])


async def _revision(conn, source_id: int, external_id: str) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', %s, %s)
        RETURNING id
        """,
        (source_id, external_id, CUTOFF),
    )
    item_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (
            source_item_id, revision_no, collected_at, content_hash, text_content
        ) VALUES (%s, 1, %s, %s, 'revision text')
        RETURNING id
        """,
        (item_id, CUTOFF, external_id),
    )
    return int((await cursor.fetchone())[0])


@pytest.mark.postgres
async def test_collection_success_is_qualified_by_freshness_cutoff(conn, edition):
    source_fresh = await _source(conn, "fresh")
    source_boundary = await _source(conn, "boundary")
    source_old = await _source(conn, "old")
    source_started_before_completed_after = await _source(conn, "started-before-completed-after")
    source_transient = await _source(conn, "transient")

    fresh_run = await _collection_run(
        conn,
        source_fresh,
        started_at=CUTOFF + dt.timedelta(minutes=1),
        completed_at=CUTOFF + dt.timedelta(minutes=2),
        status="success",
    )
    await _collection_run(
        conn,
        source_boundary,
        started_at=CUTOFF,
        completed_at=CUTOFF + dt.timedelta(minutes=1),
        status="success",
    )
    await _collection_run(
        conn,
        source_old,
        started_at=CUTOFF - dt.timedelta(seconds=1),
        completed_at=CUTOFF - dt.timedelta(seconds=1),
        status="success",
    )
    completed_after = await _collection_run(
        conn,
        source_started_before_completed_after,
        started_at=CUTOFF - dt.timedelta(seconds=10),
        completed_at=CUTOFF + dt.timedelta(seconds=20),
        status="success",
    )
    await _collection_run(
        conn,
        source_transient,
        started_at=CUTOFF + dt.timedelta(minutes=1),
        completed_at=CUTOFF + dt.timedelta(minutes=2),
        status="transient",
    )

    repo = PublicationReadinessRepository()
    refresh = await repo.get_or_create_refresh_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        slot_at=TARGET,
        requested_at=CUTOFF - dt.timedelta(minutes=1),
        trigger="manual",
        request_key="manual:freshness-test",
        freshness_cutoff_at=CUTOFF,
        deadline_at=TARGET + dt.timedelta(minutes=20),
        requested_by_user_id=123,
        source_ids=[
            source_fresh,
            source_boundary,
            source_old,
            source_started_before_completed_after,
            source_transient,
        ],
    )
    assert refresh.trigger == "manual"
    assert refresh.request_key == "manual:freshness-test"
    assert refresh.freshness_cutoff_at == CUTOFF
    assert refresh.requested_by_user_id == 123

    sources = await repo.reconcile_qualifying_collection_runs(conn, refresh.id)
    by_id = {source.source_id: source for source in sources}
    assert by_id[source_fresh].status == "succeeded"
    assert by_id[source_fresh].collection_run_id == fresh_run
    assert by_id[source_boundary].status == "succeeded"
    assert by_id[source_old].status != "succeeded"
    assert by_id[source_started_before_completed_after].status == "succeeded"
    assert by_id[source_started_before_completed_after].collection_run_id == completed_after
    assert by_id[source_transient].status == "degraded"


@pytest.mark.postgres
async def test_collection_and_revision_barriers_are_evaluated_as_of_boundary(conn, edition):
    source_id = await _source(conn, "as-of-boundary")
    revision_id = await _revision(conn, source_id, "as-of-revision")
    before_deadline = await _collection_run(
        conn,
        source_id,
        started_at=CUTOFF + dt.timedelta(minutes=1),
        completed_at=TARGET - dt.timedelta(seconds=2),
        status="success",
    )
    after_deadline = await _collection_run(
        conn,
        source_id,
        started_at=TARGET + dt.timedelta(seconds=1),
        completed_at=TARGET + dt.timedelta(seconds=2),
        status="success",
    )
    await conn.execute(
        """
        INSERT INTO collection_run_revision_observations (
            collection_run_id, source_item_revision_id
        ) VALUES (%s, %s), (%s, %s)
        """,
        (before_deadline, revision_id, after_deadline, revision_id),
    )
    await conn.execute(
        """
        INSERT INTO event_revision_processing_state (
            source_item_revision_id, status, attempt_count, completed_at
        ) VALUES (%s, 'succeeded', 1, %s)
        """,
        (revision_id, TARGET + dt.timedelta(seconds=5)),
    )

    repo = PublicationReadinessRepository()
    refresh = await repo.get_or_create_refresh_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        slot_at=TARGET,
        requested_at=CUTOFF,
        trigger="manual",
        request_key="manual:as-of-boundary",
        freshness_cutoff_at=CUTOFF,
        deadline_at=TARGET + dt.timedelta(minutes=20),
        requested_by_user_id=123,
        source_ids=[source_id],
    )

    sources = await repo.reconcile_qualifying_collection_runs(
        conn, refresh.id, evaluation_at=TARGET
    )
    assert sources[0].collection_run_id == before_deadline

    state = await repo.get_revision_barrier_state(conn, refresh.id, evaluation_at=TARGET)
    assert state.unprocessed_count == 1
    assert state.completed_at is None

    await conn.execute(
        """
        UPDATE event_revision_processing_state
        SET completed_at = %s
        WHERE source_item_revision_id = %s
        """,
        (TARGET - dt.timedelta(seconds=1), revision_id),
    )
    state = await repo.get_revision_barrier_state(conn, refresh.id, evaluation_at=TARGET)
    assert state.unprocessed_count == 0
    assert state.completed_at == TARGET - dt.timedelta(seconds=1)


@pytest.mark.postgres
async def test_repeated_request_key_does_not_expand_frozen_source_set(conn, edition):
    first_source = await _source(conn, "frozen-first")
    later_source = await _source(conn, "frozen-later")
    repo = PublicationReadinessRepository()

    first = await repo.get_or_create_refresh_run(
        conn,
        edition_id=edition.id,
        publication_type="weekly_article",
        slot_at=TARGET,
        requested_at=CUTOFF,
        trigger="manual",
        request_key="manual:frozen-source-set",
        freshness_cutoff_at=CUTOFF,
        deadline_at=TARGET + dt.timedelta(minutes=20),
        requested_by_user_id=123,
        source_ids=[first_source],
        lookback_hours=168,
    )
    second = await repo.get_or_create_refresh_run(
        conn,
        edition_id=edition.id,
        publication_type="weekly_article",
        slot_at=TARGET,
        requested_at=CUTOFF,
        trigger="manual",
        request_key="manual:frozen-source-set",
        freshness_cutoff_at=CUTOFF,
        deadline_at=TARGET + dt.timedelta(minutes=20),
        requested_by_user_id=123,
        source_ids=[first_source, later_source],
        lookback_hours=168,
    )

    assert second.id == first.id
    assert [source.source_id for source in await repo.list_refresh_sources(conn, first.id)] == [
        first_source
    ]
    assert first.knowledge_snapshot_at is None


@pytest.mark.postgres
async def test_freeze_knowledge_snapshot_sets_once(conn, edition):
    repo = PublicationReadinessRepository()
    refresh_run = await repo.get_or_create_refresh_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        slot_at=TARGET,
        requested_at=CUTOFF,
        trigger="manual",
        request_key="manual:freeze-knowledge-snapshot",
        freshness_cutoff_at=CUTOFF,
        deadline_at=TARGET + dt.timedelta(minutes=20),
        requested_by_user_id=123,
        source_ids=[],
    )
    t1 = refresh_run.deadline_at - dt.timedelta(seconds=5)

    frozen = await repo.freeze_knowledge_snapshot(
        conn, refresh_run_id=refresh_run.id, snapshot_at=t1
    )
    assert frozen.knowledge_snapshot_at == t1

    same = await repo.freeze_knowledge_snapshot(conn, refresh_run_id=refresh_run.id, snapshot_at=t1)
    assert same.knowledge_snapshot_at == t1

    with pytest.raises(ValueError, match="knowledge_snapshot_at"):
        await repo.freeze_knowledge_snapshot(
            conn,
            refresh_run_id=refresh_run.id,
            snapshot_at=t1 + dt.timedelta(seconds=1),
        )


@pytest.mark.postgres
async def test_qualifying_success_survives_a_later_failed_attempt(conn, edition):
    source_id = await _source(conn, "success-then-failure")
    successful_run = await _collection_run(
        conn,
        source_id,
        started_at=CUTOFF + dt.timedelta(minutes=1),
        completed_at=CUTOFF + dt.timedelta(minutes=2),
        status="success",
    )
    await _collection_run(
        conn,
        source_id,
        started_at=CUTOFF + dt.timedelta(minutes=3),
        completed_at=CUTOFF + dt.timedelta(minutes=4),
        status="transient",
    )

    repo = PublicationReadinessRepository()
    refresh = await repo.get_or_create_refresh_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        slot_at=TARGET,
        requested_at=CUTOFF + dt.timedelta(minutes=10),
        trigger="manual",
        request_key="manual:success-then-failure",
        freshness_cutoff_at=CUTOFF,
        deadline_at=TARGET + dt.timedelta(minutes=20),
        requested_by_user_id=123,
        source_ids=[source_id],
    )
    source = (await repo.reconcile_qualifying_collection_runs(conn, refresh.id))[0]
    assert source.status == "succeeded"
    assert source.collection_run_id == successful_run
