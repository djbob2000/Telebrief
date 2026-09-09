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
CREATE_SCHEDULED_PUBLICATION_TASK_NAME = "create_scheduled_publication"

PUBLICATION_QUEUE = "publication"

PUBLICATION_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=3,
    wait=30,
    linear_wait=60,
)


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
    name=CREATE_SCHEDULED_PUBLICATION_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    pass_context=True,
)
async def create_scheduled_publication(
    context: Any,
    refresh_run_id: int | None = None,
    edition_slug: str | None = None,
    publication_type: str | None = None,
    snapshot_at: str | None = None,
) -> None:
    """Prepare one durable refresh and atomically queue story selection."""
    from src.config_loader import load_config
    from src.publication.policies import PublicationPolicyService
    from src.publication.readiness_repository import PublicationReadinessRepository
    from src.publication.snapshot import IncompleteTriageError, PublicationSnapshotService
    from src.repositories.editions import EditionRepository

    runtime = get_runtime()
    config = load_config()

    # Keep direct invocations from the pre-readiness API operational while all
    # scheduled dispatcher traffic uses the refresh-id contract below.
    if refresh_run_id is None:
        if edition_slug is None or publication_type is None or snapshot_at is None:
            raise ValueError("scheduled publication requires refresh_run_id")
        from src.publication.snapshot import PublicationSnapshotService
        from src.repositories.editions import EditionRepository

        snap_dt = dt.datetime.fromisoformat(snapshot_at)
        async with runtime.uow.transaction() as conn:
            edition = await EditionRepository().get_by_slug(conn, edition_slug)
            if edition is None:
                raise ValueError(f"edition slug {edition_slug} not found")
        service = PublicationSnapshotService(uow=runtime.uow)
        req_key = f"scheduled:{edition_slug}:{publication_type}:{snap_dt.isoformat()}"
        run = await service.create_run(
            edition_id=edition.id,
            publication_type=publication_type,
            snapshot_at=snap_dt,
            request_key=req_key,
            config=config,
        )
        if getattr(run, "status", "created") != "created":
            return
        await service.drain_authority_gap(run_id=run.id)
        async with runtime.uow.transaction() as conn:
            await service.seal_candidates(run.id, conn=conn)
            await select_stories_for_publication.configure(connection=conn).defer_async(
                run_id=run.id
            )
        return

    readiness_repo = PublicationReadinessRepository()
    service = PublicationSnapshotService(uow=runtime.uow)

    async with runtime.uow.transaction() as conn:
        refresh = await readiness_repo.get_refresh_run(conn, refresh_run_id, for_update=True)
        if refresh is None:
            raise ValueError(f"refresh run {refresh_run_id} not found")
        if refresh.status == "publication_queued":
            return
        if refresh.status in {"ready_for_preparation", "fallback_ready"}:
            refresh = await readiness_repo.mark_preparing(
                conn,
                refresh_run_id=refresh.id,
                fallback_used=refresh.status == "fallback_ready",
            )
            if refresh is None:
                raise RuntimeError(f"refresh run {refresh_run_id} was claimed by another worker")
        if refresh.status != "preparing":
            raise ValueError(f"refresh run {refresh_run_id} is not preparing: {refresh.status}")
        edition = await EditionRepository().get_by_id(conn, refresh.edition_id)
        if edition is None:
            raise ValueError(f"edition {refresh.edition_id} not found")
        policy_set = await PublicationPolicyService().ensure_current(
            conn,
            edition_id=refresh.edition_id,
            publication_type=refresh.publication_type,
            config=config,
        )

    source_cutoff_at = (
        refresh.fallback_snapshot_at if refresh.fallback_used else refresh.normal_source_cutoff_at
    )
    knowledge_snapshot_at: dt.datetime | None = None
    if refresh.fallback_used:
        knowledge_snapshot_at = refresh.fallback_snapshot_at
        remaining = await service.count_authority_gap(
            edition_id=refresh.edition_id,
            source_cutoff_at=source_cutoff_at,
            snapshot_at=knowledge_snapshot_at,
            eligibility_policy_id=policy_set.eligibility_policy_id,
        )
        if remaining:
            raise IncompleteTriageError(
                f"historical fallback has {remaining} unresolved authority-gap stories"
            )
    else:
        remaining = 1
        for _ in range(3):
            candidate_at = dt.datetime.now(dt.timezone.utc)
            await service.drain_authority_gap(
                edition_id=refresh.edition_id,
                source_cutoff_at=source_cutoff_at,
                snapshot_at=candidate_at,
                eligibility_policy_id=policy_set.eligibility_policy_id,
                max_rounds=1,
            )
            knowledge_snapshot_at = dt.datetime.now(dt.timezone.utc)
            remaining = await service.count_authority_gap(
                edition_id=refresh.edition_id,
                source_cutoff_at=source_cutoff_at,
                snapshot_at=knowledge_snapshot_at,
                eligibility_policy_id=policy_set.eligibility_policy_id,
            )
            if remaining == 0:
                break
        if knowledge_snapshot_at is None or remaining != 0:
            raise IncompleteTriageError("authority gap did not converge")

    if knowledge_snapshot_at is None:
        raise IncompleteTriageError("authority gap did not produce a snapshot")
    slot_key = refresh.slot_at.astimezone(dt.timezone.utc).isoformat()
    req_key = f"scheduled:{edition.slug}:{refresh.publication_type}:{slot_key}"
    run_metadata = {
        "refresh_run_id": refresh.id,
        "scheduled_slot_at": slot_key,
        "fallback_used": refresh.fallback_used,
    }

    async with runtime.uow.transaction() as conn:
        current_refresh = await readiness_repo.get_refresh_run(conn, refresh.id, for_update=True)
        if current_refresh is None:
            raise ValueError(f"refresh run {refresh.id} not found")
        if current_refresh.status == "publication_queued":
            return
        if current_refresh.status != "preparing":
            raise ValueError(f"refresh run {refresh.id} is not preparing")
        run = await service.create_run(
            edition_id=refresh.edition_id,
            publication_type=refresh.publication_type,
            source_cutoff_at=source_cutoff_at,
            snapshot_at=knowledge_snapshot_at,
            request_key=req_key,
            policy_ids=policy_set,
            config=config,
            metadata=run_metadata,
            conn=conn,
        )
        await service.seal_candidates(run.id, conn=conn)
        await readiness_repo.mark_publication_queued(
            conn,
            refresh_run_id=refresh.id,
            publication_run_id=run.id,
            prepared_at=knowledge_snapshot_at,
        )
        logger.info(
            "publication_refresh_prepared",
            extra={
                "edition_id": refresh.edition_id,
                "refresh_run_id": refresh.id,
                "scheduled_slot_at": slot_key,
                "source_cutoff_at": source_cutoff_at.isoformat(),
                "snapshot_at": knowledge_snapshot_at.isoformat(),
                "fallback_used": refresh.fallback_used,
                "publication_delay_seconds": max(
                    0, (knowledge_snapshot_at - refresh.slot_at).total_seconds()
                ),
            },
        )
        await select_stories_for_publication.configure(connection=conn).defer_async(run_id=run.id)
