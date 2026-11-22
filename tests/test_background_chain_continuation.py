# ruff: noqa: S101
"""Background authority chain: which batch outcomes keep the chain going.

A semantically invalid Gate answer (invalid_response) is not a provider failure, so a
batch with real progress continues to the remaining ready stories. Timeouts, quota
errors and batches without progress still stop the chain. Unresolved stories wait for
their own next_retry_at, and that deferred retry is always scheduled.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from authority_fakes import (
    KEY,
    Retries,
    Uow,
    build_service,
    config_namespace,
    keep_gate,
    now,
    retry_state,
    scope_patch,
    target,
)

import src.jobs.event_authority as jobs
from src.processing.event_triage import StoryGateBatchResult


@pytest.fixture(autouse=True)
def _edition_scope():
    with scope_patch():
        yield


@pytest.fixture
def deferred(monkeypatch):
    """Record every background job the code schedules, with its options."""
    calls: list[dict] = []

    class _Defer:
        def __init__(self, options: dict) -> None:
            self.options = options

        async def defer_async(self, **job_args):
            calls.append({**self.options, "args": job_args})

    monkeypatch.setattr(
        jobs.process_background_authority_batch,
        "configure",
        lambda **options: _Defer(options),
    )
    return calls


@pytest.fixture
def logged(monkeypatch):
    """Capture the cycle log and skip the telemetry count query."""
    calls: list[dict] = []
    monkeypatch.setattr(jobs, "log_authority_complete", lambda **kw: calls.append(kw))

    async def no_backlog(edition_id, *, shard_index, shard_count):
        return None

    monkeypatch.setattr(jobs, "_load_background_backlog", no_backlog)
    return calls


def gate_batch(*, keep_ids=(), invalid_ids=(), error_kind=None) -> StoryGateBatchResult:
    return StoryGateBatchResult(
        results=tuple(keep_gate(i) for i in keep_ids),
        deferred_story_ids=tuple(invalid_ids),
        batch_error_kind=error_kind,
        invalid_story_ids=tuple(invalid_ids),
        invalid_reasons=dict.fromkeys(invalid_ids, "drop_without_valid_brief"),
    )


def wire(monkeypatch, service, targets, *, remaining, shard_count=1):
    """First load returns the batch; the later limit-1 load asks what remains."""
    cfg = config_namespace(singleton_enabled=False)
    cfg.authority_shard_count = shard_count
    runtime = SimpleNamespace(
        uow=Uow(), config=SimpleNamespace(settings=SimpleNamespace(event_pipeline=cfg))
    )
    monkeypatch.setattr(jobs, "get_runtime", lambda: runtime)
    loads = {"count": 0}

    async def load_targets(edition_id, *, limit, shard_index=0, shard_count=1):
        loads["count"] += 1
        if loads["count"] == 1:
            return list(targets)[:limit]
        return list(remaining)[:limit]

    monkeypatch.setattr(jobs, "_load_background_targets", load_targets)
    monkeypatch.setattr(
        jobs.EventAuthorityService,
        "from_runtime",
        classmethod(lambda cls, rt, config=None: service),
    )


def _continuations(deferred, shard_id=0):
    return [
        c
        for c in deferred
        if c.get("queueing_lock") == f"authority-background-continuation:5:{shard_id}"
    ]


def _retries_for(deferred, shard_id=0):
    return [
        c
        for c in deferred
        if str(c.get("queueing_lock", "")).startswith(f"authority-background-retry:5:{shard_id}:")
    ]


@pytest.mark.asyncio
async def test_partially_invalid_batch_continues_to_remaining_ready_stories(
    monkeypatch, deferred, logged
):
    retries = Retries()
    service, triage = build_service(
        singleton_enabled=False,
        responses=[gate_batch(keep_ids=(2,), invalid_ids=(1,), error_kind="invalid_response")],
        retries=retries,
    )
    wire(monkeypatch, service, [target(1, 10), target(2, 20)], remaining=[target(3, 30)])

    await jobs.process_background_authority_batch.func(5, 0)

    assert logged[-1]["stop_reason"] == "continuation_scheduled"
    assert logged[-1]["stats"].provider_failures == 0
    assert logged[-1]["stats"].invalid_unresolved == 1
    assert len(_continuations(deferred)) == 1
    # The invalid story is retried at its own instant, not in this loop.
    assert len(_retries_for(deferred)) == 1
    assert _retries_for(deferred)[0]["schedule_at"] == retries.failures[0]["next_retry_at"]


@pytest.mark.asyncio
async def test_fully_invalid_batch_without_progress_stops_the_chain(monkeypatch, deferred, logged):
    retries = Retries()
    service, _ = build_service(
        singleton_enabled=False,
        responses=[gate_batch(invalid_ids=(1,), error_kind="invalid_response")],
        retries=retries,
    )
    wire(monkeypatch, service, [target(1, 10)], remaining=[target(3, 30)])

    await jobs.process_background_authority_batch.func(5, 0)

    assert logged[-1]["stop_reason"] == "invalid_no_progress"
    assert _continuations(deferred) == []
    assert len(_retries_for(deferred)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["timeout", "quota"])
async def test_provider_error_stops_chain_even_with_partial_progress(
    monkeypatch, deferred, logged, error_kind
):
    retries = Retries()
    service, _ = build_service(
        singleton_enabled=False,
        responses=[gate_batch(keep_ids=(2,), invalid_ids=(1,), error_kind=error_kind)],
        retries=retries,
    )
    wire(monkeypatch, service, [target(1, 10), target(2, 20)], remaining=[target(3, 30)])

    await jobs.process_background_authority_batch.func(5, 0)

    assert logged[-1]["stats"].provider_failures == 1
    assert logged[-1]["stop_reason"] == "provider_failure"
    assert _continuations(deferred) == []
    assert len(_retries_for(deferred)) == 1


@pytest.mark.asyncio
async def test_retry_wait_story_spends_no_attempt_and_is_not_resent(monkeypatch, deferred, logged):
    wait_until = now() + dt.timedelta(minutes=10)
    retries = Retries(
        {KEY: retry_state(attempt_count=1, exhausted_at=None, next_retry_at=wait_until)}
    )
    service, triage = build_service(
        singleton_enabled=False,
        responses=[gate_batch(keep_ids=(2,))],
        retries=retries,
    )
    wire(monkeypatch, service, [target(1, 10), target(2, 20)], remaining=[])

    await jobs.process_background_authority_batch.func(5, 0)

    # Only the ready story is sent; the waiting one spends no attempt and no failure is recorded.
    assert triage.story_counts == [1]
    assert retries.failures == []
    # Only the story that was sent is cleared; the waiting story keeps its state.
    assert [c["story_id"] for c in retries.cleared] == [2]
    assert [c["schedule_at"] for c in _retries_for(deferred)] == [wait_until]


@pytest.mark.asyncio
async def test_partial_invalid_continuation_works_per_shard(monkeypatch, deferred, logged):
    retries = Retries()
    service, _ = build_service(
        singleton_enabled=False,
        responses=[gate_batch(keep_ids=(2,), invalid_ids=(1,), error_kind="invalid_response")],
        retries=retries,
    )
    wire(
        monkeypatch,
        service,
        [target(1, 10), target(2, 20)],
        remaining=[target(3, 30)],
        shard_count=2,
    )

    await jobs.process_background_authority_batch.func(5, 1)

    assert len(_continuations(deferred, shard_id=1)) == 1
    assert _continuations(deferred, shard_id=0) == []
    assert len(_retries_for(deferred, shard_id=1)) == 1
    assert _retries_for(deferred, shard_id=0) == []
    assert logged[-1]["shard_id"] == 1
