"""End-to-end database regressions for unified publication readiness."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from src.config_loader import Config
from src.publication.orchestrator import PublicationOrchestrator

UTC = dt.timezone.utc
TARGET = dt.datetime(2026, 9, 9, 9, 0, tzinfo=UTC)


@pytest.fixture
def no_background_job_enqueue(monkeypatch):
    async def defer_preparation(self, conn, intent_id):
        return None

    monkeypatch.setattr(PublicationOrchestrator, "_defer_preparation", defer_preparation)
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    fake_task = SimpleNamespace(configure=lambda **kwargs: SimpleNamespace(defer_async=AsyncMock()))
    import src.jobs.admin

    monkeypatch.setattr(src.jobs.admin, "send_publication_failure_notification", fake_task)


async def _source(conn, edition_id: int, external_id: str) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, name, enabled)
        VALUES ('telegram', 'channel', %s, %s, TRUE)
        RETURNING id
        """,
        (external_id, external_id),
    )
    source_id = int((await cursor.fetchone())[0])
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (source_id, edition_id),
    )
    return source_id


async def _collection_run(
    conn, source_id: int, *, started_at: dt.datetime, status: str = "success"
) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO collection_runs (source_id, trigger, started_at, completed_at, status)
        VALUES (%s, 'pre_publish', %s, %s, %s)
        RETURNING id
        """,
        (source_id, started_at, started_at + dt.timedelta(minutes=1), status),
    )
    return int((await cursor.fetchone())[0])


def _config(sample_config: Config) -> Config:
    settings = replace(
        sample_config.settings,
        persistent_ingestion=True,
        publication_freshness_ttl_minutes=30,
        publication_readiness_deadline_minutes=20,
        admin_user_ids=[123, 456],
    )
    return replace(
        sample_config, settings=settings, database=replace(sample_config.database, enabled=True)
    )


@pytest.mark.postgres
async def test_manual_fresh_sources_prepare_only_after_readiness(
    conn, edition, sample_config, uow, no_background_job_enqueue
):
    source_id = await _source(conn, edition.id, "manual-fresh")
    await _collection_run(conn, source_id, started_at=TARGET - dt.timedelta(minutes=5))

    cfg = _config(sample_config)
    result = await PublicationOrchestrator(uow=uow, config=cfg).request(
        edition_slug="berdyansk",
        publication_type="digest_grouped",
        trigger="manual",
        target_at=TARGET,
        requested_by_user_id=123,
        request_key="manual:integration:fresh",
        now=TARGET,
    )

    assert result.readiness_status == "ready_for_preparation"
    cur = await conn.execute(
        "SELECT status, normal_source_cutoff_at, freshness_cutoff_at FROM publication_refresh_runs WHERE id = %s",
        (result.intent_id,),
    )
    status, source_cutoff, freshness_cutoff = await cur.fetchone()
    assert status == "preparing"
    assert source_cutoff == TARGET
    assert freshness_cutoff == TARGET - dt.timedelta(minutes=30)
    cur = await conn.execute("SELECT count(*) FROM publication_runs")
    assert (await cur.fetchone())[0] == 0


@pytest.mark.postgres
async def test_stale_source_is_the_only_scan_enqueued(
    conn, edition, sample_config, uow, no_background_job_enqueue
):
    fresh = await _source(conn, edition.id, "fresh-not-rescanned")
    stale = await _source(conn, edition.id, "stale-retry")
    await _collection_run(conn, fresh, started_at=TARGET - dt.timedelta(minutes=5))
    await _collection_run(conn, stale, started_at=TARGET - dt.timedelta(minutes=31))
    enqueued = []

    async def enqueue(source_id, trigger, priority):
        enqueued.append(source_id)
        return source_id

    cfg = _config(sample_config)
    result = await PublicationOrchestrator(uow=uow, config=cfg, enqueue_source=enqueue).request(
        edition_slug="berdyansk",
        publication_type="digest_grouped",
        trigger="manual",
        target_at=TARGET,
        requested_by_user_id=123,
        request_key="manual:integration:stale",
        now=TARGET,
    )
    assert result.readiness_status == "collecting"
    assert enqueued == [stale]


@pytest.mark.postgres
async def test_terminal_source_failure_fails_without_publication_and_notifies(
    conn, edition, sample_config, uow, no_background_job_enqueue
):
    source_id = await _source(conn, edition.id, "auth-failure")
    await _collection_run(
        conn, source_id, started_at=TARGET - dt.timedelta(minutes=5), status="auth_required"
    )

    result = await PublicationOrchestrator(uow=uow, config=_config(sample_config)).request(
        edition_slug="berdyansk",
        publication_type="daily_article",
        trigger="manual",
        target_at=TARGET,
        requested_by_user_id=123,
        request_key="manual:integration:terminal",
        now=TARGET,
    )
    assert result.readiness_status == "failed"
    cur = await conn.execute(
        "SELECT error_kind, status FROM publication_refresh_runs WHERE id = %s",
        (result.intent_id,),
    )
    assert await cur.fetchone() == ("source_auth_required", "failed")
    cur = await conn.execute("SELECT count(*) FROM publication_runs")
    assert (await cur.fetchone())[0] == 0
    cur = await conn.execute(
        "SELECT count(*) FROM publication_failure_notifications WHERE refresh_run_id = %s",
        (result.intent_id,),
    )
    assert (await cur.fetchone())[0] == 1


@pytest.mark.postgres
async def test_scheduled_ready_early_waits_then_prepares_at_slot(
    conn, edition, sample_config, uow, no_background_job_enqueue
):
    source_id = await _source(conn, edition.id, "scheduled-slot")
    await _collection_run(conn, source_id, started_at=TARGET - dt.timedelta(minutes=5))
    cfg = _config(sample_config)
    orchestrator = PublicationOrchestrator(uow=uow, config=cfg)
    key = "scheduled:berdyansk:digest_grouped:2026-09-09T09:00:00+00:00"

    early = await orchestrator.request(
        edition_slug="berdyansk",
        publication_type="digest_grouped",
        trigger="scheduled",
        target_at=TARGET,
        request_key=key,
        now=TARGET - dt.timedelta(minutes=1),
    )
    assert early.readiness_status == "ready_waiting_slot"
    cur = await conn.execute("SELECT count(*) FROM publication_runs")
    assert (await cur.fetchone())[0] == 0

    late = await orchestrator.reconcile(early.intent_id, now=TARGET)
    assert late.status == "ready_for_preparation"
    cur = await conn.execute(
        "SELECT status FROM publication_refresh_runs WHERE id = %s", (early.intent_id,)
    )
    assert (await cur.fetchone())[0] == "preparing"
