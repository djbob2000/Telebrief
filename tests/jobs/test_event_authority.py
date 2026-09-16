"""Publication authority job wrapper regressions."""

from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.jobs import event_authority as authority_jobs
from src.jobs.event_authority import (
    AuthorityCoordinationBusy,
    authority_block_reason,
    log_authority_complete,
)
from src.processing.event_authority import AuthorityBatchStats


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stats", "remaining", "expected"),
    [
        (AuthorityBatchStats(), 0, None),
        (AuthorityBatchStats(terminal=1), 4, "terminal"),
        (AuthorityBatchStats(provider_failures=1), 4, "provider_failure"),
        (AuthorityBatchStats(retry_wait=1), 4, "retry_wait"),
        (AuthorityBatchStats(busy=1), 4, "coordination_busy"),
        (AuthorityBatchStats(triaged=1), 4, "pending"),
    ],
)
def test_authority_block_reason_has_stable_precedence(stats, remaining, expected):
    assert authority_block_reason(stats, remaining) == expected


@pytest.mark.unit
def test_authority_completion_log_contains_semantic_counters(caplog):
    stats = AuthorityBatchStats(
        requested=2,
        claimed=2,
        triaged=1,
        busy=1,
        provider_failures=0,
        retry_wait=0,
        terminal=0,
    )
    with caplog.at_level(logging.INFO, logger="src.jobs.event_authority"):
        log_authority_complete(
            mode="publication",
            intent_id=68,
            edition_id=1,
            shard_id=2,
            stats=stats,
            remaining_gap=1,
            duration_ms=42,
            backlog_before=5,
            backlog_after=4,
            progressed=True,
            continuation_enqueued=False,
            prompt_chars=1200,
        )
    record = caplog.records[-1]
    assert record.mode == "publication"
    assert record.intent_id == 68
    assert record.edition_id == 1
    assert record.shard_id == 2
    assert record.backlog_before == 5
    assert record.backlog_after == 4
    assert record.claimed == 2
    assert record.triaged == 1
    assert record.provider_failures == 0
    assert record.retry_wait == 0
    assert record.terminal == 0
    assert record.remaining_gap == 1
    assert record.duration_ms == 42
    assert record.block_reason == "pending"
    assert record.progressed is True
    assert record.continuation_enqueued is False
    assert record.prompt_chars == 1200


