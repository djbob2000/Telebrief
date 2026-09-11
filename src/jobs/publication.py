"""Procrastinate publication background jobs (Plan 4)."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import procrastinate

from src.jobs.app import procrastinate_app
from src.runtime import get_runtime

logger = logging.getLogger(__name__)

SELECT_STORIES_TASK_NAME = "select_stories_for_publication"
GENERATE_PUBLICATION_TASK_NAME = "generate_publication"
PREPARE_DELIVERY_PAYLOADS_TASK_NAME = "prepare_delivery_payloads"
DELIVER_PAYLOAD_TASK_NAME = "deliver_publication_payload"
PREPARE_PUBLICATION_FROM_INTENT_TASK_NAME = "prepare_publication_from_intent"

PUBLICATION_QUEUE = "publication"

PUBLICATION_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=3,
    wait=30,
    linear_wait=60,
)


class PreparationContractChangedError(RuntimeError):
    """Frozen readiness evidence no longer matches preparation-time checks."""

    error_kind = "preparation_contract_changed"


@procrastinate_app.task(
    name=SELECT_STORIES_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    lock="select_stories_for_publication:{run_id}",
    pass_context=True,
)
async def select_stories_for_publication(context: Any, run_id: int) -> None:
    """Select stories from sealed candidates."""
    from src.publication.selection import EditorialSelectionService

    runtime = get_runtime()
    service = EditorialSelectionService(uow=runtime.uow)
    await service.select(run_id)


@procrastinate_app.task(
    name=GENERATE_PUBLICATION_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    lock="generate_publication:{run_id}",
    pass_context=True,
)
async def generate_publication(context: Any, run_id: int) -> None:
    """Generate publication from sealed inputs."""
    from src.publication.errors import ArticlePublicationRejected
    from src.publication.generation import PublicationGenerationService

    runtime = get_runtime()
    service = PublicationGenerationService(uow=runtime.uow)
    try:
        await service.generate(run_id)
    except ArticlePublicationRejected as exc:
        logger.warning(
            "publication run %s ended with terminal article rejection: %s",
            run_id,
            exc.error_kind,
        )
        return


@procrastinate_app.task(
    name=PREPARE_DELIVERY_PAYLOADS_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    lock="prepare_delivery_payloads:{publication_id}",
    pass_context=True,
)
async def prepare_delivery_payloads(context: Any, publication_id: int) -> None:
    """Prepare immutable delivery payloads for a created publication."""
    from src.publication.delivery import PublicationDeliveryService

    runtime = get_runtime()
    service = PublicationDeliveryService(uow=runtime.uow)
    await service.prepare_payloads(publication_id)


@procrastinate_app.task(
    name=DELIVER_PAYLOAD_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    lock="deliver_publication_payload:{delivery_id}",
    pass_context=True,
)
async def deliver_publication_payload(context: Any, delivery_id: int) -> None:
    """Deliver a prepared publication payload."""
    from src.publication.delivery import PublicationDeliveryService

    runtime = get_runtime()
    service = PublicationDeliveryService(uow=runtime.uow)
    await service.deliver(delivery_id)


@procrastinate_app.task(
    name=PREPARE_PUBLICATION_FROM_INTENT_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    pass_context=True,
)
async def prepare_publication_from_intent(context: Any, intent_id: int) -> None:
    """Create a PublicationRun only after a durable intent is ready."""
    try:
        await _prepare_publication_from_intent_once(intent_id)
    except Exception as exc:
        if _is_final_publication_attempt(context):
            await _mark_preparation_failed(
                intent_id,
                error_kind=getattr(exc, "error_kind", None),
            )
        raise


async def _prepare_publication_from_intent_once(
    intent_id: int, *, defer_selection: bool = True
) -> int | None:
    """Create a PublicationRun only after a durable intent is ready."""
    from src.config_loader import load_config
    from src.publication.policies import PublicationPolicyService
    from src.publication.readiness_repository import PublicationReadinessRepository
    from src.publication.snapshot import PublicationSnapshotService
    from src.repositories.editions import EditionRepository

    runtime = get_runtime()
    config = load_config()

    readiness_repo = PublicationReadinessRepository()
    service = PublicationSnapshotService(uow=runtime.uow)

    async with runtime.uow.transaction() as conn:
        refresh = await readiness_repo.get_refresh_run(conn, intent_id, for_update=True)
        if refresh is None:
            raise ValueError(f"publication intent {intent_id} not found")
        if refresh.status == "publication_queued":
            return refresh.publication_run_id
        if refresh.status != "preparing":
            raise ValueError(f"publication intent {intent_id} is not preparing: {refresh.status}")
        edition = await EditionRepository().get_by_id(conn, refresh.edition_id)
        if edition is None:
            raise ValueError(f"edition {refresh.edition_id} not found")
        policy_set = await PublicationPolicyService().ensure_current(
            conn,
            edition_id=refresh.edition_id,
            publication_type=refresh.publication_type,
            config=config,
            lookback_hours_override=refresh.lookback_hours,
        )

    source_cutoff_at = refresh.normal_source_cutoff_at
    slot_key = refresh.slot_at.astimezone(dt.timezone.utc).isoformat()
    req_key = f"publication-intent:{refresh.request_key}"
    run_metadata = {
        "refresh_run_id": refresh.id,
        "scheduled_slot_at": slot_key,
        "intent_request_key": refresh.request_key,
        "trigger": refresh.trigger,
    }

    async with runtime.uow.transaction() as conn:
        current_refresh = await readiness_repo.get_refresh_run(conn, refresh.id, for_update=True)
        if current_refresh is None:
            raise ValueError(f"refresh run {refresh.id} not found")
        if current_refresh.status == "publication_queued":
            return current_refresh.publication_run_id
        if current_refresh.status != "preparing":
            raise ValueError(f"refresh run {refresh.id} is not preparing")
        legacy_refresh = not hasattr(current_refresh, "knowledge_snapshot_at")
        if legacy_refresh:
            knowledge_snapshot_at = current_refresh.normal_source_cutoff_at
        elif current_refresh.knowledge_snapshot_at is None:
            raise ValueError(
                f"refresh run {current_refresh.id} is preparing without knowledge_snapshot_at"
            )
        else:
            knowledge_snapshot_at = current_refresh.knowledge_snapshot_at
        if legacy_refresh:
            gap_count = len(
                await service.repo.find_authority_gap_story_ids(
                    conn,
                    edition_id=current_refresh.edition_id,
                    source_cutoff_at=current_refresh.normal_source_cutoff_at,
                    snapshot_at=knowledge_snapshot_at,
                    eligibility_policy_id=policy_set.eligibility_policy_id,
                )
            )
        else:
            gap_count = len(
                await service.repo.find_authority_gap_targets(
                    conn,
                    edition_id=current_refresh.edition_id,
                    source_cutoff_at=current_refresh.normal_source_cutoff_at,
                    snapshot_at=knowledge_snapshot_at,
                    eligibility_policy_id=policy_set.eligibility_policy_id,
                )
            )
        if gap_count:
            if legacy_refresh:
                await readiness_repo.transition_refresh(
                    conn, current_refresh.id, status="processing"
                )
                return None
            raise PreparationContractChangedError(
                f"refresh run {current_refresh.id} has {gap_count} authority gaps "
                f"at frozen snapshot {knowledge_snapshot_at.isoformat()}"
            )
        run = await service.create_run(
            edition_id=current_refresh.edition_id,
            publication_type=current_refresh.publication_type,
            source_cutoff_at=source_cutoff_at,
            snapshot_at=knowledge_snapshot_at,
            request_key=req_key,
            policy_ids=policy_set,
            config=config,
            metadata=run_metadata,
            conn=conn,
        )
        await service.seal_candidates(run.id, conn=conn)
        prepared_at = dt.datetime.now(dt.timezone.utc)
        await readiness_repo.mark_publication_queued(
            conn,
            refresh_run_id=refresh.id,
            publication_run_id=run.id,
            prepared_at=prepared_at,
        )
        logger.info(
            "publication_refresh_prepared",
            extra={
                "edition_id": refresh.edition_id,
                "refresh_run_id": refresh.id,
                "scheduled_slot_at": slot_key,
                "source_cutoff_at": source_cutoff_at.isoformat(),
                "snapshot_at": knowledge_snapshot_at.isoformat(),
                "publication_delay_seconds": max(
                    0, (knowledge_snapshot_at - refresh.slot_at).total_seconds()
                ),
            },
        )
        if defer_selection:
            await select_stories_for_publication.configure(connection=conn).defer_async(
                run_id=run.id
            )
        return run.id


def _is_final_publication_attempt(context: Any) -> bool:
    """Match Procrastinate's retry boundary for the current task attempt."""
    attempts = getattr(getattr(context, "job", None), "attempts", None)
    maximum = PUBLICATION_RETRY_STRATEGY.max_attempts
    return isinstance(attempts, int) and maximum is not None and attempts >= maximum


