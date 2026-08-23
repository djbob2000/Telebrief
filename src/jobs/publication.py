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
    pass_context=True,
)
async def generate_publication(context: Any, run_id: int) -> None:
    """Generate publication from sealed inputs."""
    # To be fully wired in Task 5
    logger.info("generate_publication queued for run %s", run_id)


@procrastinate_app.task(
    name=PREPARE_DELIVERY_PAYLOADS_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    pass_context=True,
)
async def prepare_delivery_payloads(context: Any, publication_id: int) -> None:
    """Prepare immutable delivery payloads for a created publication."""
    # To be fully wired in Task 7
    logger.info("prepare_delivery_payloads queued for publication %s", publication_id)


@procrastinate_app.task(
    name=DELIVER_PAYLOAD_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    pass_context=True,
)
async def deliver_publication_payload(context: Any, delivery_id: int) -> None:
    """Deliver a prepared publication payload."""
    # To be fully wired in Task 7
    logger.info("deliver_publication_payload queued for delivery %s", delivery_id)


@procrastinate_app.task(
    name=CREATE_SCHEDULED_PUBLICATION_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    pass_context=True,
)
async def create_scheduled_publication(
    context: Any,
    edition_slug: str,
    publication_type: str,
    snapshot_at: str,
) -> None:
    """Create scheduled publication run and seal candidates."""
    from src.publication.snapshot import PublicationSnapshotService
    from src.repositories.editions import EditionRepository

    runtime = get_runtime()
    snap_dt = dt.datetime.fromisoformat(snapshot_at)
    async with runtime.uow.transaction() as conn:
        edition = await EditionRepository().get_by_slug(conn, edition_slug)
        if edition is None:
            raise ValueError(f"edition slug {edition_slug} not found")
        edition_id = edition.id

    service = PublicationSnapshotService(uow=runtime.uow)
    req_key = f"scheduled:{edition_slug}:{publication_type}:{snap_dt.isoformat()}"
    run = await service.create_run(
        edition_id=edition_id,
        publication_type=publication_type,
        snapshot_at=snap_dt,
        request_key=req_key,
    )
    await service.seal_candidates(run.id)
    async with runtime.uow.transaction() as conn:
        await select_stories_for_publication.configure(connection=conn).defer_async(run_id=run.id)
