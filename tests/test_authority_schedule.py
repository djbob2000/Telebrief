# ruff: noqa: S101
"""Re-enqueueing of deferred Gate stories at their retry instant (queue guarantee).

The background job is run for real; only Procrastinate's ``configure`` is replaced
so the scheduled time and the deduplication lock can be read back.
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
    invalid_batch,
    keep_batch,
    now,
    retry_state,
    scope_patch,
    target,
)

import src.jobs.event_authority as jobs


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

    def fake_configure(**options):
        return _Defer(options)

    monkeypatch.setattr(jobs.process_background_authority_batch, "configure", fake_configure)
    return calls


def _wire_background_job(monkeypatch, service, targets, cfg):
    runtime = SimpleNamespace(
        uow=Uow(), config=SimpleNamespace(settings=SimpleNamespace(event_pipeline=cfg))
    )
    monkeypatch.setattr(jobs, "get_runtime", lambda: runtime)

    loads = {"count": 0}

    async def load_targets(edition_id, *, limit, shard_index=0, shard_count=1):
        # The first call is the batch; later calls ask whether work remains, and the
        # stories in this fixture are either sent or waiting, so none remains to run.
        loads["count"] += 1
        return list(targets)[:limit] if loads["count"] == 1 else []

    monkeypatch.setattr(jobs, "_load_background_targets", load_targets)
    monkeypatch.setattr(
        jobs.EventAuthorityService,
        "from_runtime",
        classmethod(lambda cls, rt, config=None: service),
    )


@pytest.mark.asyncio
async def test_deferred_invalid_story_is_scheduled_at_its_retry_instant(monkeypatch, deferred):
    retries = Retries()
    service, triage = build_service(
        singleton_enabled=False, responses=[invalid_batch()], retries=retries
    )
    cfg = config_namespace(singleton_enabled=False)
    _wire_background_job(monkeypatch, service, [target()], cfg)

    await jobs.process_background_authority_batch.func(5, 0)

    retry_at = retries.failures[0]["next_retry_at"]
    assert len(deferred) == 1
    assert deferred[0]["schedule_at"] == retry_at
    assert deferred[0]["args"] == {"edition_id": 5, "shard_id": 0}
    assert "authority-background-retry:5:0:" in deferred[0]["queueing_lock"]


@pytest.mark.asyncio
async def test_retry_wait_only_batch_is_still_scheduled_at_the_wait_end(monkeypatch, deferred):
    wait_until = now() + dt.timedelta(minutes=10)
    retries = Retries(
        {KEY: retry_state(attempt_count=1, exhausted_at=None, next_retry_at=wait_until)}
    )
    service, triage = build_service(singleton_enabled=True, responses=[], retries=retries)
    _wire_background_job(monkeypatch, service, [target()], config_namespace())

    await jobs.process_background_authority_batch.func(5, 0)

    # Nothing was sent: the batch stops early, but the story must not be forgotten.
    assert triage.story_counts == []
    assert [call["schedule_at"] for call in deferred] == [wait_until]


@pytest.mark.asyncio
async def test_scheduled_retry_sends_the_story_once_its_wait_has_ended(monkeypatch, deferred):
    # The scheduled run happens after next_retry_at: the story is due and is sent.
    retries = Retries(
        {
            KEY: retry_state(
                attempt_count=1, exhausted_at=None, next_retry_at=now() - dt.timedelta(seconds=1)
            )
        }
    )
    service, triage = build_service(
        singleton_enabled=False, responses=[keep_batch()], retries=retries
    )
    _wire_background_job(
        monkeypatch, service, [target()], config_namespace(singleton_enabled=False)
    )

    await jobs.process_background_authority_batch.func(5, 0)

    assert triage.story_counts == [1]
    assert deferred == []  # resolved in this run: no further retry to schedule


@pytest.mark.asyncio
async def test_nothing_deferred_means_nothing_scheduled(monkeypatch, deferred):
    service, triage = build_service(
        singleton_enabled=False, responses=[keep_batch()], retries=Retries()
    )
    _wire_background_job(
        monkeypatch, service, [target()], config_namespace(singleton_enabled=False)
    )

    await jobs.process_background_authority_batch.func(5, 0)

    assert triage.story_counts == [1]
    assert deferred == []


@pytest.mark.asyncio
async def test_exhausted_story_schedules_nothing(monkeypatch, deferred):
    # After the second failed attempt the story is terminal: no new wake-up is needed.
    retries = Retries(
        {
            KEY: retry_state(
                attempt_count=1, exhausted_at=None, next_retry_at=now() - dt.timedelta(minutes=1)
            )
        }
    )
    service, triage = build_service(
        singleton_enabled=False, responses=[invalid_batch()], retries=retries
    )
    _wire_background_job(
        monkeypatch, service, [target()], config_namespace(singleton_enabled=False)
    )

    await jobs.process_background_authority_batch.func(5, 0)

    assert retries.failures[0]["exhausted"] is True
    assert deferred == []


@pytest.mark.asyncio
async def test_defer_helper_uses_one_lock_per_instant(monkeypatch, deferred):
    at = now() + dt.timedelta(minutes=5)
    later = at + dt.timedelta(seconds=30)

    await jobs.defer_background_authority_retry(5, 1, at)
    await jobs.defer_background_authority_retry(5, 1, at)
    await jobs.defer_background_authority_retry(5, 1, later)

    locks = [call["queueing_lock"] for call in deferred]
    assert locks[0] == locks[1]  # the same instant collapses into one job
    assert locks[0] != locks[2]  # a later instant gets its own job
    assert all(call["schedule_at"] in {at, later} for call in deferred)
