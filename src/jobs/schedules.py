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

from src.config_loader import Config
from src.jobs.app import procrastinate_app

logger = logging.getLogger(__name__)

PUBLICATION_SCHEDULE_DISPATCHER_TASK_NAME = "publication_schedule_dispatcher"
PUBLICATION_SCHEDULE_DISPATCHER_LOCK = "publication-schedule-dispatcher"

PUBLISH_QUEUE = "publication"
DIGEST_PUBLICATION_TYPE = "digest_grouped"
ARTICLE_PUBLICATION_TYPE = "daily_article"
DEFAULT_EDITION_SLUG = "berdyansk"


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


def _publication_types(config: Config) -> list[tuple[str, str]]:
    """(publication_type, schedule_time) pairs enabled in config."""
    entries = [(DIGEST_PUBLICATION_TYPE, config.settings.schedule_time)]
    if config.settings.article.enabled:
        entries.append((ARTICLE_PUBLICATION_TYPE, config.settings.article.schedule_time))
    return entries


def due_publication_actions(
    config: Config, scheduled_for: dt.datetime
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

    for publication_type, time_str in _publication_types(config):
        slot = _parse_slot(time_str)
        if slot is None:
            logger.warning("invalid schedule time %r for %s", time_str, publication_type)
            continue
        hour, minute = slot
        publish_at = local_minute.replace(hour=hour, minute=minute)
        if local_minute == publish_at:
            snapshot_iso = publish_at.astimezone(dt.timezone.utc).isoformat()
            actions.append(
                DuePublicationAction(
                    kind="publish",
                    queueing_lock=f"publication-schedule:{publication_type}:{snapshot_iso}",
                    task_kwargs={
                        "edition_slug": DEFAULT_EDITION_SLUG,
                        "publication_type": publication_type,
                        "snapshot_at": snapshot_iso,
                    },
                )
            )
            continue
        pre_publish_at = publish_at - dt.timedelta(minutes=lead_minutes)
        if lead_minutes > 0 and local_minute == pre_publish_at:
            actions.append(
                DuePublicationAction(
                    kind="pre_publish",
                    queueing_lock=f"pre-publish-refresh:{snapshot_key(publish_at)}",
                    task_kwargs={},
                )
            )
    return actions


def snapshot_key(publish_at: dt.datetime) -> str:
    return publish_at.astimezone(dt.timezone.utc).isoformat()


async def _fan_out_pre_publish_scans() -> int:
    """Defer one PRE_PUBLISH scan per registered source; returns count queued."""
    from src.ingestion.models import CollectionTrigger
    from src.jobs.ingestion import PRE_PUBLISH_PRIORITY, enqueue_source_scan

    queued = 0
    async for source_id in _iter_enabled_source_ids():
        job_id = await enqueue_source_scan(
            source_id=source_id,
            trigger=CollectionTrigger.PRE_PUBLISH,
            priority=PRE_PUBLISH_PRIORITY,
        )
        if job_id is not None:
            queued += 1
    return queued


async def _iter_enabled_source_ids():
    from src.repositories.sources import SourceRepository
    from src.runtime import get_runtime

    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        sources = await SourceRepository().list_enabled(conn)
    for source in sources:
        yield source.id


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

    for action in due_publication_actions(config, scheduled_for):
        if action.kind == "publish":
            await create_scheduled_publication.configure(
                queueing_lock=action.queueing_lock,
            ).defer_async(**action.task_kwargs)
            logger.info(
                "deferred scheduled %s publication for %s",
                action.task_kwargs["publication_type"],
                action.task_kwargs["snapshot_at"],
            )
        elif action.kind == "pre_publish":
            try:
                queued = await _fan_out_pre_publish_scans()
                logger.info("pre-publish refresh deferred %d source scans", queued)
            except Exception:
                # The publication itself must still be attempted even when the
                # opportunistic refresh could not be scheduled.
                logger.exception("pre-publish refresh fan-out failed")
