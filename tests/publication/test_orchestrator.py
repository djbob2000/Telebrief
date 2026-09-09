"""Focused tests for unified publication-intent orchestration."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.ingestion.models import CollectionTrigger
from src.publication.orchestrator import (
    PublicationOrchestrator,
    _source_retry_allowed,
    calculate_intent_times,
)
from src.publication.readiness import PublicationReadinessDecision
from src.publication.readiness_repository import (
    PublicationRefreshRun,
    PublicationSourceDiagnostic,
)

UTC = dt.timezone.utc
TARGET = dt.datetime(2026, 9, 9, 9, 0, tzinfo=UTC)


def _intent(*, status: str = "collecting") -> PublicationRefreshRun:
    return PublicationRefreshRun(
        id=7,
        edition_id=1,
        publication_type="digest_grouped",
        slot_at=TARGET,
        requested_at=TARGET - dt.timedelta(minutes=30),
        normal_source_cutoff_at=TARGET,
        fallback_snapshot_at=TARGET - dt.timedelta(minutes=30),
        deadline_at=TARGET + dt.timedelta(minutes=20),
        status=status,
        collection_ready_at=None,
        processing_ready_at=None,
        prepared_at=None,
        publication_run_id=None,
        fallback_used=False,
        error_kind=None,
        metadata={},
        trigger="manual",
        request_key="manual:test",
        freshness_cutoff_at=TARGET - dt.timedelta(minutes=30),
        requested_by_user_id=123,
    )


class FakeUow:
    def transaction(self):
        return self

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *args):
        return False


class FakeRepo:
    def __init__(self, intent, diagnostics):
        self.intent = intent
        self.diagnostics = diagnostics
        self.attempts = []
        self.preparing = 0

    async def get_refresh_run(self, conn, intent_id, *, for_update=False):
        return self.intent

    async def list_unready_source_diagnostics(self, conn, intent_id):
        return self.diagnostics

    async def mark_source_enqueue_attempt(self, conn, **kwargs):
        self.attempts.append(kwargs)

    async def mark_preparing(self, conn, *, refresh_run_id):
        if self.intent.status != "ready_for_preparation":
            return None
        self.preparing += 1
        self.intent = replace(self.intent, status="preparing")
        return self.intent

    async def transition_refresh(self, conn, refresh_run_id, **kwargs):
        self.intent = replace(
            self.intent,
            status=kwargs["status"],
            error_kind=kwargs.get("error_kind"),
        )


class FakeReadiness:
    def __init__(self, repo, decision):
        self.repo = repo
        self.decision = decision

    async def reconcile(self, conn, intent_id, *, now):
        return self.decision


def _diagnostic(source_id: int, outcome: str | None, backoff_until=None):
    return PublicationSourceDiagnostic(
        source_id=source_id,
        status="pending",
        collection_outcome=outcome,
        collection_run_id=None,
        backoff_until=backoff_until,
        retryable=outcome in {None, "transient", "rate_limited"},
    )


def test_intent_timing_uses_target_for_cutoff_and_deadline():
    cutoff, deadline = calculate_intent_times(TARGET, freshness_ttl_minutes=30, deadline_minutes=20)
    assert cutoff == TARGET - dt.timedelta(minutes=30)
    assert deadline == TARGET + dt.timedelta(minutes=20)


def test_retry_policy_waits_for_rate_limit_backoff_and_deadline():
    diagnostic = _diagnostic(
        2,
        "rate_limited",
        TARGET + dt.timedelta(minutes=5),
    )
    assert not _source_retry_allowed(
        diagnostic, now=TARGET, deadline_at=TARGET + dt.timedelta(minutes=20)
    )
    assert _source_retry_allowed(
        diagnostic,
        now=TARGET + dt.timedelta(minutes=6),
        deadline_at=TARGET + dt.timedelta(minutes=20),
    )
    assert not _source_retry_allowed(
        diagnostic,
        now=TARGET + dt.timedelta(minutes=20),
        deadline_at=TARGET + dt.timedelta(minutes=20),
    )


@pytest.mark.asyncio
async def test_reconcile_enqueues_only_unresolved_retryable_sources():
    repo = FakeRepo(
        _intent(),
        [
            _diagnostic(1, "transient"),
            _diagnostic(2, "rate_limited", TARGET - dt.timedelta(minutes=1)),
            _diagnostic(3, "auth_required"),
        ],
    )
    queued = []

    async def enqueue(source_id, trigger, priority):
        queued.append((source_id, trigger, priority))
        return source_id

    readiness = FakeReadiness(repo, PublicationReadinessDecision("collecting", None))
    orchestrator = PublicationOrchestrator(
        uow=FakeUow(),
        config=SimpleNamespace(settings=SimpleNamespace()),
        readiness=readiness,
        readiness_repo=repo,
        enqueue_source=enqueue,
    )
    decision = await orchestrator.reconcile(7, now=TARGET)

    assert decision.status == "collecting"
    assert [source_id for source_id, _, _ in queued] == [1, 2]
    assert all(trigger == CollectionTrigger.PRE_PUBLISH for _, trigger, _ in queued)
    assert [item["source_id"] for item in repo.attempts] == [1, 2]


@pytest.mark.asyncio
async def test_ready_decision_claims_and_defers_preparation_once():
    repo = FakeRepo(_intent(status="ready_for_preparation"), [])
    deferred = []
    readiness = FakeReadiness(repo, PublicationReadinessDecision("ready_for_preparation", TARGET))
    orchestrator = PublicationOrchestrator(
        uow=FakeUow(),
        config=SimpleNamespace(settings=SimpleNamespace()),
        readiness=readiness,
        readiness_repo=repo,
    )

    async def defer(conn, intent_id):
        deferred.append(intent_id)

    orchestrator._defer_preparation = defer
    first = await orchestrator.reconcile(7, now=TARGET)
    second = await orchestrator.reconcile(7, now=TARGET)

    assert first.status == second.status == "ready_for_preparation"
    assert repo.preparing == 1
    assert deferred == [7]
