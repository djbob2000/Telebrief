"""Durable collection jobs: periodic dispatcher and per-source scan task.

Procrastinate owns orchestration. ``dispatch_due_sources`` runs every minute
(cron), loads enabled sources with their checkpoints, applies the pure
:class:`~src.ingestion.schedule.CollectionSchedulePolicy`, and defers one
bounded-retry ``scan_source`` job per due source. ``scan_source`` selects the
platform collector, performs the network scan OUTSIDE any domain transaction,
then persists the batch through :class:`~src.ingestion.service.IngestionService`
inside a single committed transaction.

Typed outcome handling:

* ``transient`` commits partial observations, then raises
  :class:`~src.ingestion.errors.TransientCollectionError` so the bounded
  Procrastinate retry strategy re-runs the job (safe: ingestion is idempotent);
* ``rate_limited`` commits observations plus ``backoff_until`` derived from the
  provider retry-after, and returns - later dispatcher ticks reschedule after
  the backoff instead of hammering the provider;
* ``auth_required`` / ``account_action_required`` / ``access_denied`` /
  ``permanent`` (and the other terminal outcomes) are recorded and returned
  without any automatic retry loop;
* ``success`` simply returns after persistence.

Jobs resolve everything through ``get_runtime()``; none constructs a fresh
connection pool. The deferral helper is the single place computing the
per-source ``scan-source:<id>`` queueing lock plus the optional platform
execution lock (Telegram needs none; Plan 5 extends the resolver).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued

from src.domain.sources import Source
from src.ingestion.errors import TransientCollectionError
from src.ingestion.models import CollectionBatch, CollectionOutcome, CollectionTrigger
from src.ingestion.protocol import CollectionContext
from src.ingestion.registry import build_default_collector_registry
from src.ingestion.repository import IngestionRepository
from src.ingestion.schedule import CollectionSchedulePolicy
from src.ingestion.service import IngestionService
from src.jobs.app import procrastinate_app
from src.repositories.sources import SourceRepository
from src.runtime import get_runtime

logger = logging.getLogger(__name__)

SCAN_SOURCE_TASK_NAME = "scan_source"
DISPATCHER_TASK_NAME = "dispatch_due_sources"

NORMAL_COLLECTION_PRIORITY = 0
PRE_PUBLISH_PRIORITY = 100

DISPATCHER_QUEUEING_LOCK = "source-collection-dispatcher"

# Bounded transient retry: max_attempts counts TOTAL executions (the gate is
# attempts >= max_attempts with attempts starting at 0), so 2 means the
# initial attempt plus exactly two retries, honoring "transient may retry
# twice". Waits increase across retries: total_wait = wait + linear_wait *
# attempts → 30s after the first failure, then 90s. Only genuinely transient
# failures enter this loop; repeated execution is safe because SourceItem /
# Revision application is idempotent at the database level.
TRANSIENT_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=2,
    wait=30,
    linear_wait=60,
    retry_exceptions=(TransientCollectionError,),
)

# Conservative resume delay when a rate-limited batch carries no usable
# provider retry-after; otherwise the persisted value comes from the batch.
RATE_LIMIT_FALLBACK_BACKOFF_SECONDS = 900


# Platform-specific execution locks shared across sources authenticated by one
def _resolve_facebook_execution_lock(source: Source) -> str | None:
    options = source.collector_options or {}
    auth_profile = options.get("auth_profile") or options.get("auth_profile_id") or "default"
    return f"facebook-auth-profile:{auth_profile}"


_EXECUTION_LOCK_RESOLVERS: dict[str, Callable[[Source], str | None]] = {
    "facebook": _resolve_facebook_execution_lock,
}

schedule_policy = CollectionSchedulePolicy()

collector_registry = build_default_collector_registry()


def resolve_execution_lock(source: Source) -> str | None:
    """Optional cross-source execution lock for the source's platform."""
    resolver = _EXECUTION_LOCK_RESOLVERS.get(source.platform)
    return None if resolver is None else resolver(source)


