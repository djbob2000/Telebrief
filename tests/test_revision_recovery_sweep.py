# ruff: noqa: S101
"""The periodic sweep re-defers unfinished revisions without waiting for a collection scan."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import src.jobs.event_processing as event_processing
from src.jobs.event_processing import (
    RECOVERY_MAX_REVISIONS_PER_SWEEP,
    RECOVERY_RETRY_AFTER_SECONDS,
    requeue_unfinished_revisions,
)
from src.repositories.event_revision_processing import EventRevisionProcessingRepository

_CONN = object()


class _FakeUow:
    @asynccontextmanager
    async def transaction(self):
        yield _CONN


def _runtime(batch_size: int) -> SimpleNamespace:
    settings = SimpleNamespace(
        event_pipeline=SimpleNamespace(
            revision_processing_batch_size=batch_size,
            thin_fragment_routing_enabled=True,
            thin_fragment_max_content_words=2,
            thin_fragment_near_identical_similarity=0.95,
            thin_fragment_conversation_minutes=30,
        )
    )
    return SimpleNamespace(uow=_FakeUow(), config=SimpleNamespace(settings=settings))


class _DeferCalls:
    def __init__(self) -> None:
        self.batches: list[list[int]] = []
        self.connections: list[object] = []

    def configure(self, **options):
        self.connections.append(options["connection"])
        return self

    async def defer_async(self, *, revision_ids: list[int]) -> None:
        self.batches.append(revision_ids)


@pytest.fixture
def defer_calls(monkeypatch) -> _DeferCalls:
    calls = _DeferCalls()
    monkeypatch.setattr(event_processing, "process_event_revisions_task", calls)
    return calls


def _stub_recoverable(monkeypatch, revision_ids: list[int], seen: dict) -> None:
    async def fake_list(self, conn, *, retry_after_seconds, limit, reply_parent_hops):
        seen.update(
            conn=conn,
            retry_after_seconds=retry_after_seconds,
            limit=limit,
            reply_parent_hops=reply_parent_hops,
        )
        return list(revision_ids)

    monkeypatch.setattr(
        EventRevisionProcessingRepository, "list_recoverable_revision_ids", fake_list
    )


@pytest.mark.asyncio
async def test_sweep_defers_every_recoverable_revision_in_batches(monkeypatch, defer_calls):
    seen: dict = {}
    _stub_recoverable(monkeypatch, list(range(1, 121)), seen)
    monkeypatch.setattr(event_processing, "get_runtime", lambda: _runtime(batch_size=50))

    await requeue_unfinished_revisions(timestamp=0)

    assert seen == {
        "conn": _CONN,
        "retry_after_seconds": RECOVERY_RETRY_AFTER_SECONDS,
        "limit": RECOVERY_MAX_REVISIONS_PER_SWEEP,
        "reply_parent_hops": 3,
    }
    assert [len(batch) for batch in defer_calls.batches] == [50, 50, 20]
    assert [rid for batch in defer_calls.batches for rid in batch] == list(range(1, 121))
    assert set(defer_calls.connections) == {_CONN}


@pytest.mark.asyncio
async def test_sweep_does_nothing_when_no_revision_is_unfinished(monkeypatch, defer_calls):
    _stub_recoverable(monkeypatch, [], {})
    monkeypatch.setattr(event_processing, "get_runtime", lambda: _runtime(batch_size=50))

    await requeue_unfinished_revisions(timestamp=0)

    assert defer_calls.batches == []


def test_recovery_waits_before_retrying_a_fresh_job():
    # A failed or pending row younger than this is probably still queued or retrying.
    assert RECOVERY_RETRY_AFTER_SECONDS >= 600


def test_sweep_is_bounded_per_tick():
    assert 0 < RECOVERY_MAX_REVISIONS_PER_SWEEP <= 1000
