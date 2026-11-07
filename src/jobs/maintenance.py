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
    """Nightly cleanup: expired artifacts, idle vectors, old job and collection history."""
    import datetime as dt

    from src.retention import RetentionService
    from src.runtime import get_runtime

    runtime = get_runtime()
    service = RetentionService(uow=runtime.uow)
    await service.cleanup(now=dt.datetime.fromtimestamp(timestamp, dt.timezone.utc))


@procrastinate_app.periodic(cron="20 4,16 * * *", periodic_id="situation-memory-update")
@procrastinate_app.task(queue="maintenance", queueing_lock="situation-memory-update")
async def update_situation_memory_job(timestamp: int) -> None:
    """Advance each edition's running-story memory before the scheduled publications."""
    import datetime as dt
    import logging

    from src.article_generator import ArticleGenerator
    from src.config_loader import load_config
    from src.publication.situation_memory import update_situation_memory
    from src.runtime import get_runtime

    log = logging.getLogger(__name__)
    runtime = get_runtime()
    config = load_config()
    generator = ArticleGenerator(config=config, logger=log)
    model = getattr(config.settings, "openai_model", None) or getattr(
        config.settings, "ai_model", None
    )
    as_of = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
    async with runtime.uow.transaction() as conn:
        cursor = await conn.execute("SELECT id FROM editions ORDER BY id")
        edition_ids = [int(row[0]) for row in await cursor.fetchall()]
    for edition_id in edition_ids:
        await update_situation_memory(
            uow=runtime.uow,
            provider=generator.provider,
            model=model,
            edition_id=edition_id,
            as_of=as_of,
            log=log,
            reasoning_effort=getattr(
                config.settings.publication_editorial, "situation_memory_reasoning_effort", "none"
            ),
            reasoning_headroom_tokens=getattr(
                config.settings.publication_editorial, "reasoning_headroom_tokens", 12000
            ),
        )


@procrastinate_app.periodic(cron="35 */2 * * *", periodic_id="storyline-chronicle")
@procrastinate_app.task(queue="maintenance", queueing_lock="storyline-chronicle")
async def advance_storyline_chronicle(timestamp: int) -> None:
    """Build closed chronicle weeks and roll up completed periods (AGENTS.md §0.12)."""
    import datetime as dt
    import logging

    from src.article_generator import ArticleGenerator
    from src.config_loader import load_config
    from src.publication.chronicle import advance_chronicle
    from src.runtime import get_runtime

    log = logging.getLogger(__name__)
    config = load_config()
    editorial = config.settings.publication_editorial
    if not getattr(editorial, "chronicle_enabled", False):
        return
    runtime = get_runtime()
    generator = ArticleGenerator(config=config, logger=log)
    model = getattr(config.settings, "openai_model", None) or getattr(
        config.settings, "ai_model", None
    )
    now = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
    async with runtime.uow.transaction() as conn:
        cursor = await conn.execute("SELECT id FROM editions WHERE enabled ORDER BY id")
        edition_ids = [int(row[0]) for row in await cursor.fetchall()]
    for edition_id in edition_ids:
        summary = await advance_chronicle(
            uow=runtime.uow,
            provider=generator.provider,
            model=model,
            edition_id=edition_id,
            now=now,
            reasoning_effort=getattr(editorial, "chronicle_reasoning_effort", "none"),
            headroom_tokens=getattr(editorial, "reasoning_headroom_tokens", 12000),
            log=log,
        )
        log.info("storyline chronicle edition %s: %s", edition_id, summary)


@procrastinate_app.periodic(cron="0 7 * * *", periodic_id="openrouter-balance-check")
@procrastinate_app.task(queue="maintenance", queueing_lock="openrouter-balance-check")
async def check_openrouter_balance_job(timestamp: int) -> None:
    """Daily: warn the operators before OpenRouter credits or the daily budget run out."""
    from telegram import Bot

    from src.config_loader import load_config
    from src.openrouter_balance import check_openrouter_balance

    config = load_config()
    if not config.openrouter_api_key or not config.settings.admin_user_ids:
        return
    async with Bot(token=config.telegram_bot_token) as bot:

        async def send(chat_id: int, text: str) -> None:
            await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)

        await check_openrouter_balance(
            api_key=config.openrouter_api_key,
            base_url=config.openrouter_base_url,
            recipient_ids=config.settings.admin_user_ids,
            send_message=send,
        )