async def enqueue_source_scan(
    source_id: int, trigger: CollectionTrigger, priority: int
) -> int | None:
    """Defer one ``scan_source`` job; the single place computing lock options.

    Returns the queued job id, or ``None`` when the source vanished, is not
    registered, or a job with the same ``scan-source:<id>`` queueing lock is
    already waiting (duplicate enqueues collapse into that no-op).
    """
    runtime = get_runtime()
    app = runtime.procrastinate_app

    async with runtime.uow.transaction() as conn:
        source = await SourceRepository().get(conn, source_id)
    if source is None:
        logger.warning(f"enqueue_source_scan: unknown source {source_id}; skipping")
        return None

    execution_lock = resolve_execution_lock(source)
    try:
        return (
            await app.tasks[SCAN_SOURCE_TASK_NAME]
            .configure(
                queueing_lock=f"scan-source:{source_id}",
                lock=execution_lock,
                priority=priority,
            )
            .defer_async(source_id=source_id, trigger=trigger.value)
        )
    except AlreadyEnqueued:
        logger.info(f"scan-source:{source_id} already queued; skipping duplicate enqueue")
        return None


@procrastinate_app.task(
    name=SCAN_SOURCE_TASK_NAME,
    queue="collection",
    retry=TRANSIENT_RETRY_STRATEGY,
)
async def scan_source(source_id: int, trigger: str) -> None:
    """Scan one source with its platform collector, then persist the batch."""
    runtime = get_runtime()
    trigger_value = CollectionTrigger(trigger)

    async with runtime.uow.transaction() as conn:
        source = await SourceRepository().get(conn, source_id)
        checkpoint = await IngestionRepository().get_checkpoint(conn, source_id)

    if source is None or not source.enabled:
        logger.warning(f"scan_source: source {source_id} missing or disabled; skipping")
        return

    # Network work stays outside every domain transaction.
    collector = collector_registry.select(source.platform)
    context = CollectionContext(now=datetime.now(timezone.utc))
    batch = await collector.scan(source, checkpoint, context)

    ingestion_service = IngestionService(uow=runtime.uow, repo=IngestionRepository())
    await ingestion_service.ingest_batch(source_id, trigger_value, batch)

    if batch.outcome == CollectionOutcome.TRANSIENT:
        # Partial observations are committed above; the retry strategy may now
        # re-run this job safely.
        raise TransientCollectionError(source_id=source_id)
    if batch.outcome == CollectionOutcome.RATE_LIMITED:
        async with runtime.uow.transaction() as conn:
            await IngestionRepository().apply_backoff(
                conn,
                source_id=source_id,
                backoff_until=_rate_limit_backoff_until(batch),
            )
        return  # checkpoint.backoff_until prevents dispatcher hammering
    return


def _rate_limit_backoff_until(batch: CollectionBatch) -> datetime:
    """Resume time for a rate-limited source: provider retry-after or fallback."""
    raw = batch.adapter_state.get("retry_after_seconds")
    seconds = (
        raw
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0
        else RATE_LIMIT_FALLBACK_BACKOFF_SECONDS
    )
    return batch.completed_at + timedelta(seconds=seconds)


@procrastinate_app.periodic(
    cron="* * * * *",
    periodic_id=DISPATCHER_QUEUEING_LOCK,
)
@procrastinate_app.task(
    queue="maintenance",
    queueing_lock=DISPATCHER_QUEUEING_LOCK,
)
async def dispatch_due_sources(timestamp: int) -> None:
    """Defer scans for every due source; never sleeps inside a worker job."""
    scheduled_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        candidates = await IngestionRepository().list_collection_candidates(conn)

    # Sources on platforms without a registered collector would otherwise turn
    # into a permanently failing scan_source job every dispatcher minute.
    registered = collector_registry.registered_platforms()
    unregistered = sorted(
        {source.platform for source, _ in candidates if source.platform not in registered}
    )
    if unregistered:
        logger.warning(
            "dispatch_due_sources: no collector registered for platform(s) "
            f"{', '.join(unregistered)}; skipping those sources"
        )

    for source, checkpoint in candidates:
        if source.platform not in registered:
            continue
        if schedule_policy.is_due(source, checkpoint, scheduled_at):
            await enqueue_source_scan(
                source_id=source.id,
                trigger=CollectionTrigger.SCHEDULED,
                priority=NORMAL_COLLECTION_PRIORITY,
            )
