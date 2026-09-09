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


@pytest.mark.postgres
async def test_collection_success_is_qualified_by_freshness_cutoff(conn, edition):
    source_fresh = await _source(conn, "fresh")
    source_boundary = await _source(conn, "boundary")
    source_old = await _source(conn, "old")
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
        completed_at=CUTOFF + dt.timedelta(minutes=5),
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
        source_ids=[source_fresh, source_boundary, source_old, source_transient],
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
    assert by_id[source_transient].status == "degraded"


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
