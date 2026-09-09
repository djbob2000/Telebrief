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
    decision = await PublicationReadinessService(repo).reconcile(
        None, 10, now=NOW, on_deadline="fallback"
    )
    assert decision.status == "ready_for_preparation"
    assert decision.source_cutoff_at == NOW


@pytest.mark.asyncio
async def test_processed_barrier_keeps_refresh_in_processing():
    repo = FakeReadinessRepository(_refresh(), [_source()], unprocessed=1)
    decision = await PublicationReadinessService(repo).reconcile(
        None, 10, now=NOW, on_deadline="fallback"
    )
    assert decision.status == "processing"


@pytest.mark.asyncio
async def test_degraded_source_waits_before_deadline():
    repo = FakeReadinessRepository(_refresh(), [_source(status="degraded")])
    decision = await PublicationReadinessService(repo).reconcile(
        None, 10, now=NOW, on_deadline="fallback"
    )
    assert decision.status == "collecting"


@pytest.mark.asyncio
async def test_deadline_fallback_is_historical():
    historical = NOW - dt.timedelta(minutes=45)
    repo = FakeReadinessRepository(
        _refresh(deadline_at=NOW - dt.timedelta(seconds=1)), [_source(status="degraded")]
    )
    decision = await PublicationReadinessService(repo).reconcile(
        None, 10, now=NOW, on_deadline="fallback"
    )
    assert decision.status == "fallback_ready"
    assert decision.source_cutoff_at == historical
    assert decision.historical_snapshot_at == historical


@pytest.mark.asyncio
async def test_deadline_fail_closed_is_terminal():
    repo = FakeReadinessRepository(
        _refresh(deadline_at=NOW - dt.timedelta(seconds=1)), [_source(status="degraded")]
    )
    decision = await PublicationReadinessService(repo).reconcile(
        None, 10, now=NOW, on_deadline="fail_closed"
    )
    assert decision.status == "failed"
    assert repo.refresh.error_kind == "readiness_deadline"


@pytest.mark.asyncio
async def test_no_bound_sources_is_immediately_ready():
    repo = FakeReadinessRepository(_refresh(), [])
    decision = await PublicationReadinessService(repo).reconcile(
        None, 10, now=NOW, on_deadline="fallback"
    )
    assert decision.status == "ready_for_preparation"
