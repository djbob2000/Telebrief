"""Periodic maintenance tasks for the Telebrief Procrastinate queue."""

from __future__ import annotations

from procrastinate import JobContext

from src.jobs.app import procrastinate_app


@procrastinate_app.periodic(cron="*/10 * * * *")
@procrastinate_app.task(queue="maintenance", queueing_lock="retry_stalled_jobs", pass_context=True)
async def retry_stalled_jobs(context: JobContext, timestamp: int) -> None:
    """Requeue jobs stuck in the doing state so workers retry them."""
    del context, timestamp
    stalled_jobs = await procrastinate_app.job_manager.get_stalled_jobs()
    for job in stalled_jobs:
        await procrastinate_app.job_manager.retry_job(job)


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