@pytest.mark.unit
def test_contention_error_is_distinct_from_provider_failure():
    assert issubclass(AuthorityCoordinationBusy, RuntimeError)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publication_dispatcher_fans_out_shards_without_calling_service(monkeypatch):
    deadline_at = authority_jobs.dt.datetime.now(
        authority_jobs.dt.timezone.utc
    ) + authority_jobs.dt.timedelta(minutes=20)
    intent = SimpleNamespace(status="processing", deadline_at=deadline_at, edition_id=1)
    diagnostics_repo = SimpleNamespace(get_refresh_run=AsyncMock(return_value=intent))
    orchestrator = SimpleNamespace(readiness_repo=diagnostics_repo)

    runtime = SimpleNamespace(
        uow=MagicMock(),
        config=SimpleNamespace(
            settings=SimpleNamespace(event_pipeline=SimpleNamespace(authority_shard_count=3))
        ),
    )
    runtime.uow.transaction.return_value.__aenter__.return_value = AsyncMock()

    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)
    monkeypatch.setattr(authority_jobs, "PublicationOrchestrator", lambda **kwargs: orchestrator)

    mock_service_from_runtime = MagicMock()
    monkeypatch.setattr(
        authority_jobs.EventAuthorityService, "from_runtime", mock_service_from_runtime
    )

    configured = MagicMock()
    configured.defer_async = AsyncMock()
    configure = MagicMock(return_value=configured)
    monkeypatch.setattr(authority_jobs.process_publication_authority_batch, "configure", configure)

    await authority_jobs.process_publication_authority_gap(intent_id=42)

    mock_service_from_runtime.assert_not_called()
    assert configure.call_count == 3
    configured_calls = [c.kwargs for c in configure.call_args_list]
    assert configured_calls == [
        {
            "priority": authority_jobs.PUBLICATION_AUTHORITY_PRIORITY,
            "queueing_lock": "publication-authority:42:0",
        },
        {
            "priority": authority_jobs.PUBLICATION_AUTHORITY_PRIORITY,
            "queueing_lock": "publication-authority:42:1",
        },
        {
            "priority": authority_jobs.PUBLICATION_AUTHORITY_PRIORITY,
            "queueing_lock": "publication-authority:42:2",
        },
    ]
    defer_calls = [c.kwargs for c in configured.defer_async.call_args_list]
    assert defer_calls == [
        {"intent_id": 42, "shard_id": 0},
        {"intent_id": 42, "shard_id": 1},
        {"intent_id": 42, "shard_id": 2},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publication_authority_batch_preserves_boundaries_and_coordination_scope(monkeypatch):
    current_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    snapshot_at = current_at - dt.timedelta(minutes=5)
    cutoff_at = snapshot_at - dt.timedelta(minutes=5)
    deadline_at = current_at + dt.timedelta(minutes=15)

    class Clock(authority_jobs.dt.datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return current_at

    intent = SimpleNamespace(
        status="processing",
        deadline_at=deadline_at,
        knowledge_snapshot_at=snapshot_at,
        normal_source_cutoff_at=cutoff_at,
        edition_id=1,
    )
    target = SimpleNamespace(story_id=10, assignment_id=20)
    diagnostics_repo = SimpleNamespace(
        get_refresh_run=AsyncMock(return_value=intent),
        update_authority_diagnostics=AsyncMock(),
    )
    orchestrator = SimpleNamespace(
        readiness_repo=diagnostics_repo,
        find_authority_gap_targets=AsyncMock(side_effect=[[target], []]),
    )
    authority_service = SimpleNamespace(
        process_batch=AsyncMock(
            return_value=SimpleNamespace(
                stats=AuthorityBatchStats(triaged=1), enrichment_targets=()
            )
        )
    )
    runtime = SimpleNamespace(
        uow=MagicMock(),
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(
                    authority_shard_count=4,
                    triage_batch_size=10,
                )
            )
        ),
    )
    runtime.uow.transaction.return_value.__aenter__.return_value = AsyncMock()

    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)
    monkeypatch.setattr(authority_jobs.dt, "datetime", Clock)
    monkeypatch.setattr(authority_jobs, "PublicationOrchestrator", lambda **kwargs: orchestrator)
    monkeypatch.setattr(
        authority_jobs.EventAuthorityService,
        "from_runtime",
        lambda runtime, config: authority_service,
    )
    defer_reconcile = AsyncMock()
    monkeypatch.setattr(authority_jobs, "_defer_publication_reconcile", defer_reconcile)

    await authority_jobs.process_publication_authority_batch(intent_id=55, shard_id=2)

    first_call_kwargs = orchestrator.find_authority_gap_targets.await_args_list[0].kwargs
    assert first_call_kwargs["evaluation_at"] == snapshot_at
    assert first_call_kwargs["shard_index"] == 2
    assert first_call_kwargs["shard_count"] == 4

    authority_service.process_batch.assert_awaited_once_with(
        [target], mode="publication", coordination_scope="publication:55:2"
    )
    defer_reconcile.assert_awaited_once_with(55)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publication_authority_records_progress_after_provider_batch(monkeypatch):
    started_at = authority_jobs.dt.datetime(
        2026, 9, 10, 10, 0, tzinfo=authority_jobs.dt.timezone.utc
    )
    observed_at = started_at + authority_jobs.dt.timedelta(seconds=5)
    deadline_at = started_at + authority_jobs.dt.timedelta(minutes=20)

    class Clock(authority_jobs.dt.datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            del tz
            cls.calls += 1
            return started_at if cls.calls == 1 else observed_at

    intent = SimpleNamespace(
        status="processing",
        deadline_at=deadline_at,
        edition_id=1,
        knowledge_snapshot_at=None,
    )
    target = SimpleNamespace(story_id=10, assignment_id=20)
    diagnostics_repo = SimpleNamespace(
        get_refresh_run=AsyncMock(return_value=intent),
        update_authority_diagnostics=AsyncMock(),
    )
    orchestrator = SimpleNamespace(
        readiness_repo=diagnostics_repo,
        find_authority_gap_targets=AsyncMock(side_effect=[[target], []]),
    )
    authority_service = SimpleNamespace(
        process_batch=AsyncMock(
            return_value=SimpleNamespace(
                stats=AuthorityBatchStats(triaged=1), enrichment_targets=()
            )
        )
    )
    runtime = SimpleNamespace(
        uow=MagicMock(),
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(
                    authority_shard_count=1,
                    triage_batch_size=10,
                )
            )
        ),
    )
    runtime.uow.transaction.return_value.__aenter__.return_value = AsyncMock()

    monkeypatch.setattr(authority_jobs.dt, "datetime", Clock)
    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)
    monkeypatch.setattr(authority_jobs, "PublicationOrchestrator", lambda **kwargs: orchestrator)
    monkeypatch.setattr(
        authority_jobs.EventAuthorityService,
        "from_runtime",
        lambda runtime, config: authority_service,
    )
    defer_reconcile = AsyncMock()
    monkeypatch.setattr(authority_jobs, "_defer_publication_reconcile", defer_reconcile)

    await authority_jobs.process_publication_authority_batch(68, shard_id=0)

    assert (
        orchestrator.find_authority_gap_targets.await_args_list[1].kwargs["evaluation_at"]
        == observed_at
    )
    diagnostics_repo.update_authority_diagnostics.assert_awaited_once_with(
        runtime.uow.transaction.return_value.__aenter__.return_value,
        refresh_run_id=68,
        observed_at=observed_at,
        gap_count=0,
        block_reason=None,
        terminal_count=0,
    )
    defer_reconcile.assert_awaited_once_with(68)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_background_dispatch_does_not_queueing_lock_execution_locked_batch(
    monkeypatch,
):
    configured = MagicMock()
    configured.defer_async = AsyncMock()
    configure = MagicMock(return_value=configured)

    monkeypatch.setattr(
        authority_jobs,
        "_load_background_targets",
        AsyncMock(return_value=[SimpleNamespace(story_id=1)]),
    )
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(background_authority_enabled=True)
            )
        )
    )
    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)
    monkeypatch.setattr(
        authority_jobs.process_background_authority_batch,
        "configure",
        configure,
    )

    await authority_jobs.dispatch_background_authority(edition_id=1)

    configure.assert_called_once_with(
        priority=authority_jobs.BACKGROUND_AUTHORITY_PRIORITY,
    )
    configured.defer_async.assert_awaited_once_with(edition_id=1, shard_id=0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_background_authority_batch_schedules_continuation_on_progress(monkeypatch):
    target = SimpleNamespace(story_id=10, assignment_id=20)
    authority_service = SimpleNamespace(
        process_batch=AsyncMock(
            return_value=SimpleNamespace(
                stats=AuthorityBatchStats(triaged=1), enrichment_targets=()
            )
        )
    )
    runtime = SimpleNamespace(
        uow=MagicMock(),
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(
                    background_authority_enabled=True,
                    triage_batch_size=10,
                    authority_shard_count=4,
                    active_window_hours=72,
                )
            )
        ),
    )
    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)
    load_targets = AsyncMock(side_effect=[[target], [target]])
    monkeypatch.setattr(authority_jobs, "_load_background_targets", load_targets)
    monkeypatch.setattr(
        authority_jobs.EventAuthorityService,
        "from_runtime",
        lambda runtime, config: authority_service,
    )
    defer_continuation = AsyncMock()
    monkeypatch.setattr(
        authority_jobs, "_defer_background_authority_continuation", defer_continuation
    )

    await authority_jobs.process_background_authority_batch(edition_id=1, shard_id=2)

    authority_service.process_batch.assert_awaited_once_with(
        [target], mode="background", coordination_scope="authority:2"
    )
    defer_continuation.assert_awaited_once_with(1, 2)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stats",
    [
        AuthorityBatchStats(provider_failures=1),
        AuthorityBatchStats(retry_wait=1, triaged=0),
    ],
)
async def test_process_background_authority_batch_does_not_tight_loop_on_failure_or_retry(
    monkeypatch, stats
):
    target = SimpleNamespace(story_id=10, assignment_id=20)
    authority_service = SimpleNamespace(
        process_batch=AsyncMock(return_value=SimpleNamespace(stats=stats, enrichment_targets=()))
    )
    runtime = SimpleNamespace(
        uow=MagicMock(),
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(
                    background_authority_enabled=True,
                    triage_batch_size=10,
                    authority_shard_count=4,
                    active_window_hours=72,
                )
            )
        ),
    )
    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)
    load_targets = AsyncMock(return_value=[target])
    monkeypatch.setattr(authority_jobs, "_load_background_targets", load_targets)
    monkeypatch.setattr(
        authority_jobs.EventAuthorityService,
        "from_runtime",
        lambda runtime, config: authority_service,
    )
    defer_continuation = AsyncMock()
    monkeypatch.setattr(
        authority_jobs, "_defer_background_authority_continuation", defer_continuation
    )

    await authority_jobs.process_background_authority_batch(edition_id=1, shard_id=2)

    defer_continuation.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_background_targets_query_uses_active_cutoff_dirty_and_descending_order():
    from src.domain.event_authority import AuthorityTarget
    from src.repositories.event_authority import EventAuthorityRepository

    class Cursor:
        async def fetchall(self):
            return [(12, 102)]

    class Connection:
        def __init__(self):
            self.query = ""
            self.params = ()

        async def execute(self, query, params):
            self.query = query
            self.params = params
            return Cursor()

    now = authority_jobs.dt.datetime(2026, 9, 11, 12, 0, tzinfo=authority_jobs.dt.timezone.utc)
    conn = Connection()
    result = await EventAuthorityRepository().list_background_targets(
        cast(Any, conn),
        edition_id=1,
        triage_version="v10",
        scope_version="v1",
        scope_config_hash="scope_hash",
        now=now,
        limit=10,
        active_window_hours=72,
    )

    assert result == [
        AuthorityTarget(
            story_id=12,
            assignment_id=102,
            edition_id=1,
            triage_version="v10",
            scope_version="v1",
            scope_config_hash="scope_hash",
        )
    ]
    # Invariants: must be analysis_dirty, bounded by active cutoff, and ordered DESC (freshest first)
    assert "sc.analysis_dirty = TRUE" in conn.query
    assert "sc.last_seen_at >= %s" in conn.query
    assert "ORDER BY sc.last_seen_at DESC, sc.story_id DESC" in conn.query
    expected_cutoff = now - authority_jobs.dt.timedelta(hours=72)
    assert expected_cutoff in conn.params


