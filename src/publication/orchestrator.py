"""Unified durable orchestration for manual and scheduled publications.

This module owns the boundary between a publication intent and the background
jobs that satisfy it.  It deliberately does not collect, process, select, or
generate anything itself; those operations are deferred as durable jobs.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import psycopg

from src.config_loader import Config
from src.ingestion.models import CollectionTrigger
from src.publication.policies import resolve_publication_lookback_hours
from src.publication.readiness import (
    PublicationReadinessDecision,
    PublicationReadinessService,
)
from src.publication.readiness_repository import (
    PublicationReadinessRepository,
    PublicationRefreshRun,
    PublicationSourceDiagnostic,
)
from src.publication.repository import PublicationRepository
from src.repositories.editions import EditionRepository

logger = logging.getLogger(__name__)

RETRYABLE_COLLECTION_OUTCOMES = frozenset({"transient", "rate_limited"})
TERMINAL_COLLECTION_OUTCOMES = frozenset(
    {
        "auth_required",
        "account_action_required",
        "access_denied",
        "source_not_found",
        "layout_changed",
        "permanent",
    }
)


@dataclass(frozen=True)
class PublicationIntentResult:
    """The durable intent accepted by the orchestrator."""

    intent_id: int
    request_key: str
    edition_slug: str
    publication_type: str
    trigger: Literal["manual", "scheduled"]
    target_at: dt.datetime
    freshness_cutoff_at: dt.datetime
    deadline_at: dt.datetime
    readiness_status: str

    @property
    def run_id(self) -> int:
        """Compatibility alias; this ID is an intent, not a PublicationRun."""
        return self.intent_id


EnqueueSource = Callable[[int, CollectionTrigger, int], Awaitable[int | None]]


def calculate_intent_times(
    target_at: dt.datetime, *, freshness_ttl_minutes: int, deadline_minutes: int
) -> tuple[dt.datetime, dt.datetime]:
    """Return the freshness cutoff and readiness deadline for an intent."""
    target_at = _utc(target_at)
    return (
        target_at - dt.timedelta(minutes=freshness_ttl_minutes),
        target_at + dt.timedelta(minutes=deadline_minutes),
    )


class PublicationOrchestrator:
    """Coordinate durable publication intents and their targeted background work."""

    def __init__(
        self,
        *,
        uow,
        config: Config,
        readiness: PublicationReadinessService | None = None,
        readiness_repo: PublicationReadinessRepository | None = None,
        enqueue_source: EnqueueSource | None = None,
        pre_publish_priority: int = 100,
    ) -> None:
        self.uow = uow
        self.config = config
        self.readiness = readiness or PublicationReadinessService(
            authority_gap_checker=self._find_authority_gap_story_ids,
            authority_barrier_checker=self._get_authority_barrier_state,
        )
        self.readiness_repo = readiness_repo or self.readiness.repo
        self.enqueue_source = enqueue_source or self._default_enqueue_source
        self.pre_publish_priority = pre_publish_priority

    async def request(
        self,
        *,
        edition_slug: str,
        publication_type: str,
        trigger: Literal["manual", "scheduled"],
        target_at: dt.datetime,
        requested_by_user_id: int | None = None,
        request_key: str | None = None,
        lookback_hours: int | None = None,
        now: dt.datetime | None = None,
    ) -> PublicationIntentResult:
        """Create/get and immediately reconcile one publication intent."""
        target_at = _utc(target_at)
        now = _utc(now or dt.datetime.now(dt.timezone.utc))
        key = request_key or f"{trigger}:{edition_slug}:{publication_type}:{uuid.uuid4().hex}"
        freshness_cutoff_at, deadline_at = calculate_intent_times(
            target_at,
            freshness_ttl_minutes=self.config.settings.publication_freshness_ttl_minutes,
            deadline_minutes=self.config.settings.publication_readiness_deadline_minutes,
        )
        effective_lookback_hours = resolve_publication_lookback_hours(
            publication_type, self.config, override=lookback_hours
        )
        if effective_lookback_hours <= 0:
            raise ValueError("lookback_hours must be positive")

        async with self.uow.transaction() as conn:
            edition = await EditionRepository().get_by_slug(conn, edition_slug)
            if edition is None:
                raise ValueError(f"edition slug {edition_slug!r} not found")
            source_ids = await EditionRepository().list_enabled_source_ids(conn, edition.id)
            intent = await self.readiness.create_refresh(
                conn,
                edition_id=edition.id,
                publication_type=publication_type,
                slot_at=target_at,
                source_ids=source_ids,
                requested_at=now,
                trigger=trigger,
                request_key=key,
                freshness_cutoff_at=freshness_cutoff_at,
                requested_by_user_id=requested_by_user_id,
                lookback_hours=effective_lookback_hours,
                deadline_minutes=self.config.settings.publication_readiness_deadline_minutes,
            )
            if intent.status in {"preparing", "publication_queued", "failed"}:
                # The scheduler may submit the same stable request key on
                # every catch-up tick. A handed-off or terminal intent is
                # already decided and must remain an idempotent no-op.
                decision = None
                source_ids_to_enqueue: list[int] = []
                edition_slug_result = edition.slug
            else:
                decision = await self.readiness.reconcile(conn, intent.id, now=now)
                decision, source_ids_to_enqueue = await self._prepare_decision(
                    conn, intent, decision, now=now
                )
                edition_slug_result = edition.slug

        await self._enqueue_sources(source_ids_to_enqueue)
        logger.info(
            "publication_intent_requested",
            extra={
                "intent_id": intent.id,
                "trigger": intent.trigger,
                "publication_type": intent.publication_type,
                "target_at": intent.slot_at.isoformat(),
                "freshness_cutoff_at": freshness_cutoff_at.isoformat(),
                "deadline_at": deadline_at.isoformat(),
                "retry_source_count": len(source_ids_to_enqueue),
                "status": decision.status if decision is not None else intent.status,
            },
        )
        return self._result(
            intent,
            edition_slug=edition_slug_result,
            decision=decision,
            freshness_cutoff_at=intent.freshness_cutoff_at or freshness_cutoff_at,
            deadline_at=intent.deadline_at,
        )

    async def reconcile(
        self, intent_id: int, *, now: dt.datetime | None = None
    ) -> PublicationReadinessDecision:
        """Reconcile one open intent and defer only work still needed."""
        now = _utc(now or dt.datetime.now(dt.timezone.utc))
        async with self.uow.transaction() as conn:
            intent = await self.readiness_repo.get_refresh_run(conn, intent_id, for_update=True)
            if intent is None:
                raise ValueError(f"publication intent {intent_id} not found")
            decision = await self.readiness.reconcile(conn, intent.id, now=now)
            decision, source_ids_to_enqueue = await self._prepare_decision(
                conn, intent, decision, now=now
            )
        await self._enqueue_sources(source_ids_to_enqueue)
        logger.info(
            "publication_intent_reconciled",
            extra={
                "intent_id": intent_id,
                "status": decision.status,
                "retry_source_count": len(source_ids_to_enqueue),
                "target_at": intent.slot_at.isoformat(),
                "freshness_cutoff_at": (
                    intent.freshness_cutoff_at.isoformat()
                    if intent.freshness_cutoff_at is not None
                    else None
                ),
                "deadline_at": intent.deadline_at.isoformat(),
            },
        )
        return decision

    async def _prepare_decision(
        self,
        conn: psycopg.AsyncConnection,
        intent: PublicationRefreshRun,
        decision: PublicationReadinessDecision,
        *,
        now: dt.datetime,
    ) -> tuple[PublicationReadinessDecision, list[int]]:
        """Apply retry/claim policy while the intent transaction is held."""
        if decision.status == "failed":
            await self._enqueue_failure_notification(conn, intent, decision.failure_kind)
            return decision, []

        if decision.authority_gap_story_ids:
            await self._defer_event_processing(
                conn,
                intent_id=intent.id,
                edition_id=intent.edition_id,
                story_ids=decision.authority_gap_story_ids,
            )

        if decision.status == "ready_for_preparation":
            claimed = await self.readiness_repo.mark_preparing(conn, refresh_run_id=intent.id)
            if claimed is not None:
                await self._defer_preparation(conn, intent.id)
            return decision, []

        diagnostics = await self.readiness_repo.list_unready_source_diagnostics(conn, intent.id)
        if intent.status not in {"collecting", "processing"} and not diagnostics:
            return decision, []

        if _rate_limit_exceeds_deadline(intent, diagnostics):
            await self.readiness_repo.transition_refresh(
                conn,
                intent.id,
                status="failed",
                error_kind="rate_limit_deadline",
            )
            failed = PublicationReadinessDecision("failed", None, "rate_limit_deadline")
            await self._enqueue_failure_notification(conn, intent, failed.failure_kind)
            return failed, []

        source_ids = [
            diagnostic.source_id
            for diagnostic in diagnostics
            if diagnostic.stage == "collection"
            if _source_retry_allowed(diagnostic, now=now, deadline_at=intent.deadline_at)
        ]
        for source_id in source_ids:
            await self.readiness_repo.mark_source_enqueue_attempt(
                conn,
                refresh_run_id=intent.id,
                source_id=source_id,
                attempted_at=now,
            )

        return decision, source_ids

    async def _find_authority_gap_story_ids(
        self,
        conn: psycopg.AsyncConnection,
        intent: PublicationRefreshRun,
        now: dt.datetime,
    ) -> Sequence[int]:
        targets = await self.find_authority_gap_targets(conn, intent, evaluation_at=now)
        return [target.story_id for target in targets]

    async def find_authority_gap_targets(
        self,
        conn: psycopg.AsyncConnection,
        intent: PublicationRefreshRun,
        *,
        evaluation_at: dt.datetime,
    ):
        from src.publication.policies import PublicationPolicyService

        policy_set = await PublicationPolicyService().ensure_current(
            conn,
            edition_id=intent.edition_id,
            publication_type=intent.publication_type,
            config=self.config,
            lookback_hours_override=intent.lookback_hours,
        )
        return await PublicationRepository().find_authority_gap_targets(
            conn,
            edition_id=intent.edition_id,
            source_cutoff_at=intent.normal_source_cutoff_at,
            snapshot_at=evaluation_at,
            eligibility_policy_id=policy_set.eligibility_policy_id,
        )

    async def _get_authority_barrier_state(
        self,
        conn: psycopg.AsyncConnection,
        intent: PublicationRefreshRun,
        evaluation_at: dt.datetime,
        observed_at: dt.datetime,
    ):
        from src.publication.policies import PublicationPolicyService
        from src.repositories.event_authority import EventAuthorityRepository

        policy_set = await PublicationPolicyService().ensure_current(
            conn,
            edition_id=intent.edition_id,
            publication_type=intent.publication_type,
            config=self.config,
            lookback_hours_override=intent.lookback_hours,
        )
        publication_repo = PublicationRepository()
        required = await publication_repo.list_required_authority_targets(
            conn,
            edition_id=intent.edition_id,
            source_cutoff_at=intent.normal_source_cutoff_at,
            snapshot_at=evaluation_at,
            eligibility_policy_id=policy_set.eligibility_policy_id,
        )
        gaps = await publication_repo.find_authority_gap_targets(
            conn,
            edition_id=intent.edition_id,
            source_cutoff_at=intent.normal_source_cutoff_at,
            snapshot_at=evaluation_at,
            eligibility_policy_id=policy_set.eligibility_policy_id,
        )
        return await EventAuthorityRepository().get_publication_barrier_state(
            conn,
            required_targets=required,
            gap_targets=gaps,
            evaluation_at=evaluation_at,
            deadline_at=intent.deadline_at,
            observed_at=observed_at,
            publication_repo=publication_repo,
        )

    async def _defer_event_processing(
        self,
        conn: psycopg.AsyncConnection,
        *,
        intent_id: int,
        edition_id: int,
        story_ids: Sequence[int],
    ) -> None:
        if not story_ids:
            return
        from procrastinate.exceptions import AlreadyEnqueued

        from src.jobs.event_authority import (
            PUBLICATION_AUTHORITY_PRIORITY,
            process_publication_authority_gap,
        )

        try:
            await process_publication_authority_gap.configure(
                connection=conn,
                priority=PUBLICATION_AUTHORITY_PRIORITY,
                queueing_lock=f"publication-authority:{intent_id}",
            ).defer_async(intent_id=intent_id)
        except AlreadyEnqueued:
            logger.debug(
                "publication authority already queued for authority gap",
                extra={"edition_id": edition_id, "story_count": len(story_ids)},
            )

    async def _enqueue_failure_notification(
        self,
        conn: psycopg.AsyncConnection,
        intent: PublicationRefreshRun,
        failure_kind: str | None,
    ) -> None:
        from src.publication.notifications import PublicationFailureNotificationService

        current = await self.readiness_repo.get_refresh_run(conn, intent.id)
        if current is None:
            return
        await PublicationFailureNotificationService(
            config=self.config,
            readiness_repo=self.readiness_repo,
        ).enqueue_for_failed_intent(
            conn,
            intent=current,
            failure_kind=failure_kind,
        )

    async def _enqueue_sources(self, source_ids: Sequence[int]) -> None:
        for source_id in source_ids:
            await self.enqueue_source(
                source_id, CollectionTrigger.PRE_PUBLISH, self.pre_publish_priority
            )

    async def _defer_preparation(self, conn: psycopg.AsyncConnection, intent_id: int) -> None:
        from src.jobs.publication import prepare_publication_from_intent

        await prepare_publication_from_intent.configure(
            connection=conn,
            queueing_lock=f"publication-intent:{intent_id}",
        ).defer_async(intent_id=intent_id)

    @staticmethod
    async def _default_enqueue_source(
        source_id: int, trigger: CollectionTrigger, priority: int
    ) -> int | None:
        from src.jobs.ingestion import enqueue_source_scan

        return await enqueue_source_scan(source_id, trigger, priority)

    @staticmethod
    def _result(
        intent: PublicationRefreshRun,
        *,
        edition_slug: str,
        decision: PublicationReadinessDecision | None,
        freshness_cutoff_at: dt.datetime,
        deadline_at: dt.datetime,
    ) -> PublicationIntentResult:
        return PublicationIntentResult(
            intent_id=intent.id,
            request_key=intent.request_key,
            edition_slug=edition_slug,
            publication_type=intent.publication_type,
            trigger=intent.trigger,  # type: ignore[arg-type]
            target_at=intent.slot_at,
            freshness_cutoff_at=freshness_cutoff_at,
            deadline_at=deadline_at,
            readiness_status=decision.status if decision is not None else intent.status,
        )


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _source_retry_allowed(
    diagnostic: PublicationSourceDiagnostic,
    *,
    now: dt.datetime,
    deadline_at: dt.datetime,
) -> bool:
    if not diagnostic.retryable:
        return False
    if diagnostic.backoff_until is not None and _utc(diagnostic.backoff_until) > now:
        return False
    return now < deadline_at


def _rate_limit_exceeds_deadline(
    intent: PublicationRefreshRun, diagnostics: Sequence[PublicationSourceDiagnostic]
) -> bool:
    return any(
        diagnostic.collection_outcome == "rate_limited"
        and diagnostic.backoff_until is not None
        and _utc(diagnostic.backoff_until) >= intent.deadline_at
        for diagnostic in diagnostics
    )
