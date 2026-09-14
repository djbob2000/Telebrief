"""Bounded background and publication authority job wrappers."""

from __future__ import annotations

import datetime as dt
import logging
import time

import procrastinate

from src.config_loader import load_config
from src.jobs.app import procrastinate_app
from src.processing.edition_scope import SCOPE_VERSION, resolve_edition_scope, scope_config_hash
from src.processing.event_authority import AuthorityBatchStats, EventAuthorityService
from src.processing.event_triage import TRIAGE_VERSION
from src.publication.orchestrator import PublicationOrchestrator
from src.repositories.event_authority import EventAuthorityRepository
from src.runtime import get_runtime

logger = logging.getLogger(__name__)
PUBLICATION_AUTHORITY_PRIORITY = 100
BACKGROUND_AUTHORITY_PRIORITY = -10


class AuthorityCoordinationBusy(RuntimeError):
    """No publication target progressed because assignment coordination was busy."""


PUBLICATION_AUTHORITY_CONTENTION_RETRY = procrastinate.RetryStrategy(
    max_attempts=5,
    wait=2,
    linear_wait=1,
    retry_exceptions={AuthorityCoordinationBusy},
)


def log_authority_complete(
    *,
    mode: str,
    intent_id: int | None,
    edition_id: int,
    stats: AuthorityBatchStats,
    remaining_gap: int | None,
    duration_ms: int,
    shard_id: int | None = None,
    backlog_before: int | None = None,
    backlog_after: int | None = None,
    progressed: bool | None = None,
    continuation_enqueued: bool | None = None,
    prompt_chars: int | None = None,
    block_reason: str | None = None,
) -> None:
    resolved_block_reason = block_reason or authority_block_reason(stats, remaining_gap or 0)
    logger.info(
        "event_first_authority_complete",
        extra={
            "mode": mode,
            "intent_id": intent_id,
            "edition_id": edition_id,
            "shard_id": shard_id,
            "backlog_before": backlog_before,
            "backlog_after": backlog_after,
            "progressed": progressed,
            "continuation_enqueued": continuation_enqueued,
            "prompt_chars": prompt_chars,
            "block_reason": resolved_block_reason,
            "requested": stats.requested,
            "already_satisfied": stats.already_satisfied,
            "claimed": stats.claimed,
            "busy": stats.busy,
            "triaged": stats.triaged,
            "retry_wait": stats.retry_wait,
            "terminal": stats.terminal,
            "provider_failures": stats.provider_failures,
            "remaining_gap": remaining_gap,
            "duration_ms": duration_ms,
        },
    )


def authority_block_reason(stats: AuthorityBatchStats, remaining_gap: int) -> str | None:
    if remaining_gap == 0:
        return None
    if stats.terminal > 0:
        return "terminal"
    if stats.provider_failures > 0:
        return "provider_failure"
    if stats.retry_wait > 0 and stats.triaged == 0:
        return "retry_wait"
    if stats.busy > 0 and stats.triaged == 0 and stats.already_satisfied == 0:
        return "coordination_busy"
    return "pending"


async def _load_background_targets(
    edition_id: int,
    *,
    limit: int,
    shard_index: int = 0,
    shard_count: int = 1,
):
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    cfg = config.settings.event_pipeline
    async with runtime.uow.transaction() as conn:
        _slug, scope_config = await resolve_edition_scope(conn, config, edition_id)
        return await EventAuthorityRepository().list_background_targets(
            conn,
            edition_id=edition_id,
            triage_version=TRIAGE_VERSION,
            scope_version=SCOPE_VERSION,
            scope_config_hash=scope_config_hash(scope_config),
            now=dt.datetime.now(dt.timezone.utc),
            limit=limit,
            active_window_hours=cfg.active_window_hours,
            shard_index=shard_index,
            shard_count=shard_count,
        )


async def _defer_background_authority_continuation(edition_id: int, shard_id: int = 0) -> None:
    from procrastinate.exceptions import AlreadyEnqueued

    try:
        await process_background_authority_batch.configure(
            priority=BACKGROUND_AUTHORITY_PRIORITY,
            queueing_lock=f"authority-background-continuation:{edition_id}:{shard_id}",
        ).defer_async(edition_id=edition_id, shard_id=shard_id)
    except AlreadyEnqueued:
        return