@pytest.mark.unit
@pytest.mark.asyncio
async def test_defer_event_processing_savepoint_handles_already_enqueued(monkeypatch):
    from procrastinate.exceptions import AlreadyEnqueued

    from src.publication.orchestrator import PublicationOrchestrator

    savepoint_entered = False
    savepoint_exited = False

    class FakeSavepoint:
        async def __aenter__(self):
            nonlocal savepoint_entered
            savepoint_entered = True
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            nonlocal savepoint_exited
            savepoint_exited = True
            return False  # Let exception bubble so try/except AlreadyEnqueued catches it

    class FakeConn:
        def transaction(self):
            return FakeSavepoint()

    configured = MagicMock()
    configured.defer_async = AsyncMock(side_effect=AlreadyEnqueued())
    configure = MagicMock(return_value=configured)

    monkeypatch.setattr(authority_jobs.process_publication_authority_gap, "configure", configure)

    orchestrator = PublicationOrchestrator(
        uow=MagicMock(),
        config=cast(Any, SimpleNamespace(settings=SimpleNamespace())),
    )
    conn = FakeConn()

    # Should not raise exception
    await orchestrator._defer_event_processing(
        cast(Any, conn),
        intent_id=7,
        edition_id=1,
        story_ids=(9001,),
    )

    assert savepoint_entered is True
    assert savepoint_exited is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_defer_event_processing_treats_queueing_lock_race_as_already_queued(monkeypatch):
    from procrastinate.exceptions import UniqueViolation

    from src.publication.orchestrator import PublicationOrchestrator

    configured = MagicMock()
    configured.defer_async = AsyncMock(
        side_effect=UniqueViolation(
            constraint_name="procrastinate_jobs_queueing_lock_idx_v1",
            queueing_lock="publication-authority-dispatch:7",
        )
    )
    monkeypatch.setattr(
        authority_jobs.process_publication_authority_gap,
        "configure",
        MagicMock(return_value=configured),
    )

    orchestrator = PublicationOrchestrator(
        uow=MagicMock(),
        config=cast(Any, SimpleNamespace(settings=SimpleNamespace())),
    )

    class FakeConn:
        def transaction(self):
            return FakeSavepoint()

    class FakeSavepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False

    await orchestrator._defer_event_processing(
        cast(Any, FakeConn()),
        intent_id=7,
        edition_id=1,
        story_ids=(9001,),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_background_authority_disabled_by_default_prevents_dispatch(monkeypatch):
    configured = MagicMock()
    configured.defer_async = AsyncMock()
    configure = MagicMock(return_value=configured)
    monkeypatch.setattr(
        authority_jobs.dispatch_background_authority,
        "configure",
        configure,
    )
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(background_authority_enabled=False)
            )
        )
    )
    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)

    await authority_jobs.request_background_authority_dispatch(1)

    configure.assert_not_called()
    configured.defer_async.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_queued_background_authority_batch_is_noop_when_disabled(monkeypatch):
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(
                    background_authority_enabled=False,
                    triage_batch_size=10,
                )
            )
        )
    )
    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)
    load_targets = AsyncMock()
    load_targets.return_value = [SimpleNamespace(story_id=1, assignment_id=2)]
    monkeypatch.setattr(authority_jobs, "_load_background_targets", load_targets)
    authority_service = SimpleNamespace(process_batch=AsyncMock())
    monkeypatch.setattr(
        authority_jobs.EventAuthorityService,
        "from_runtime",
        authority_service,
    )

    await authority_jobs.process_background_authority_batch(edition_id=1)

    load_targets.assert_not_awaited()
    authority_service.process_batch.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_direct_background_authority_dispatch_is_noop_when_disabled(monkeypatch):
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(background_authority_enabled=False)
            )
        )
    )
    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)
    load_targets = AsyncMock()
    load_targets.return_value = [SimpleNamespace(story_id=1)]
    monkeypatch.setattr(authority_jobs, "_load_background_targets", load_targets)
    configured = SimpleNamespace(defer_async=AsyncMock())
    process_batch = MagicMock(return_value=configured)
    monkeypatch.setattr(
        authority_jobs.process_background_authority_batch,
        "configure",
        process_batch,
    )

    await authority_jobs.dispatch_background_authority(edition_id=1)

    load_targets.assert_not_awaited()
    process_batch.assert_not_called()
    configured.defer_async.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_periodic_background_authority_dispatch_skipped_when_disabled(monkeypatch):
    request_mock = AsyncMock()
    monkeypatch.setattr(
        authority_jobs,
        "request_background_authority_dispatch",
        request_mock,
    )
    runtime = SimpleNamespace(
        uow=MagicMock(),
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(background_authority_enabled=False)
            )
        ),
    )
    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)

    await authority_jobs.periodic_background_authority_dispatch(12345)

    request_mock.assert_not_awaited()
    runtime.uow.transaction.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publication_shards_monotonic_gap_drain_under_frozen_snapshot(monkeypatch):
    frozen_now = authority_jobs.dt.datetime(
        2026, 9, 14, 10, 0, tzinfo=authority_jobs.dt.timezone.utc
    )

    class Clock(authority_jobs.dt.datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return frozen_now

    intent = SimpleNamespace(
        status="processing",
        deadline_at=frozen_now + authority_jobs.dt.timedelta(minutes=15),
        edition_id=1,
        knowledge_snapshot_at=frozen_now,
    )

    logged_gaps: list[int] = []

    def fake_log_authority_complete(**kwargs):
        if kwargs.get("remaining_gap") is not None:
            logged_gaps.append(kwargs["remaining_gap"])

    monkeypatch.setattr(authority_jobs, "log_authority_complete", fake_log_authority_complete)

    # Shard targets shrinking as work completes
    active_gap: dict[int, int] = {1: 0, 2: 1}  # story_id -> shard_id

    async def fake_find_targets(*args, **kwargs):
        shard_index = kwargs.get("shard_index")
        if shard_index is not None:
            return [
                SimpleNamespace(story_id=sid, assignment_id=sid * 10)
                for sid, sh in active_gap.items()
                if sh == shard_index
            ]
        return [SimpleNamespace(story_id=sid, assignment_id=sid * 10) for sid in active_gap]

    async def fake_process_batch(targets, **kwargs):
        for t in targets:
            active_gap.pop(t.story_id, None)
        return SimpleNamespace(
            stats=AuthorityBatchStats(
                requested=len(targets), claimed=len(targets), triaged=len(targets)
            ),
            enrichment_targets=(),
        )

    orchestrator = SimpleNamespace(
        readiness_repo=SimpleNamespace(
            get_refresh_run=AsyncMock(return_value=intent),
            update_authority_diagnostics=AsyncMock(),
        ),
        find_authority_gap_targets=AsyncMock(side_effect=fake_find_targets),
        count_authority_gap=AsyncMock(side_effect=[2, 1, 0]),
    )

    authority_service = SimpleNamespace(process_batch=AsyncMock(side_effect=fake_process_batch))

    runtime = SimpleNamespace(
        uow=MagicMock(),
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(authority_shard_count=2, triage_batch_size=10)
            )
        ),
    )
    runtime.uow.transaction.return_value.__aenter__.return_value = AsyncMock()

    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)
    monkeypatch.setattr(authority_jobs.dt, "datetime", Clock)
    monkeypatch.setattr(authority_jobs, "PublicationOrchestrator", lambda **kwargs: orchestrator)
    monkeypatch.setattr(
        authority_jobs.EventAuthorityService, "from_runtime", lambda r, c: authority_service
    )
    monkeypatch.setattr(authority_jobs, "_defer_publication_reconcile", AsyncMock())

    # Run 3 consecutive batches
    await authority_jobs.process_publication_authority_batch(intent_id=77, shard_id=0)
    await authority_jobs.process_publication_authority_batch(intent_id=77, shard_id=1)
    await authority_jobs.process_publication_authority_batch(intent_id=77, shard_id=0)

    # Verify that remaining_gap monotonically non-increases
    assert len(logged_gaps) >= 2
    for prev, curr in zip(logged_gaps, logged_gaps[1:], strict=False):
        assert curr <= prev, f"remaining_gap increased from {prev} to {curr}"
    assert logged_gaps[-1] == 0
