# ruff: noqa: S101
"""A story waiting on an authority retry does not hold publication readiness.

Readiness is decided from collection and revision processing only. A story
still waiting on Gate is left out of the sealed candidates, not waited for.
This is a service-level test with a fake repository, not a database test.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from src.publication.readiness import PublicationReadinessService
from src.publication.readiness_repository import RevisionBarrierState

SLOT = dt.datetime(2026, 11, 22, 18, 0, tzinfo=dt.timezone.utc)
DEADLINE = SLOT + dt.timedelta(minutes=90)
RETRY_WAITING_STORY = 1


class _Repo:
    def __init__(self) -> None:
        self.refresh = SimpleNamespace(
            id=7,
            edition_id=1,
            publication_type="daily_digest",
            slot_at=SLOT,
            requested_at=SLOT - dt.timedelta(hours=1),
            normal_source_cutoff_at=SLOT,
            fallback_snapshot_at=SLOT,
            deadline_at=DEADLINE,
            status="collecting",
            collection_ready_at=None,
            processing_ready_at=None,
            prepared_at=None,
            publication_run_id=None,
            fallback_used=False,
            error_kind=None,
            metadata={},
            trigger="scheduled",
            request_key="",
            freshness_cutoff_at=None,
            requested_by_user_id=None,
            lookback_hours=24,
            knowledge_snapshot_at=None,
        )
        self.transitions: list[dict] = []

    async def get_refresh_run(self, conn, refresh_run_id):
        return self.refresh

    async def reconcile_qualifying_collection_runs(self, conn, refresh_run_id, *, evaluation_at):
        return [
            SimpleNamespace(
                status="succeeded",
                collection_outcome="success",
                completed_at=SLOT - dt.timedelta(minutes=5),
            )
        ]

    async def get_revision_barrier_state(
        self, conn, refresh_run_id, *, evaluation_at, window_start, source_cutoff_at
    ):
        # Every revision is processed; the Gate retry only affects authority.
        return RevisionBarrierState(
            unprocessed_count=0, completed_at=SLOT - dt.timedelta(minutes=2)
        )

    async def freeze_knowledge_snapshot(self, conn, *, refresh_run_id, snapshot_at):
        return None

    async def transition_refresh(self, conn, refresh_run_id, **fields):
        self.transitions.append(fields)
        self.refresh.status = fields["status"]


async def _retry_waiting_gap(conn, refresh, now):
    # Authority reports a story whose Gate retry is still deferred.
    return [RETRY_WAITING_STORY]


def _service(repo: _Repo) -> PublicationReadinessService:
    return PublicationReadinessService(repo, authority_gap_checker=_retry_waiting_gap)


def _failed_transitions(repo: _Repo) -> list[dict]:
    return [fields for fields in repo.transitions if fields.get("status") == "failed"]


@pytest.mark.asyncio
async def test_retry_waiting_story_does_not_hold_readiness_at_the_slot():
    repo = _Repo()

    decision = await _service(repo).reconcile(None, 7, now=SLOT + dt.timedelta(minutes=1))

    assert decision.status == "ready_for_preparation"
    assert decision.authority_gap_story_ids == ()
    assert _failed_transitions(repo) == []


@pytest.mark.asyncio
async def test_retry_waiting_story_does_not_fail_readiness_past_the_deadline():
    repo = _Repo()

    decision = await _service(repo).reconcile(None, 7, now=DEADLINE + dt.timedelta(minutes=1))

    assert decision.status == "ready_for_preparation"
    assert decision.failure_kind is None
    assert _failed_transitions(repo) == []
