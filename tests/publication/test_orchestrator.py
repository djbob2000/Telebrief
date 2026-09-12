"""Focused tests for unified publication-intent orchestration."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from src.domain.event_authority import AuthorityTarget
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authority_gap_uses_assignment_at_source_cutoff_and_current_snapshot(monkeypatch):
    """Readiness must use the publication temporal contract, not current story state."""
    intent = _intent(status="processing")
    orchestrator = PublicationOrchestrator(
        uow=FakeUow(),
        config=SimpleNamespace(settings=SimpleNamespace()),
        readiness=FakeReadiness(
            FakeRepo(intent, []), PublicationReadinessDecision("processing", None)
        ),
    )
    reconciliation_now = TARGET + dt.timedelta(minutes=7)
    policy_calls = []
    gap_calls = []

    async def ensure_current(_service, conn, **kwargs):
        policy_calls.append((conn, kwargs))
        return SimpleNamespace(eligibility_policy_id=73)

    async def find_gap(_repository, conn, **kwargs):
        gap_calls.append((conn, kwargs))
        return [
            AuthorityTarget(
                story_id=9001,
                assignment_id=1201,
                edition_id=intent.edition_id,
                triage_version="v10",
                scope_version="v1",
                scope_config_hash="hash",
            )
        ]

    monkeypatch.setattr(
        "src.publication.policies.PublicationPolicyService.ensure_current",
        ensure_current,
    )
    monkeypatch.setattr(
        "src.publication.repository.PublicationRepository.find_authority_gap_targets",
        find_gap,
    )

    result = await orchestrator._find_authority_gap_story_ids(
        "connection",
        intent,
        reconciliation_now,
    )

    assert result == [9001]
    assert policy_calls[0][1]["edition_id"] == intent.edition_id
    assert policy_calls[0][1]["publication_type"] == intent.publication_type
    assert policy_calls[0][1]["lookback_hours_override"] == intent.lookback_hours
    assert gap_calls == [
        (
            "connection",
            {
                "edition_id": intent.edition_id,
                "source_cutoff_at": intent.normal_source_cutoff_at,
                "snapshot_at": reconciliation_now,
                "eligibility_policy_id": 73,
            },
        )
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_authority_gap_requeues_processing_without_preparing():
    repo = FakeRepo(_intent(status="processing"), [])
    readiness = FakeReadiness(
        repo,
        PublicationReadinessDecision(
            "processing",
            None,
            authority_gap_story_ids=(9001, 9002),
        ),
    )
    orchestrator = PublicationOrchestrator(
        uow=FakeUow(),
        config=SimpleNamespace(settings=SimpleNamespace()),
        readiness=readiness,
        readiness_repo=repo,
    )
    defer_event_processing = AsyncMock()
    orchestrator._defer_event_processing = defer_event_processing

    decision = await orchestrator.reconcile(7, now=TARGET)

    assert decision.status == "processing"
    assert repo.preparing == 0
    defer_event_processing.assert_awaited_once_with(
        ANY,
        intent_id=7,
        edition_id=1,
        story_ids=(9001, 9002),
    )


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publication_authority_barrier_enforced_when_background_triage_disabled(
    monkeypatch,
):
    """Publication barrier must discover untriaged active stories even if background authority is disabled."""
    repo = FakeRepo(_intent(status="processing"), [])
    intent = _intent(status="processing")
    reconciliation_now = TARGET + dt.timedelta(minutes=5)

    config = SimpleNamespace(
        settings=SimpleNamespace(event_pipeline=SimpleNamespace(background_authority_enabled=False))
    )

    triage_service_called = []

    async def ensure_current(_service, conn, **kwargs):
        return SimpleNamespace(eligibility_policy_id=73)

    async def find_gap(_repository, conn, **kwargs):
        # First check finds gap target (active story without triage/revision)
        if not triage_service_called:
            return [
                AuthorityTarget(
                    story_id=9001,
                    assignment_id=1201,
                    edition_id=intent.edition_id,
                    triage_version="v10",
                    scope_version="v1",
                    scope_config_hash="hash",
                )
            ]
        return []

    monkeypatch.setattr(
        "src.publication.policies.PublicationPolicyService.ensure_current",
        ensure_current,
    )
    monkeypatch.setattr(
        "src.publication.repository.PublicationRepository.find_authority_gap_targets",
        find_gap,
    )

    readiness = FakeReadiness(
        repo,
        PublicationReadinessDecision(
            "processing",
            None,
            authority_gap_story_ids=(9001,),
        ),
    )
    orchestrator = PublicationOrchestrator(
        uow=FakeUow(),
        config=config,
        readiness=readiness,
        readiness_repo=repo,
    )

    async def defer_event_processing(conn, *, intent_id, edition_id, story_ids):
        triage_service_called.append((edition_id, story_ids))

    orchestrator._defer_event_processing = defer_event_processing

    decision = await orchestrator.reconcile(7, now=reconciliation_now)

    assert decision.status == "processing"
    assert triage_service_called == [(1, (9001,))]
    assert repo.preparing == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publication_authority_barrier_fails_closed_when_triage_fails():
    """If Gate V2 authority cannot be obtained before deadline, publication fails closed without sealing."""
    # Past deadline with unsatisfied authority gap
    past_deadline_intent = replace(
        _intent(status="processing"), deadline_at=TARGET - dt.timedelta(minutes=1)
    )
    repo = FakeRepo(past_deadline_intent, [])
    readiness = FakeReadiness(
        repo,
        PublicationReadinessDecision(
            "failed",
            None,
            failure_kind="deadline_exceeded",
            authority_gap_story_ids=(9001,),
        ),
    )
    orchestrator = PublicationOrchestrator(
        uow=FakeUow(),
        config=SimpleNamespace(settings=SimpleNamespace()),
        readiness=readiness,
        readiness_repo=repo,
    )
    orchestrator._enqueue_failure_notification = AsyncMock()

    decision = await orchestrator.reconcile(7, now=TARGET)

    assert decision.status == "failed"
    assert decision.status != "selected_inputs_sealed"
    assert repo.preparing == 0