@procrastinate_app.task(
    queue="authority",
    name="process_background_authority_batch",
    lock="authority-background:{edition_id}:{shard_id}",
)
async def process_background_authority_batch(edition_id: int, shard_id: int = 0) -> None:
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    cfg = getattr(config.settings, "event_pipeline", None)
    if not getattr(cfg, "background_authority_enabled", False):
        return
    shard_count = getattr(cfg, "authority_shard_count", 1)
    targets = await _load_background_targets(
        edition_id,
        limit=config.settings.event_pipeline.triage_batch_size,
        shard_index=shard_id,
        shard_count=shard_count,
    )
    if not targets:
        return
    started = time.perf_counter()
    result = await EventAuthorityService.from_runtime(runtime, config).process_batch(
        targets, mode="background", coordination_scope=f"authority:{shard_id}"
    )
    progressed = result.stats.triaged > 0 or (
        result.stats.claimed > 0
        and result.stats.busy == 0
        and result.stats.provider_failures == 0
        and result.stats.retry_wait == 0
    )
    can_continue = (
        progressed
        and result.stats.provider_failures == 0
        and not (result.stats.retry_wait > 0 and result.stats.triaged == 0)
        and not (
            result.stats.busy > 0
            and result.stats.triaged == 0
            and result.stats.already_satisfied == 0
        )
    )
    continuation_enqueued = False
    remaining_after: int | None = None
    if can_continue:
        remaining = await _load_background_targets(
            edition_id, limit=1, shard_index=shard_id, shard_count=shard_count
        )
        if remaining:
            remaining_after = len(remaining)
            await _defer_background_authority_continuation(edition_id, shard_id)
            continuation_enqueued = True
        else:
            remaining_after = 0

    log_authority_complete(
        mode="background",
        intent_id=None,
        edition_id=edition_id,
        stats=result.stats,
        remaining_gap=remaining_after,
        duration_ms=int((time.perf_counter() - started) * 1000),
        shard_id=shard_id,
        backlog_before=len(targets),
        backlog_after=remaining_after,
        progressed=progressed,
        continuation_enqueued=continuation_enqueued,
    )
    from src.jobs.event_enrichment import defer_event_enrichment

    for target in result.enrichment_targets:
        await defer_event_enrichment(target.story_id, target.assignment_id)


async def request_background_authority_dispatch(edition_id: int) -> None:
    from procrastinate.exceptions import AlreadyEnqueued

    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    cfg = getattr(config.settings, "event_pipeline", None)
    if not getattr(cfg, "background_authority_enabled", False):
        return

    try:
        await dispatch_background_authority.configure(
            priority=BACKGROUND_AUTHORITY_PRIORITY,
            queueing_lock=f"authority-background-dispatch:{edition_id}",
        ).defer_async(edition_id=edition_id)
    except AlreadyEnqueued:
        return


@procrastinate_app.task(
    queue="maintenance",
    name="dispatch_background_authority",
    lock="authority-background-dispatch:{edition_id}",
)
async def dispatch_background_authority(edition_id: int) -> None:
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    cfg = getattr(config.settings, "event_pipeline", None)
    if not getattr(cfg, "background_authority_enabled", False):
        return
    targets = await _load_background_targets(edition_id, limit=1)
    if not targets:
        return
    shard_count = getattr(cfg, "authority_shard_count", 1)
    for shard_id in range(shard_count):
        await process_background_authority_batch.configure(
            priority=BACKGROUND_AUTHORITY_PRIORITY,
        ).defer_async(edition_id=edition_id, shard_id=shard_id)


@procrastinate_app.task(
    queue="maintenance",
    name="process_publication_authority_gap",
    lock="publication-authority-dispatch:{intent_id}",
)
async def process_publication_authority_gap(intent_id: int) -> None:
    from procrastinate.exceptions import AlreadyEnqueued

    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    orchestrator = PublicationOrchestrator(uow=runtime.uow, config=config)
    now = dt.datetime.now(dt.timezone.utc)
    async with runtime.uow.transaction() as conn:
        intent = await orchestrator.readiness_repo.get_refresh_run(conn, intent_id)
        if intent is None or intent.status not in {
            "collecting",
            "processing",
            "ready_waiting_slot",
        }:
            return
        if now >= intent.deadline_at:
            await _defer_publication_reconcile(intent_id)
            return

    cfg = config.settings.event_pipeline
    shard_count = getattr(cfg, "authority_shard_count", 1)
    for shard_id in range(shard_count):
        try:
            await process_publication_authority_batch.configure(
                priority=PUBLICATION_AUTHORITY_PRIORITY,
                queueing_lock=f"publication-authority:{intent_id}:{shard_id}",
            ).defer_async(intent_id=intent_id, shard_id=shard_id)
        except AlreadyEnqueued:
            pass


