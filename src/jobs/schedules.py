"""Durable publication schedule orchestration (Plan 4 Task 8).

Procrastinate owns the publication clock: a one-minute periodic dispatcher
reads the configured digest/article times, decides what is due from the
periodic ``timestamp`` (never wall-clock ``now()``), and defers durable work
only — it never generates content inline.

Due semantics:

* at ``slot - pre_publish_lead_minutes`` a high-priority ``PRE_PUBLISH``
  refresh fans out ordinary source scans so fresh knowledge is collected
  ahead of the publication minute;
* at the configured publication minute a
  :func:`src.jobs.publication.create_scheduled_publication` job is deferred
  with ``snapshot_at`` equal to the scheduled slot; the deterministic request
  key derived inside that task makes repeated dispatcher executions
  idempotent — a late worker still uses the scheduled timestamp, so
  at-least-once dispatch cannot create duplicate scheduled publications.

A slot is evaluated once per matching minute tick; if no worker was running
at that minute the slot's publication is skipped for that day (there is no
Telebrief-owned schedule state table by design — Procrastinate owns
orchestration).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import procrastinate

from src.config_loader import Config
from src.jobs.app import procrastinate_app

logger = logging.getLogger(__name__)

PUBLICATION_SCHEDULE_DISPATCHER_TASK_NAME = "publication_schedule_dispatcher"
PUBLICATION_SCHEDULE_DISPATCHER_LOCK = "publication-schedule-dispatcher"
PRE_PUBLISH_REFRESH_TASK_NAME = "pre_publish_refresh"

PUBLISH_QUEUE = "publication"
DIGEST_PUBLICATION_TYPE = "digest_grouped"
ARTICLE_PUBLICATION_TYPE = "daily_article"
WEEKLY_ARTICLE_PUBLICATION_TYPE = "weekly_article"
MONTHLY_ARTICLE_PUBLICATION_TYPE = "monthly_article"
DEFAULT_EDITION_SLUG = "berdyansk"

PRE_PUBLISH_FANOUT_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=2,
    wait=15,
    linear_wait=30,
)


@dataclass(frozen=True)
class DuePublicationAction:
    """One durable action the dispatcher decided to defer."""

    kind: str  # "publish" or "pre_publish"
    queueing_lock: str
    task_kwargs: dict


def _parse_slot(time_str: str) -> tuple[int, int] | None:
    """Parse ``HH:MM`` into (hour, minute); None when unparseable."""
    try:
        hour_s, minute_s = time_str.split(":", maxsplit=1)
        hour, minute = int(hour_s), int(minute_s)
    except (AttributeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


_DAY_OF_WEEK_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _matches_schedule_day(day_val: str | int | None, target_dt: dt.datetime) -> bool:
    if day_val is None:
        return True
    if isinstance(day_val, str):
        expected_weekday = _DAY_OF_WEEK_MAP.get(day_val.strip().lower())
        if expected_weekday is not None:
            return target_dt.weekday() == expected_weekday
        try:
            expected_day = int(day_val)
            return target_dt.day == expected_day
        except ValueError:
            return False
    if isinstance(day_val, int):
        return target_dt.day == day_val
    return False


def _publication_types(config: Config) -> list[tuple[str, str, str | int | None]]:
    """(publication_type, schedule_time, schedule_day) tuples enabled in config."""
    entries: list[tuple[str, str, str | int | None]] = [
        (DIGEST_PUBLICATION_TYPE, config.settings.schedule_time, None)
    ]
    if config.settings.article.enabled:
        entries.append((ARTICLE_PUBLICATION_TYPE, config.settings.article.schedule_time, None))
    weekly_cfg = getattr(config.settings, "weekly_article", None)
    if weekly_cfg is not None and getattr(weekly_cfg, "enabled", False):
        entries.append(
            (
                WEEKLY_ARTICLE_PUBLICATION_TYPE,
                weekly_cfg.schedule_time,
                getattr(weekly_cfg, "schedule_day", "sunday"),
            )
        )
    monthly_cfg = getattr(config.settings, "monthly_article", None)
    if monthly_cfg is not None and getattr(monthly_cfg, "enabled", False):
        entries.append(
            (
                MONTHLY_ARTICLE_PUBLICATION_TYPE,
                monthly_cfg.schedule_time,
                getattr(monthly_cfg, "schedule_day", 1),
            )
        )
    return entries


DEFAULT_CATCH_UP_WINDOW_MINUTES = 120


def due_publication_actions(
    config: Config,
    scheduled_for: dt.datetime,
    *,
    catch_up_window_minutes: int = DEFAULT_CATCH_UP_WINDOW_MINUTES,
) -> list[DuePublicationAction]:
    """Pure decision helper: what is due at the periodic tick timestamp."""
    tz: dt.tzinfo
    try:
        tz = ZoneInfo(config.settings.timezone)
    except Exception:
        logger.warning(
            "invalid settings.timezone %r; falling back to UTC", config.settings.timezone
        )
        tz = dt.timezone.utc
    local_minute = scheduled_for.astimezone(tz).replace(second=0, microsecond=0)

    lead_minutes = config.settings.pre_publish_lead_minutes
    actions: list[DuePublicationAction] = []

    for publication_type, time_str, schedule_day in _publication_types(config):
        if not _matches_schedule_day(schedule_day, local_minute):
            continue
        slot = _parse_slot(time_str)
        if slot is None:
            logger.warning("invalid schedule time %r for %s", time_str, publication_type)
            continue
        hour, minute = slot
        publish_at = local_minute.replace(hour=hour, minute=minute)
        # Check if local_minute falls within [publish_at, publish_at + catch_up_window_minutes)
        publish_window_end = publish_at + dt.timedelta(minutes=max(1, catch_up_window_minutes))
        if publish_at <= local_minute < publish_window_end:
            lag_minutes = getattr(config.settings, "publication_snapshot_lag_minutes", 0)
            snapshot_at = publish_at - dt.timedelta(minutes=max(0, lag_minutes))
            snapshot_iso = snapshot_at.astimezone(dt.timezone.utc).isoformat()
            actions.append(
                DuePublicationAction(
                    kind="publish",
                    queueing_lock=f"publication-schedule:{publication_type}:{snapshot_iso}",
                    task_kwargs={
                        "edition_slug": DEFAULT_EDITION_SLUG,
                        "publication_type": publication_type,
                        "slot_at": snapshot_key(publish_at),
                        "snapshot_at": snapshot_iso,
                    },
                )
            )
            continue
        pre_publish_at = publish_at - dt.timedelta(minutes=lead_minutes)
        pre_publish_window_end = pre_publish_at + dt.timedelta(minutes=max(1, lead_minutes))
        if lead_minutes > 0 and pre_publish_at <= local_minute < pre_publish_window_end:
            actions.append(
                DuePublicationAction(
                    kind="pre_publish",
                    queueing_lock=f"pre-publish-refresh:{publication_type}:{snapshot_key(publish_at)}",
                    task_kwargs={
                        "publication_type": publication_type,
                        "publish_at": snapshot_key(publish_at),
                    },
                )
            )
    return actions


def snapshot_key(publish_at: dt.datetime) -> str:
    return publish_at.astimezone(dt.timezone.utc).isoformat()


async def _fan_out_refresh_sources(refresh_run_id: int) -> int:
    """Defer pending source scans for a refresh without fabricating success."""
    from src.ingestion.models import CollectionTrigger
    from src.jobs.ingestion import PRE_PUBLISH_PRIORITY, enqueue_source_scan
    from src.publication.readiness_repository import PublicationReadinessRepository
    from src.runtime import get_runtime

    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        sources = await PublicationReadinessRepository().list_refresh_sources(conn, refresh_run_id)

    queued = 0
    for source in sources:
        if source.status != "pending":
            continue
        attempted_at = dt.datetime.now(dt.timezone.utc)
        async with runtime.uow.transaction() as conn:
            await PublicationReadinessRepository().mark_source_enqueue_attempt(
                conn,
                refresh_run_id=refresh_run_id,
                source_id=source.source_id,
                attempted_at=attempted_at,
            )
        job_id = await enqueue_source_scan(
            source_id=source.source_id,
            trigger=CollectionTrigger.PRE_PUBLISH,
            priority=PRE_PUBLISH_PRIORITY,
        )
        if job_id is not None:
            queued += 1
    return queued


@procrastinate_app.periodic(
    cron="* * * * *",
    periodic_id=PUBLICATION_SCHEDULE_DISPATCHER_LOCK,
)
@procrastinate_app.task(
    name=PUBLICATION_SCHEDULE_DISPATCHER_TASK_NAME,
    queue="maintenance",
    queueing_lock=PUBLICATION_SCHEDULE_DISPATCHER_LOCK,
)
async def publication_schedule_dispatcher(timestamp: int) -> None:
    """Decide what the configured schedule makes due and defer durable work."""
    from src.config_loader import load_config

    scheduled_for = dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc)
    config = load_config()

    from src.jobs.publication import create_scheduled_publication
    from src.publication.readiness import PublicationReadinessService
    from src.publication.readiness_repository import PublicationReadinessRepository
    from src.repositories.editions import EditionRepository
    from src.runtime import get_runtime

    runtime = get_runtime()
    readiness_service = PublicationReadinessService()

    async with runtime.uow.transaction() as conn:
        open_refreshes = await PublicationReadinessRepository().list_reconcilable_refresh_runs(conn)
        for refresh in open_refreshes:
            decision = await readiness_service.reconcile(
                conn,
                refresh.id,
                now=scheduled_for,
                on_deadline=config.settings.publication_readiness_on_deadline,
            )
            if decision.status in {"ready_for_preparation", "fallback_ready"}:
                prepared = await PublicationReadinessRepository().mark_preparing(
                    conn,
                    refresh_run_id=refresh.id,
                    fallback_used=decision.status == "fallback_ready",
                )
                if prepared is not None:
                    await create_scheduled_publication.configure(
                        connection=conn,
                        queueing_lock=f"publication-refresh:{refresh.id}",
                    ).defer_async(refresh_run_id=refresh.id)

    for action in due_publication_actions(config, scheduled_for):
        if action.kind == "publish":
            edition_slug = action.task_kwargs.get("edition_slug", DEFAULT_EDITION_SLUG)
            pub_type = action.task_kwargs.get("publication_type", "")
            slot_at = dt.datetime.fromisoformat(action.task_kwargs["slot_at"])
            async with runtime.uow.transaction() as conn:
                edition = await EditionRepository().get_by_slug(conn, edition_slug)
                if edition is None:
                    raise ValueError(f"edition slug {edition_slug} not found")
                source_ids = await EditionRepository().list_enabled_source_ids(conn, edition.id)
                requested_at = slot_at - dt.timedelta(
                    minutes=config.settings.pre_publish_lead_minutes
                )
                refresh = await readiness_service.create_refresh(
                    conn,
                    edition_id=edition.id,
                    publication_type=pub_type,
                    slot_at=slot_at,
                    source_ids=source_ids,
                    requested_at=requested_at,
                    deadline_minutes=config.settings.publication_readiness_deadline_minutes,
                )
                decision = await readiness_service.reconcile(
                    conn,
                    refresh.id,
                    now=scheduled_for,
                    on_deadline=config.settings.publication_readiness_on_deadline,
                )
                if decision.status in {"ready_for_preparation", "fallback_ready"}:
                    prepared = await PublicationReadinessRepository().mark_preparing(
                        conn,
                        refresh_run_id=refresh.id,
                        fallback_used=decision.status == "fallback_ready",
                    )
                    if prepared is not None:
                        await create_scheduled_publication.configure(
                            connection=conn,
                            queueing_lock=f"publication-refresh:{refresh.id}",
                        ).defer_async(refresh_run_id=refresh.id)
                else:
                    await pre_publish_refresh.configure(
                        connection=conn,
                        queueing_lock=f"pre-publish-refresh:{refresh.id}",
                    ).defer_async(refresh_run_id=refresh.id)
            logger.info("reconciled scheduled %s publication for %s", pub_type, slot_at)
        elif action.kind == "pre_publish":
            pub_type = action.task_kwargs.get("publication_type", "")
            publish_at = action.task_kwargs.get("publish_at", "")
            slot_at = dt.datetime.fromisoformat(publish_at)
            async with runtime.uow.transaction() as conn:
                edition = await EditionRepository().get_by_slug(conn, DEFAULT_EDITION_SLUG)
                if edition is None:
                    raise ValueError(f"edition slug {DEFAULT_EDITION_SLUG} not found")
                source_ids = await EditionRepository().list_enabled_source_ids(conn, edition.id)
                requested_at = slot_at - dt.timedelta(
                    minutes=config.settings.pre_publish_lead_minutes
                )
                refresh = await readiness_service.create_refresh(
                    conn,
                    edition_id=edition.id,
                    publication_type=pub_type,
                    slot_at=slot_at,
                    source_ids=source_ids,
                    requested_at=requested_at,
                    deadline_minutes=config.settings.publication_readiness_deadline_minutes,
                )
                await pre_publish_refresh.configure(
                    connection=conn,
                    queueing_lock=f"pre-publish-refresh:{refresh.id}",
                ).defer_async(refresh_run_id=refresh.id)
            logger.info("deferred pre-publish refresh %s for %s", refresh.id, slot_at)


@procrastinate_app.task(
    name=PRE_PUBLISH_REFRESH_TASK_NAME,
    queue="maintenance",
    retry=PRE_PUBLISH_FANOUT_RETRY_STRATEGY,
)
async def pre_publish_refresh(refresh_run_id: int) -> None:
    """Fan out a durable refresh; exceptions remain retryable task failures."""
    queued = await _fan_out_refresh_sources(refresh_run_id)
    logger.info("pre-publish refresh %s deferred %d source scans", refresh_run_id, queued)