async def _mark_preparation_failed(intent_id: int, *, error_kind: str | None = None) -> None:
    """Atomically close a preparation intent and persist notification outbox rows."""
    from src.config_loader import load_config
    from src.publication.notifications import PublicationFailureNotificationService
    from src.publication.readiness_repository import PublicationReadinessRepository
    from src.runtime import get_runtime

    runtime = get_runtime()
    readiness_repo = PublicationReadinessRepository()
    notification_ids: list[int] = []
    async with runtime.uow.transaction() as conn:
        refresh = await readiness_repo.get_refresh_run(conn, intent_id, for_update=True)
        if refresh is None or refresh.status != "preparing":
            return
        await readiness_repo.transition_refresh(
            conn,
            intent_id,
            status="failed",
            error_kind=error_kind or "preparation_failed",
        )
        notification_ids = await PublicationFailureNotificationService(
            config=load_config(), readiness_repo=readiness_repo
        ).enqueue_for_failed_intent(
            conn,
            intent=refresh,
            failure_kind=error_kind or "preparation_failed",
            dispatch=False,
        )

    try:
        if notification_ids:
            async with runtime.uow.transaction() as conn:
                refresh = await readiness_repo.get_refresh_run(conn, intent_id)
                if refresh is not None:
                    await PublicationFailureNotificationService(
                        config=load_config(), readiness_repo=readiness_repo
                    ).dispatch_existing(
                        conn,
                        intent=refresh,
                        notification_ids=notification_ids,
                    )
    except Exception:
        logger.exception(
            "failed to dispatch preparation failure notification for intent %s", intent_id
        )


# Kept as an import-level compatibility name for code that only imported the
# old symbol.  It intentionally has the new intent-only signature and cannot
# accept the former edition/snapshot arguments.
create_scheduled_publication = prepare_publication_from_intent
