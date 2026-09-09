"""Tests for publication jobs in src/jobs/publication.py."""

from __future__ import annotations

import datetime as dt
import os
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest

from src.db.uow import DatabaseUnitOfWork
from src.jobs.publication import (
    select_stories_for_publication,
)
from src.publication.snapshot import PublicationSnapshotService

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


@pytest.mark.skipif(
    "TELEBRIEF_TEST_DATABASE_URL" not in os.environ,
    reason="TELEBRIEF_TEST_DATABASE_URL is not set",
)
@pytest.mark.postgres
async def test_select_stories_job_runs_with_fail_open_selection(
    conn: psycopg.AsyncConnection, pool, edition
):
    uow = DatabaseUnitOfWork(pool)
    snap_service = PublicationSnapshotService(uow=uow)

    cur = await conn.execute(
        "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
        (edition.id, _NOW),
    )
    story_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
        VALUES (%s, 1, 'open', 'Свежая новость', 'h-job-1', %s)
        RETURNING id
        """,
        (story_id, _NOW),
    )
    rev_id = (await cur.fetchone())[0]
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
    )

    run = await snap_service.create_run(
        edition_id=edition.id,
        publication_type="digest_grouped",
        snapshot_at=_NOW,
        request_key="test-job-select-run",
    )
    await snap_service.seal_candidates(run.id)

    # Execute job
    from types import SimpleNamespace

    from src import runtime

    runtime._runtime = SimpleNamespace(uow=uow)

    # Job executes select_stories_for_publication
    context = {}
    from src.ai_providers import ProviderUnavailableError

    with (
        patch("src.jobs.publication.generate_publication.configure") as mock_gen_conf,
        patch(
            "src.publication.selection_ai.AIPublicationSelectionModel.select_stories",
            side_effect=ProviderUnavailableError("simulated provider offline"),
        ),
    ):
        mock_task = AsyncMock()
        mock_gen_conf.return_value = mock_task
        await select_stories_for_publication(context, run.id)

    # Check that run transitioned to selected_inputs_sealed
    async with uow.transaction() as check_conn:
        cur = await check_conn.execute(
            "SELECT status FROM publication_runs WHERE id = %s", (run.id,)
        )
        status = (await cur.fetchone())[0]
        assert status == "selected_inputs_sealed"


@pytest.mark.asyncio
async def test_generate_publication_job_does_not_retry_terminal_article_rejection(monkeypatch):
    from types import SimpleNamespace

    from src import runtime
    from src.jobs.publication import generate_publication
    from src.publication.errors import ArticlePublicationRejected

    runtime._runtime = SimpleNamespace(uow=AsyncMock())
    mocked_generate = AsyncMock(
        side_effect=ArticlePublicationRejected(
            reason="validation_failed",
            message="invalid article",
        )
    )
    monkeypatch.setattr(
        "src.publication.generation.PublicationGenerationService.generate",
        mocked_generate,
    )

    await generate_publication({}, run_id=42)

    mocked_generate.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_generate_publication_job_still_raises_infrastructure_failure(monkeypatch):
    from types import SimpleNamespace

    from src import runtime
    from src.jobs.publication import generate_publication

    runtime._runtime = SimpleNamespace(uow=AsyncMock())
    mocked_generate = AsyncMock(side_effect=ConnectionError("db unavailable"))
    monkeypatch.setattr(
        "src.publication.generation.PublicationGenerationService.generate",
        mocked_generate,
    )

    with pytest.raises(ConnectionError):
        await generate_publication({}, run_id=42)


@pytest.mark.asyncio
async def test_drain_authority_gap_loops_and_drains():
    mock_uow = MagicMock()
    mock_conn = AsyncMock()
    mock_uow.transaction.return_value.__aenter__.return_value = mock_conn
    mock_repo = AsyncMock()

    # Story 101 in gap initially, drained after round 1
    mock_repo.find_authority_gap_story_ids = AsyncMock(side_effect=[[101], []])

    service = PublicationSnapshotService(uow=mock_uow, repo=mock_repo)

    coalesce_mock = AsyncMock()
    with patch("src.jobs.event_processing.coalesce_dirty_stories_task.func", coalesce_mock):
        remaining = await service.drain_authority_gap(
            edition_id=1,
            snapshot_at=dt.datetime.now(dt.timezone.utc),
            eligibility_policy_id=5,
        )

    assert remaining == 0
    assert coalesce_mock.await_count == 1
    coalesce_mock.assert_awaited_with(edition_id=1, force_settled=True, story_ids=[101])


@pytest.mark.asyncio
async def test_prepare_publication_from_intent_drains_authority_gap(monkeypatch):
    from types import SimpleNamespace

    from src import runtime
    from src.jobs.publication import prepare_publication_from_intent

    mock_uow = MagicMock()
    mock_conn = AsyncMock()
    mock_uow.transaction.return_value.__aenter__.return_value = mock_conn
    runtime._runtime = SimpleNamespace(uow=mock_uow)

    mock_edition = SimpleNamespace(id=1, slug="berdyansk")
    mock_refresh = SimpleNamespace(
        id=10,
        edition_id=1,
        publication_type="digest_grouped",
        slot_at=dt.datetime(2026, 9, 7, 6, 0, tzinfo=dt.timezone.utc),
        request_key="manual:test",
        trigger="manual",
        status="preparing",
    )
    mock_run = SimpleNamespace(
        id=10,
        edition_id=1,
        eligibility_policy_id=5,
        snapshot_at=dt.datetime(2026, 9, 7, 6, 0, tzinfo=dt.timezone.utc),
    )

    drain_called = []
    seal_called = []

    async def fake_drain(*args, **kwargs):
        drain_called.append(kwargs)
        return 0

    async def fake_seal(run_id, conn=None):
        seal_called.append(run_id)

    mock_service = AsyncMock()
    mock_service.create_run.return_value = mock_run
    mock_service.drain_authority_gap.side_effect = fake_drain
    mock_service.count_authority_gap.return_value = 0
    mock_service.seal_candidates.side_effect = fake_seal

    monkeypatch.setattr(
        "src.repositories.editions.EditionRepository.get_by_id",
        AsyncMock(return_value=mock_edition),
    )
    monkeypatch.setattr(
        "src.publication.readiness_repository.PublicationReadinessRepository.get_refresh_run",
        AsyncMock(return_value=mock_refresh),
    )
    monkeypatch.setattr(
        "src.publication.readiness_repository.PublicationReadinessRepository.mark_publication_queued",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "src.publication.policies.PublicationPolicyService.ensure_current",
        AsyncMock(return_value=SimpleNamespace(eligibility_policy_id=5)),
    )
    monkeypatch.setattr(
        "src.publication.snapshot.PublicationSnapshotService",
        lambda uow: mock_service,
    )
    monkeypatch.setattr(
        "src.jobs.publication.select_stories_for_publication.configure",
        lambda connection: SimpleNamespace(defer_async=AsyncMock()),
    )

    await prepare_publication_from_intent({}, intent_id=10)

    assert len(drain_called) == 1
    assert drain_called[0]["edition_id"] == 1
    assert drain_called[0]["source_cutoff_at"] == mock_refresh.slot_at
    assert seal_called == [10]


@pytest.mark.asyncio
async def test_prepare_publication_from_intent_rejects_unclaimed_intent(monkeypatch):
    from types import SimpleNamespace

    from src import runtime
    from src.jobs.publication import prepare_publication_from_intent

    mock_uow = MagicMock()
    mock_conn = AsyncMock()
    mock_uow.transaction.return_value.__aenter__.return_value = mock_conn
    runtime._runtime = SimpleNamespace(uow=mock_uow)

    mock_run = SimpleNamespace(
        id=10,
        edition_id=1,
        status="ready_waiting_slot",
        eligibility_policy_id=5,
        snapshot_at=dt.datetime(2026, 9, 7, 6, 0, tzinfo=dt.timezone.utc),
    )

    monkeypatch.setattr(
        "src.publication.readiness_repository.PublicationReadinessRepository.get_refresh_run",
        AsyncMock(return_value=mock_run),
    )
    with pytest.raises(ValueError, match="not preparing"):
        await prepare_publication_from_intent({}, intent_id=10)
