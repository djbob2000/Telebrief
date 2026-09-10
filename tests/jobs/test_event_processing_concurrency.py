"""Unit tests for revision-processing transaction/retry boundaries."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psycopg
import pytest

from src.jobs.event_processing import _process_cluster_unit_with_retry


@pytest.mark.unit
async def test_deadlock_retries_only_the_cluster_unit(monkeypatch):
    connections = object()
    calls = 0

    class FakeUow:
        @asynccontextmanager
        async def transaction(self):
            yield connections

    class FakeClusteringService:
        async def process_fragment(self, conn, fragment, **kwargs):
            nonlocal calls
            assert conn is connections
            calls += 1
            if calls == 1:
                raise psycopg.errors.DeadlockDetected("cluster deadlock")

    class FakeProcessingRepository:
        async def claims_owned(self, conn, revision_ids, *, claim_token):
            return True

    sleep = AsyncMock()
    monkeypatch.setattr("src.jobs.event_processing.asyncio.sleep", sleep)

    await _process_cluster_unit_with_retry(
        SimpleNamespace(uow=FakeUow()),
        FakeClusteringService(),
        SimpleNamespace(id=1),
        edition_id=1,
        processing_repo=FakeProcessingRepository(),
        revision_ids=[1],
        claim_token=uuid.uuid4(),
        claim_lost=asyncio.Event(),
        fragment_embedding_id=1,
        vector=[1.0, 0.0],
        model="test",
        dimensions=2,
        item_timestamp=dt.datetime.now(dt.timezone.utc),
        join_similarity=0.84,
        active_window_hours=72,
        max_cluster_candidates=20,
    )

    assert calls == 2
    sleep.assert_awaited_once()
