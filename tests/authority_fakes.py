"""Fake collaborators for driving EventAuthorityService without a database.

The fakes keep the same method signatures as the real repositories and record what
was written, so tests can check the Gate queue, retry and decision state.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.domain.event_authority import AuthorityTarget
from src.processing.event_authority import EventAuthorityService
from src.processing.event_triage import StoryGateBatchResult

BACKOFF_SECONDS = 300
EDITION_ID = 5
STORY_ID = 1
ASSIGNMENT_ID = 10
KEY = (STORY_ID, ASSIGNMENT_ID)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def scope_patch():
    """Patch the edition-scope lookup, which needs a live connection."""
    return patch(
        "src.processing.event_authority.resolve_edition_scope",
        AsyncMock(return_value=("slug", {})),
    )


class Tx:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc: object) -> bool:
        return False


class Uow:
    def transaction(self) -> Tx:
        return Tx()


class Claims:
    async def acquire_cycle(self, conn, **kwargs):
        return object()

    async def release_cycle(self, conn, cycle):
        return None

    async def try_claim_stage(self, conn, **kwargs):
        return SimpleNamespace(story_id=kwargs["story_id"])

    async def release_stage(self, conn, claim):
        return None

    async def renew_stage(self, conn, claim, **kwargs):
        return None


class Retries:
    def __init__(self, states: dict | None = None) -> None:
        self.states = dict(states or {})
        self.failures: list[dict] = []
        self.cleared: list[dict] = []

    async def get_for_assignments(self, conn, keys, *, stage):
        return {key: self.states[key] for key in keys if key in self.states}

    async def record_failure(self, conn, **kwargs):
        self.failures.append(kwargs)

    async def clear(self, conn, **kwargs):
        self.cleared.append(kwargs)


class Cluster:
    async def get_cluster_state(self, conn, story_id):
        return SimpleNamespace(fragment_count=1)

    async def mark_cluster_processed_without_analysis(self, conn, **kwargs):
        return None


class Authority:
    async def filter_unsatisfied_targets(self, conn, *, targets, snapshot_at):
        return list(targets)


class Brief:
    async def persist_brief(self, conn, **kwargs):
        return None


class Triage:
    """Replays scripted Gate responses in call order and records each call."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.story_counts: list[int] = []
        self.max_output_tokens: list[int | None] = []

    async def triage_stories_batch(self, _conn, states, **kwargs):
        self.story_counts.append(len(states))
        self.max_output_tokens.append(kwargs.get("max_output_tokens"))
        return self.responses.pop(0)


def config_namespace(*, singleton_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        triage_batch_size=10,
        triage_excerpt_chars=800,
        triage_max_input_chars=48_000,
        triage_min_ignore_confidence=0.95,
        triage_max_output_tokens=4_096,
        triage_split_max_extra_calls_per_cycle=8,
        triage_singleton_recovery_enabled=singleton_enabled,
        triage_max_attempts_per_assignment=2,
        provider_retry_backoff_seconds=BACKOFF_SECONDS,
        provider_retry_backoff_max_seconds=3600,
        authority_coordination_lease_seconds=60,
        event_processing_stage_lease_seconds=300,
        authority_provider_timeout_seconds=120,
        authority_shard_count=1,
        background_authority_enabled=True,
    )


def build_service(
    *,
    singleton_enabled: bool,
    responses: list,
    retries: Retries,
    triage: Triage | None = None,
) -> tuple[EventAuthorityService, Triage]:
    cfg = config_namespace(singleton_enabled=singleton_enabled)
    triage_service = triage or Triage(responses)
    service = EventAuthorityService(
        SimpleNamespace(uow=Uow()),
        SimpleNamespace(settings=SimpleNamespace(event_pipeline=cfg)),
        cluster_repo=Cluster(),
        authority_repo=Authority(),
        retry_repo=retries,
        claim_repo=Claims(),
        triage_service=triage_service,
        brief_service=Brief(),
    )
    return service, triage_service


def target(story_id: int = STORY_ID, assignment_id: int = ASSIGNMENT_ID) -> AuthorityTarget:
    return AuthorityTarget(
        story_id=story_id,
        assignment_id=assignment_id,
        edition_id=EDITION_ID,
        triage_version="t",
        scope_version="s",
        scope_config_hash="h",
    )


def invalid_batch(story_id: int = STORY_ID) -> StoryGateBatchResult:
    return StoryGateBatchResult(
        results=(),
        deferred_story_ids=(story_id,),
        invalid_story_ids=(story_id,),
        invalid_reasons={story_id: "drop_without_valid_brief"},
    )


def keep_gate(story_id: int = STORY_ID) -> SimpleNamespace:
    return SimpleNamespace(
        story_id=story_id,
        scope="LOCAL",
        retention="KEEP",
        enrichment="PASS",
        brief_payload=None,
    )


def keep_batch(story_id: int = STORY_ID) -> StoryGateBatchResult:
    return StoryGateBatchResult(results=(keep_gate(story_id),), deferred_story_ids=())


def retry_state(**fields) -> SimpleNamespace:
    return SimpleNamespace(**fields)
