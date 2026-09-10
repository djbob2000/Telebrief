"""Pure tests for the durable publication refresh state machine."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from src.publication.readiness import PublicationReadinessService
from src.publication.readiness_repository import (
    PublicationRefreshRun,
    PublicationRefreshSource,
)

NOW = dt.datetime(2026, 9, 9, 6, 0, tzinfo=dt.timezone.utc)


def _refresh(*, status: str = "collecting", deadline_at: dt.datetime | None = None):
    return PublicationRefreshRun(
        id=10,
        edition_id=1,
        publication_type="digest_grouped",
        slot_at=NOW,
        requested_at=NOW - dt.timedelta(minutes=45),
        normal_source_cutoff_at=NOW,
        fallback_snapshot_at=NOW - dt.timedelta(minutes=45),
        deadline_at=deadline_at or NOW + dt.timedelta(minutes=20),
        status=status,
        collection_ready_at=None,
        processing_ready_at=None,
        prepared_at=None,
        publication_run_id=None,
        fallback_used=False,
        error_kind=None,
        metadata={},
        trigger="scheduled",
        request_key="scheduled:test",
        freshness_cutoff_at=NOW - dt.timedelta(minutes=30),
        requested_by_user_id=None,
    )


def _source(source_id: int = 1, status: str = "succeeded"):
    return PublicationRefreshSource(
        refresh_run_id=10,
        source_id=source_id,
        required_since_at=NOW - dt.timedelta(minutes=45),
        status=status,
        collection_run_id=source_id,
        collection_outcome="success" if status == "succeeded" else None,
        last_enqueue_attempt_at=None,
        completed_at=NOW if status == "succeeded" else None,
    )


class FakeReadinessRepository:
    def __init__(self, refresh, sources, unprocessed=0):
        self.refresh = refresh
        self.sources = sources
        self.unprocessed = unprocessed

    async def get_refresh_run(self, conn, refresh_run_id, *, for_update=False):
        return self.refresh

    async def reconcile_qualifying_collection_runs(self, conn, refresh_run_id):
        return self.sources

    async def count_unprocessed_refresh_revisions(self, conn, refresh_run_id):
        return self.unprocessed

    async def transition_refresh(self, conn, refresh_run_id, **kwargs):
        self.refresh = replace(
            self.refresh,
            status=kwargs["status"],
            error_kind=kwargs.get("error_kind") or self.refresh.error_kind,
            collection_ready_at=kwargs.get("collection_ready_at")
            or self.refresh.collection_ready_at,
            processing_ready_at=kwargs.get("processing_ready_at")
            or self.refresh.processing_ready_at,
        )


@pytest.mark.asyncio
async def test_all_sources_success_and_no_new_revisions_is_ready():
    repo = FakeReadinessRepository(_refresh(), [_source()])
    decision = await PublicationReadinessService(repo).reconcile(None, 10, now=NOW)
    assert decision.status == "ready_for_preparation"
    assert decision.source_cutoff_at == NOW


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authority_gap_keeps_fully_revision_processed_intent_in_processing():
    repo = FakeReadinessRepository(_refresh(), [_source()])

    async def authority_gap_checker(conn, refresh, now):
        assert refresh.id == 10
        assert now == NOW
        return [9001]

    decision = await PublicationReadinessService(
        repo,
        authority_gap_checker=authority_gap_checker,
    ).reconcile(None, 10, now=NOW)

    assert decision.status == "processing"
    assert decision.source_cutoff_at is None
    assert decision.authority_gap_story_ids == (9001,)
    assert repo.refresh.status == "processing"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authority_gap_can_converge_before_deadline():
    repo = FakeReadinessRepository(_refresh(), [_source()])
    calls = 0

    async def authority_gap_checker(conn, refresh, now):
        nonlocal calls
        calls += 1
        return [9001] if calls == 1 else []

    service = PublicationReadinessService(repo, authority_gap_checker=authority_gap_checker)
    first = await service.reconcile(None, 10, now=NOW)
    second = await service.reconcile(None, 10, now=NOW + dt.timedelta(minutes=7))

    assert first.status == "processing"
    assert second.status == "ready_for_preparation"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authority_gap_fails_closed_at_deadline():
    repo = FakeReadinessRepository(_refresh(deadline_at=NOW - dt.timedelta(seconds=1)), [_source()])

    async def authority_gap_checker(conn, refresh, now):
        raise AssertionError("deadline must be checked before authority-gap work")

    decision = await PublicationReadinessService(
        repo,
        authority_gap_checker=authority_gap_checker,
    ).reconcile(None, 10, now=NOW)

    assert decision.status == "failed"
    assert decision.failure_kind == "readiness_deadline"


@pytest.mark.asyncio
async def test_processed_barrier_keeps_refresh_in_processing():
    repo = FakeReadinessRepository(_refresh(), [_source()], unprocessed=1)
    decision = await PublicationReadinessService(repo).reconcile(None, 10, now=NOW)
    assert decision.status == "processing"


@pytest.mark.asyncio
async def test_degraded_source_waits_before_deadline():
    repo = FakeReadinessRepository(_refresh(), [_source(status="degraded")])
    decision = await PublicationReadinessService(repo).reconcile(None, 10, now=NOW)
    assert decision.status == "collecting"


@pytest.mark.asyncio
async def test_deadline_is_fail_closed():
    repo = FakeReadinessRepository(
        _refresh(deadline_at=NOW - dt.timedelta(seconds=1)), [_source(status="degraded")]
    )
    decision = await PublicationReadinessService(repo).reconcile(None, 10, now=NOW)
    assert decision.status == "failed"
    assert decision.failure_kind == "readiness_deadline"


@pytest.mark.asyncio
async def test_deadline_fail_closed_is_terminal():
    repo = FakeReadinessRepository(
        _refresh(deadline_at=NOW - dt.timedelta(seconds=1)), [_source(status="degraded")]
    )
    decision = await PublicationReadinessService(repo).reconcile(None, 10, now=NOW)
    assert decision.status == "failed"
    assert repo.refresh.error_kind == "readiness_deadline"


@pytest.mark.asyncio
async def test_no_bound_sources_is_immediately_ready():
    repo = FakeReadinessRepository(_refresh(), [])
    decision = await PublicationReadinessService(repo).reconcile(None, 10, now=NOW)
    assert decision.status == "failed"
    assert decision.failure_kind == "no_enabled_sources"


@pytest.mark.asyncio
async def test_scheduled_ready_before_slot_waits():
    repo = FakeReadinessRepository(_refresh(), [_source()])
    decision = await PublicationReadinessService(repo).reconcile(
        None, 10, now=NOW - dt.timedelta(minutes=1)
    )
    assert decision.status == "ready_waiting_slot"


@pytest.mark.asyncio
async def test_persisted_ready_state_cannot_bypass_scheduled_slot():
    repo = FakeReadinessRepository(_refresh(status="ready_for_preparation"), [_source()])
    decision = await PublicationReadinessService(repo).reconcile(
        None, 10, now=NOW - dt.timedelta(minutes=1)
    )
    assert decision.status == "ready_waiting_slot"
    assert repo.refresh.status == "ready_waiting_slot"


@pytest.mark.asyncio
async def test_terminal_source_failure_is_immediate():
    repo = FakeReadinessRepository(
        _refresh(), [replace(_source(), status="degraded", collection_outcome="auth_required")]
    )
    decision = await PublicationReadinessService(repo).reconcile(None, 10, now=NOW)
    assert decision.status == "failed"
    assert decision.failure_kind == "source_auth_required"


@pytest.mark.asyncio
async def test_ready_after_deadline_is_still_fail_closed():
    repo = FakeReadinessRepository(_refresh(deadline_at=NOW - dt.timedelta(seconds=1)), [_source()])
    decision = await PublicationReadinessService(repo).reconcile(None, 10, now=NOW)
    assert decision.status == "failed"
    assert decision.failure_kind == "readiness_deadline"
