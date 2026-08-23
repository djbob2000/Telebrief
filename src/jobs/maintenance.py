"""Periodic maintenance tasks for the Telebrief Procrastinate queue."""

from __future__ import annotations

from procrastinate import JobContext

from src.jobs.app import procrastinate_app


@procrastinate_app.periodic(cron="*/10 * * * *")
@procrastinate_app.task(queueing_lock="retry_stalled_jobs", pass_context=True)
async def retry_stalled_jobs(context: JobContext, timestamp: int) -> None:
    """Requeue jobs stuck in the doing state so workers retry them."""
    del context, timestamp
    stalled_jobs = await procrastinate_app.job_manager.get_stalled_jobs()
    for job in stalled_jobs:
        await procrastinate_app.job_manager.retry_job(job)


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