@procrastinate_app.task(
    queue="authority",
    name="process_publication_authority_batch",
    retry=PUBLICATION_AUTHORITY_CONTENTION_RETRY,
    lock="publication-authority:{intent_id}:{shard_id}",
)
async def process_publication_authority_batch(intent_id: int, shard_id: int = 0) -> None:
    from procrastinate.exceptions import AlreadyEnqueued

    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    cfg = config.settings.event_pipeline
    shard_count = getattr(cfg, "authority_shard_count", 1)
    orchestrator = PublicationOrchestrator(uow=runtime.uow, config=config)
    now = dt.datetime.now(dt.timezone.utc)
    async with runtime.uow.transaction() as conn:
        intent = await orchestrator.readiness_repo.get_refresh_run(conn, intent_id)
        if intent is None or intent.status not in {
            "collecting",
            "processing",
            "ready_waiting_slot",
        }:
            return
        if now >= intent.deadline_at:
            await _defer_publication_reconcile(intent_id)
            return
        eval_boundary = intent.knowledge_snapshot_at or min(now, intent.deadline_at)
        targets = await orchestrator.find_authority_gap_targets(
            conn,
            intent,
            evaluation_at=eval_boundary,
            shard_index=shard_id,
            shard_count=shard_count,
        )

    if not targets:
        await _defer_publication_reconcile(intent_id)
        return

    started = time.perf_counter()
    batch_targets = targets[: cfg.triage_batch_size]
    result = await EventAuthorityService.from_runtime(runtime, config).process_batch(
        batch_targets,
        mode="publication",
        coordination_scope=f"publication:{intent_id}:{shard_id}",
    )
    observed_at = dt.datetime.now(dt.timezone.utc)
    async with runtime.uow.transaction() as conn:
        eval_boundary = intent.knowledge_snapshot_at or min(observed_at, intent.deadline_at)
        remaining = await orchestrator.find_authority_gap_targets(
            conn, intent, evaluation_at=eval_boundary
        )
        remaining_gap = len(remaining)
        if hasattr(orchestrator.readiness_repo, "update_authority_diagnostics"):
            await orchestrator.readiness_repo.update_authority_diagnostics(
                conn,
                refresh_run_id=intent_id,
                observed_at=observed_at,
                gap_count=remaining_gap,
                block_reason=authority_block_reason(result.stats, remaining_gap),
                terminal_count=result.stats.terminal,
            )

    progressed = result.stats.triaged > 0 or result.stats.already_satisfied > 0
    continuation_enqueued = False
    if progressed and remaining_gap > 0 and observed_at < intent.deadline_at:
        async with runtime.uow.transaction() as conn:
            shard_remaining = await orchestrator.find_authority_gap_targets(
                conn,
                intent,
                evaluation_at=eval_boundary,
                shard_index=shard_id,
                shard_count=shard_count,
            )
        if shard_remaining:
            try:
                await process_publication_authority_batch.configure(
                    priority=PUBLICATION_AUTHORITY_PRIORITY,
                    queueing_lock=f"publication-authority-continuation:{intent_id}:{shard_id}",
                ).defer_async(intent_id=intent_id, shard_id=shard_id)
                continuation_enqueued = True
            except AlreadyEnqueued:
                pass

    log_authority_complete(
        mode="publication",
        intent_id=intent_id,
        edition_id=intent.edition_id,
        stats=result.stats,
        remaining_gap=remaining_gap,
        duration_ms=int((time.perf_counter() - started) * 1000),
        shard_id=shard_id,
        backlog_before=len(targets),
        backlog_after=remaining_gap,
        progressed=progressed,
        continuation_enqueued=continuation_enqueued,
    )
    from src.jobs.event_enrichment import defer_event_enrichment

    for target in result.enrichment_targets:
        await defer_event_enrichment(target.story_id, target.assignment_id)
    if (
        result.stats.triaged == 0
        and result.stats.already_satisfied == 0
        and result.stats.busy > 0
        and result.stats.retry_wait == 0
        and result.stats.terminal == 0
        and result.stats.provider_failures == 0
    ):
        await _defer_publication_reconcile(intent_id)
        raise AuthorityCoordinationBusy(
            f"publication intent {intent_id} shard {shard_id} authority targets are busy"
        )
    await _defer_publication_reconcile(intent_id)


async def _defer_publication_reconcile(intent_id: int) -> None:
    from procrastinate.exceptions import AlreadyEnqueued

    try:
        await reconcile_publication_intent.configure(
            priority=PUBLICATION_AUTHORITY_PRIORITY,
            queueing_lock=f"publication-authority-reconcile:{intent_id}",
        ).defer_async(intent_id=intent_id)
    except AlreadyEnqueued:
        return


@procrastinate_app.task(
    queue="maintenance",
    name="reconcile_publication_intent",
    lock="publication-authority-reconcile:{intent_id}",
)
async def reconcile_publication_intent(intent_id: int) -> None:
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    orchestrator = PublicationOrchestrator(uow=runtime.uow, config=config)
    await orchestrator.reconcile(intent_id, now=dt.datetime.now(dt.timezone.utc))


@procrastinate_app.periodic(
    cron="*/5 * * * *", periodic_id="periodic-background-authority-dispatch"
)
@procrastinate_app.task(
    queue="maintenance",
    name="periodic_background_authority_dispatch",
    queueing_lock="periodic-background-authority-dispatch",
)
async def periodic_background_authority_dispatch(timestamp: int) -> None:
    del timestamp
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    cfg = getattr(config.settings, "event_pipeline", None)
    if not getattr(cfg, "background_authority_enabled", False):
        return
    async with runtime.uow.transaction() as conn:
        cursor = await conn.execute("SELECT id FROM editions ORDER BY id")
        edition_ids = [int(row[0]) for row in await cursor.fetchall()]
    for edition_id in edition_ids:
        await request_background_authority_dispatch(edition_id)
