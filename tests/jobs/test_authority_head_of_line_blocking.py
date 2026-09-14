"""Unit tests for head-of-line blocking prevention in publication authority queue."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.jobs.event_authority as authority_jobs
from src.processing.event_authority import AuthorityBatchStats


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publication_authority_continues_when_batch_quarantined_and_actionable_remains(
    monkeypatch,
):
    """When a batch fails and its items are quarantined, the shard must continue to the next actionable items."""
    snapshot_at = dt.datetime(2026, 9, 14, 9, 0, tzinfo=dt.timezone.utc)
    deadline_at = snapshot_at + dt.timedelta(minutes=30)
    observed_at = snapshot_at + dt.timedelta(minutes=1)

    class Clock(authority_jobs.dt.datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return observed_at

    intent = SimpleNamespace(
        status="processing",
        deadline_at=deadline_at,
        knowledge_snapshot_at=snapshot_at,
        normal_source_cutoff_at=snapshot_at,
        edition_id=1,
    )

    batch_targets = [SimpleNamespace(story_id=1, assignment_id=10)]
    remaining_actionable_targets = [SimpleNamespace(story_id=2, assignment_id=20)]

    find_mock = AsyncMock(
        side_effect=[
            batch_targets,
            [batch_targets[0], remaining_actionable_targets[0]],
            remaining_actionable_targets,
        ]
    )

    orchestrator = SimpleNamespace(
        readiness_repo=SimpleNamespace(
            get_refresh_run=AsyncMock(return_value=intent),
            update_authority_diagnostics=AsyncMock(),
        ),
        find_authority_gap_targets=find_mock,
    )

    authority_service = SimpleNamespace(
        process_batch=AsyncMock(
            return_value=SimpleNamespace(
                stats=AuthorityBatchStats(requested=1, provider_failures=1),
                enrichment_targets=(),
            )
        )
    )

    runtime = SimpleNamespace(
        uow=MagicMock(),
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(
                    authority_shard_count=2,
                    triage_batch_size=10,
                )
            )
        ),
    )
    runtime.uow.transaction.return_value.__aenter__.return_value = AsyncMock()

    configured = MagicMock()
    configured.defer_async = AsyncMock()
    configure_mock = MagicMock(return_value=configured)

    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)
    monkeypatch.setattr(authority_jobs.dt, "datetime", Clock)
    monkeypatch.setattr(authority_jobs, "PublicationOrchestrator", lambda **kwargs: orchestrator)
    monkeypatch.setattr(
        authority_jobs.EventAuthorityService, "from_runtime", lambda r, c: authority_service
    )
    monkeypatch.setattr(
        authority_jobs.process_publication_authority_batch, "configure", configure_mock
    )
    monkeypatch.setattr(authority_jobs, "_defer_publication_reconcile", AsyncMock())

    await authority_jobs.process_publication_authority_batch(intent_id=101, shard_id=0)

    # Continuation MUST be enqueued because actionable items remain in this shard
    assert configure_mock.called
    assert (
        configure_mock.call_args.kwargs["queueing_lock"]
        == "publication-authority-continuation:101:0"
    )
    configured.defer_async.assert_awaited_once_with(intent_id=101, shard_id=0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publication_authority_stops_continuation_when_no_actionable_targets_remain(
    monkeypatch,
):
    """When all remaining items in the shard are in retry cooldown, do not continue in a tight loop."""
    snapshot_at = dt.datetime(2026, 9, 14, 9, 0, tzinfo=dt.timezone.utc)
    deadline_at = snapshot_at + dt.timedelta(minutes=30)
    observed_at = snapshot_at + dt.timedelta(minutes=1)

    class Clock(authority_jobs.dt.datetime):
        @classmethod
        def now(cls, tz=None):
            del tz
            return observed_at

    intent = SimpleNamespace(
        status="processing",
        deadline_at=deadline_at,
        knowledge_snapshot_at=snapshot_at,
        normal_source_cutoff_at=snapshot_at,
        edition_id=1,
    )

    batch_targets = [SimpleNamespace(story_id=1, assignment_id=10)]

    find_mock = AsyncMock(
        side_effect=[
            batch_targets,
            batch_targets,
            [],
        ]
    )

    orchestrator = SimpleNamespace(
        readiness_repo=SimpleNamespace(
            get_refresh_run=AsyncMock(return_value=intent),
            update_authority_diagnostics=AsyncMock(),
        ),
        find_authority_gap_targets=find_mock,
    )

    authority_service = SimpleNamespace(
        process_batch=AsyncMock(
            return_value=SimpleNamespace(
                stats=AuthorityBatchStats(requested=1, provider_failures=1),
                enrichment_targets=(),
            )
        )
    )

    runtime = SimpleNamespace(
        uow=MagicMock(),
        config=SimpleNamespace(
            settings=SimpleNamespace(
                event_pipeline=SimpleNamespace(
                    authority_shard_count=2,
                    triage_batch_size=10,
                )
            )
        ),
    )
    runtime.uow.transaction.return_value.__aenter__.return_value = AsyncMock()

    configured = MagicMock()
    configured.defer_async = AsyncMock()
    configure_mock = MagicMock(return_value=configured)

    monkeypatch.setattr(authority_jobs, "get_runtime", lambda: runtime)
    monkeypatch.setattr(authority_jobs.dt, "datetime", Clock)
    monkeypatch.setattr(authority_jobs, "PublicationOrchestrator", lambda **kwargs: orchestrator)
    monkeypatch.setattr(
        authority_jobs.EventAuthorityService, "from_runtime", lambda r, c: authority_service
    )
    monkeypatch.setattr(
        authority_jobs.process_publication_authority_batch, "configure", configure_mock
    )
    defer_reconcile = AsyncMock()
    monkeypatch.setattr(authority_jobs, "_defer_publication_reconcile", defer_reconcile)

    await authority_jobs.process_publication_authority_batch(intent_id=102, shard_id=0)

    # Continuation MUST NOT be enqueued because no actionable items remain
    assert not configure_mock.called
    defer_reconcile.assert_awaited_once_with(102)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_authority_gap_targets_only_actionable_sql(monkeypatch):
    from src.publication.repository import PublicationRepository

    repo = PublicationRepository()
    monkeypatch.setattr(
        repo,
        "_load_authority_policy",
        AsyncMock(return_value=(24, (), "v1", "v1", "hash123")),
    )

    executed_queries: list[tuple[str, dict]] = []

    class FakeCursor:
        async def fetchall(self):
            return [(10, 100)]

    class FakeConn:
        async def execute(self, query, params):
            executed_queries.append((query, params))
            return FakeCursor()

    snapshot = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc)
    eval_now = snapshot + dt.timedelta(minutes=5)

    # 1. only_actionable=False (default: includes all gaps)
    res_all = await repo.find_authority_gap_targets(
        FakeConn(),
        edition_id=1,
        snapshot_at=snapshot,
        eligibility_policy_id=42,
        only_actionable=False,
    )
    assert len(res_all) == 1
    query_all, params_all = executed_queries[0]
    assert "story_event_processing_retries" not in query_all
    assert "now" not in params_all

    # 2. only_actionable=True (filters out cooling down retries and exhausted)
    res_actionable = await repo.find_authority_gap_targets(
        FakeConn(),
        edition_id=1,
        snapshot_at=snapshot,
        eligibility_policy_id=42,
        only_actionable=True,
        now=eval_now,
    )
    assert len(res_actionable) == 1
    query_actionable, params_actionable = executed_queries[1]
    assert "story_event_processing_retries retry" in query_actionable
    assert "retry.next_retry_at > %(now)s" in query_actionable
    assert params_actionable["now"] == eval_now
