"""Publication authority job wrapper regressions."""

from __future__ import annotations

import logging
from types import SimpleNamespace
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
    stats = AuthorityBatchStats(requested=2, claimed=2, triaged=1, busy=1)
    with caplog.at_level(logging.INFO, logger="src.jobs.event_authority"):
        log_authority_complete(
            mode="publication",
            intent_id=68,
            edition_id=1,
            stats=stats,
            remaining_gap=1,
            duration_ms=42,
        )
    record = caplog.records[-1]
    assert record.intent_id == 68
    assert record.remaining_gap == 1
    assert record.duration_ms == 42


@pytest.mark.unit
def test_contention_error_is_distinct_from_provider_failure():
    assert issubclass(AuthorityCoordinationBusy, RuntimeError)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publication_authority_records_progress_after_provider_batch(monkeypatch):
    started_at = authority_jobs.dt.datetime(2026, 9, 10, 10, 0, tzinfo=authority_jobs.dt.timezone.utc)
    observed_at = started_at + authority_jobs.dt.timedelta(seconds=5)
    deadline_at = started_at + authority_jobs.dt.timedelta(minutes=20)

    class Clock(authority_jobs.dt.datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            del tz
            cls.calls += 1
            return started_at if cls.calls == 1 else observed_at

    intent = SimpleNamespace(status="processing", deadline_at=deadline_at, edition_id=1)
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
            return_value=SimpleNamespace(stats=AuthorityBatchStats(triaged=1), enrichment_targets=())
        )
    )
    runtime = SimpleNamespace(uow=MagicMock(), config=SimpleNamespace())
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

    await authority_jobs.process_publication_authority_gap(68)

    assert orchestrator.find_authority_gap_targets.await_args_list[1].kwargs[
        "evaluation_at"
    ] == observed_at
    diagnostics_repo.update_authority_diagnostics.assert_awaited_once_with(
        runtime.uow.transaction.return_value.__aenter__.return_value,
        refresh_run_id=68,
        observed_at=observed_at,
        gap_count=0,
        block_reason=None,
        terminal_count=0,
    )
    defer_reconcile.assert_awaited_once_with(68)
