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
) -> None:
    logger.info(
        "event_first_authority_complete",
        extra={
            "mode": mode,
            "intent_id": intent_id,
            "edition_id": edition_id,
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


async def _load_background_targets(edition_id: int, *, limit: int):
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
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
        )


@procrastinate_app.task(
    queue="processing",
    name="process_background_authority_batch",
    lock="authority-background:{edition_id}",
)
async def process_background_authority_batch(edition_id: int) -> None:
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    targets = await _load_background_targets(
        edition_id, limit=config.settings.event_pipeline.triage_batch_size
    )
    if targets:
        started = time.perf_counter()
        result = await EventAuthorityService.from_runtime(runtime, config).process_batch(
            targets, mode="background"
        )
        log_authority_complete(
            mode="background",
            intent_id=None,
            edition_id=edition_id,
            stats=result.stats,
            remaining_gap=None,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        from src.jobs.event_enrichment import defer_event_enrichment

        for target in result.enrichment_targets:
            await defer_event_enrichment(target.story_id, target.assignment_id)
    await request_background_authority_dispatch(edition_id)


async def request_background_authority_dispatch(edition_id: int) -> None:
    from procrastinate.exceptions import AlreadyEnqueued

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
    targets = await _load_background_targets(edition_id, limit=1)
    if not targets:
        return
    from procrastinate.exceptions import AlreadyEnqueued

    try:
        await process_background_authority_batch.configure(
            priority=BACKGROUND_AUTHORITY_PRIORITY,
            queueing_lock=f"authority-background:{edition_id}",
        ).defer_async(edition_id=edition_id)
    except AlreadyEnqueued:
        return


@procrastinate_app.task(
    queue="processing",
    name="process_publication_authority_gap",
    retry=PUBLICATION_AUTHORITY_CONTENTION_RETRY,
    lock="publication-authority:{intent_id}",
)
async def process_publication_authority_gap(intent_id: int) -> None:
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
            targets = []
        else:
            targets = await orchestrator.find_authority_gap_targets(conn, intent, evaluation_at=now)

    if now < intent.deadline_at and targets:
        started = time.perf_counter()
        result = await EventAuthorityService.from_runtime(runtime, config).process_batch(
            targets, mode="publication"
        )
        observed_at = dt.datetime.now(dt.timezone.utc)
        async with runtime.uow.transaction() as conn:
            remaining = await orchestrator.find_authority_gap_targets(
                conn, intent, evaluation_at=min(observed_at, intent.deadline_at)
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
        log_authority_complete(
            mode="publication",
            intent_id=intent_id,
            edition_id=intent.edition_id,
            stats=result.stats,
            remaining_gap=remaining_gap,
            duration_ms=int((time.perf_counter() - started) * 1000),
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
                f"publication intent {intent_id} authority targets are busy"
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
    async with runtime.uow.transaction() as conn:
        cursor = await conn.execute("SELECT id FROM editions ORDER BY id")
        edition_ids = [int(row[0]) for row in await cursor.fetchall()]
    for edition_id in edition_ids:
        await request_background_authority_dispatch(edition_id)
