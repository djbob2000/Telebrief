"""Periodic maintenance tasks for the Telebrief Procrastinate queue."""

from __future__ import annotations

import logging

import procrastinate
from procrastinate import JobContext

from src.jobs.app import procrastinate_app

logger = logging.getLogger(__name__)


@procrastinate_app.periodic(cron="*/10 * * * *")
@procrastinate_app.task(queue="maintenance", queueing_lock="retry_stalled_jobs", pass_context=True)
async def retry_stalled_jobs(context: JobContext, timestamp: int) -> None:
    """Requeue jobs stuck in the doing state so workers retry them."""
    del context, timestamp
    stalled_jobs = await procrastinate_app.job_manager.get_stalled_jobs()
    for job in stalled_jobs:
        try:
            await procrastinate_app.job_manager.retry_job(job)
        except procrastinate.exceptions.UniqueViolation as exc:
            if exc.queueing_lock and exc.constraint_name and "queueing_lock" in exc.constraint_name:
                blockers = list(
                    await procrastinate_app.job_manager.list_jobs_async(
                        status="todo",
                        queueing_lock=exc.queueing_lock,
                    )
                )
                recoverable = [
                    blocker
                    for blocker in blockers
                    if blocker.id is not None
                    and blocker.id != job.id
                    and blocker.task_name == job.task_name
                    and blocker.lock == job.lock
                ]
                if len(recoverable) == 1:
                    blocker = recoverable[0]
                    if blocker.id is not None:
                        await procrastinate_app.job_manager.cancel_job_by_id_async(blocker.id)
                        try:
                            await procrastinate_app.job_manager.retry_job(job)
                            continue
                        except procrastinate.exceptions.UniqueViolation:
                            logger.error(
                                "stalled job %s still cannot be retried after cancelling blocker %s",
                                job.id,
                                blocker.id,
                            )
                            continue
                logger.info(
                    "stalled job %s was not retried because queueing_lock=%s is already occupied",
                    job.id,
                    exc.queueing_lock,
                )
                continue
            raise


@procrastinate_app.periodic(
    cron="*/5 * * * *",
    periodic_id="publication-failure-notification-redrive",
)
@procrastinate_app.task(
    queue="maintenance",
    queueing_lock="publication-failure-notification-redrive",
)
async def redrive_publication_failure_notifications(timestamp: int) -> None:
    """Requeue durable publication-failure outbox rows after queue failures."""
    del timestamp
    from src.config_loader import load_config
    from src.publication.notifications import PublicationFailureNotificationService

    await PublicationFailureNotificationService(config=load_config()).redrive_pending()


@procrastinate_app.periodic(cron="15 3 * * *", periodic_id="retention-cleanup")
@procrastinate_app.task(queue="maintenance", queueing_lock="retention-cleanup")
async def retention_cleanup(timestamp: int) -> None:
    """Periodic cleanup of expired diagnostic artifacts."""
    import datetime as dt

    from src.retention import RetentionService
    from src.runtime import get_runtime

    runtime = get_runtime()
    service = RetentionService(uow=runtime.uow)
    await service.cleanup(now=dt.datetime.fromtimestamp(timestamp, dt.timezone.utc))
