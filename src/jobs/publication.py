"""Procrastinate publication background jobs (Plan 4)."""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

import procrastinate

from src.jobs.app import procrastinate_app
from src.runtime import get_runtime

logger = logging.getLogger(__name__)

SELECT_STORIES_TASK_NAME = "select_stories_for_publication"
GENERATE_PUBLICATION_TASK_NAME = "generate_publication"
PREPARE_DELIVERY_PAYLOADS_TASK_NAME = "prepare_delivery_payloads"
DELIVER_PAYLOAD_TASK_NAME = "deliver_publication_payload"
PREPARE_PUBLICATION_FROM_INTENT_TASK_NAME = "prepare_publication_from_intent"

PUBLICATION_QUEUE = "publication"

PUBLICATION_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=3,
    wait=30,
    linear_wait=60,
)


def _is_preview_request_key(request_key: str | None) -> bool:
    """Return whether a durable request key belongs to an in-process preview."""
    return bool(
        request_key
        and (
            request_key.startswith("preview:")
            or request_key.startswith("publication-intent:preview:")
        )
    )


def _is_preview_run(run: Any) -> bool:
    """Identify preview runs, including rows created by older deployments."""
    metadata = getattr(run, "metadata", None)
    return bool(
        isinstance(metadata, dict) and metadata.get("preview") is True
    ) or _is_preview_request_key(getattr(run, "request_key", None))


async def _load_publication_run(run_id: int) -> Any | None:
    """Read a publication run before a worker task performs side effects."""
    from src.publication.repository import PublicationRepository

    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        return await PublicationRepository().get_run_by_id(conn, run_id)


DIGEST_PUBLICATION_TYPE = "digest_grouped"
RETRY_DIGEST_PUBLICATION_TASK_NAME = "retry_digest_publication"
# Automatic retries of a failed scheduled digest slot. The first attempt is the
# scheduled one; each retry gets a fresh request key and the same target slot.
DIGEST_RETRY_MAX_ATTEMPTS = 3
DIGEST_RETRY_DELAY_SECONDS = 20 * 60
# Do not start an attempt this close to the readiness deadline.
DIGEST_RETRY_MIN_LEAD_SECONDS = 10 * 60
_RETRY_KEY_PREFIX_RE = re.compile(r"^retry-(\d+):")


def digest_retry_attempt(request_key: str) -> int:
    """Return 1 for a scheduled request key and N for a `retry-N:` key."""
    match = _RETRY_KEY_PREFIX_RE.match(request_key)
    return int(match.group(1)) if match else 1


def digest_retry_base_key(request_key: str) -> str:
    """Return the scheduled request key without any `retry-N:` prefix."""
    return _RETRY_KEY_PREFIX_RE.sub("", request_key, count=1)


def digest_retry_delay_seconds(
    *, attempt: int, now: dt.datetime, deadline_at: dt.datetime
) -> float | None:
    """Seconds to wait before attempt `attempt + 1`, or None when no attempt fits."""
    if attempt >= DIGEST_RETRY_MAX_ATTEMPTS:
        return None
    last_start = deadline_at - dt.timedelta(seconds=DIGEST_RETRY_MIN_LEAD_SECONDS)
    if now + dt.timedelta(seconds=DIGEST_RETRY_DELAY_SECONDS) > last_start:
        return None
    return float(DIGEST_RETRY_DELAY_SECONDS)


@procrastinate_app.task(
    name=SELECT_STORIES_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    lock="select_stories_for_publication:{run_id}",
    pass_context=True,
)
async def select_stories_for_publication(context: Any, run_id: int) -> None:
    """Select stories from sealed candidates."""
    from src.publication.selection import EditorialSelectionService

    runtime = get_runtime()
    run = await _load_publication_run(run_id)
    if run is not None and _is_preview_run(run):
        logger.warning("Skipping queued selection for preview publication run %s", run_id)
        return
    service = EditorialSelectionService(uow=runtime.uow)
    await service.select(run_id)


@procrastinate_app.task(
    name=GENERATE_PUBLICATION_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    lock="generate_publication:{run_id}",
    pass_context=True,
)
async def generate_publication(context: Any, run_id: int) -> None:
    """Generate publication from sealed inputs."""
    from src.publication.errors import ArticlePublicationRejected, PublicationGenerationError
    from src.publication.generation import PublicationGenerationService

    runtime = get_runtime()
    run = await _load_publication_run(run_id)
    if run is not None and _is_preview_run(run):
        logger.warning("Skipping queued generation for preview publication run %s", run_id)
        return
    if run is not None and getattr(run, "status", None) == "failed":
        logger.warning("Skipping queued generation for already failed publication run %s", run_id)
        return
    service = PublicationGenerationService(uow=runtime.uow)
    try:
        await service.generate(run_id)
    except ArticlePublicationRejected as exc:
        logger.warning(
            "publication run %s ended with terminal article rejection: %s",
            run_id,
            exc.error_kind,
        )
        await _mark_publication_intent_failed(run, error_kind=exc.error_kind)
        return
    except PublicationGenerationError as exc:
        logger.error("publication run %s ended with terminal generation failure: %s", run_id, exc)
        error_kind = getattr(exc, "error_kind", type(exc).__name__)
        retried = await _schedule_digest_retry(run)
        await _mark_publication_intent_failed(run, error_kind=error_kind, notify=not retried)
        return


@procrastinate_app.task(
    name=PREPARE_DELIVERY_PAYLOADS_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    lock="prepare_delivery_payloads:{publication_id}",
    pass_context=True,
)
async def prepare_delivery_payloads(context: Any, publication_id: int) -> None:
    """Prepare immutable delivery payloads for a created publication."""
    from src.publication.delivery import PublicationDeliveryService

    runtime = get_runtime()
    service = PublicationDeliveryService(uow=runtime.uow)
    await service.prepare_payloads(publication_id)


@procrastinate_app.task(
    name=DELIVER_PAYLOAD_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    lock="deliver_publication_payload:{delivery_id}",
    pass_context=True,
)
async def deliver_publication_payload(context: Any, delivery_id: int) -> None:
    """Deliver a prepared publication payload."""
    from src.publication.delivery import PublicationDeliveryService

    runtime = get_runtime()
    service = PublicationDeliveryService(uow=runtime.uow)
    await service.deliver(delivery_id)


@procrastinate_app.task(
    name=PREPARE_PUBLICATION_FROM_INTENT_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    retry=PUBLICATION_RETRY_STRATEGY,
    pass_context=True,
)
async def prepare_publication_from_intent(context: Any, intent_id: int) -> None:
    """Create a PublicationRun only after a durable intent is ready."""
    from src.publication.readiness_repository import PublicationReadinessRepository

    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        intent = await PublicationReadinessRepository().get_refresh_run(conn, intent_id)
    if intent is not None and _is_preview_request_key(getattr(intent, "request_key", None)):
        logger.warning("Skipping queued preparation for preview intent %s", intent_id)
        return
    try:
        await _prepare_publication_from_intent_once(intent_id)
    except Exception as exc:
        if _is_final_publication_attempt(context):
            await _mark_preparation_failed(
                intent_id,
                error_kind=getattr(exc, "error_kind", None),
            )
        raise


async def _prepare_publication_from_intent_once(
    intent_id: int,
    *,
    defer_selection: bool = True,
    preview: bool = False,
    config: Any | None = None,
) -> int | None:
    """Create a PublicationRun only after a durable intent is ready."""
    from src.config_loader import load_config
    from src.publication.policies import PublicationPolicyService
    from src.publication.readiness_repository import PublicationReadinessRepository
    from src.publication.snapshot import PublicationSnapshotService
    from src.repositories.editions import EditionRepository

    runtime = get_runtime()
    config = config or load_config()

    readiness_repo = PublicationReadinessRepository()
    service = PublicationSnapshotService(uow=runtime.uow)

    async with runtime.uow.transaction() as conn:
        refresh = await readiness_repo.get_refresh_run(conn, intent_id, for_update=True)
        if refresh is None:
            raise ValueError(f"publication intent {intent_id} not found")
        if refresh.status == "publication_queued":
            return refresh.publication_run_id
        if refresh.status != "preparing":
            raise ValueError(f"publication intent {intent_id} is not preparing: {refresh.status}")
        edition = await EditionRepository().get_by_id(conn, refresh.edition_id)
        if edition is None:
            raise ValueError(f"edition {refresh.edition_id} not found")
        policy_set = await PublicationPolicyService().ensure_current(
            conn,
            edition_id=refresh.edition_id,
            publication_type=refresh.publication_type,
            config=config,
            lookback_hours_override=refresh.lookback_hours,
        )

    source_cutoff_at = refresh.normal_source_cutoff_at
    slot_key = refresh.slot_at.astimezone(dt.timezone.utc).isoformat()
    req_key = f"publication-intent:{refresh.request_key}"
    run_metadata = {
        "refresh_run_id": refresh.id,
        "scheduled_slot_at": slot_key,
        "intent_request_key": refresh.request_key,
        "trigger": refresh.trigger,
    }
    if preview:
        run_metadata["preview"] = True

    async with runtime.uow.transaction() as conn:
        current_refresh = await readiness_repo.get_refresh_run(conn, refresh.id, for_update=True)
        if current_refresh is None:
            raise ValueError(f"refresh run {refresh.id} not found")
        if current_refresh.status == "publication_queued":
            return current_refresh.publication_run_id
        if current_refresh.status != "preparing":
            raise ValueError(f"refresh run {refresh.id} is not preparing")
        if not hasattr(current_refresh, "knowledge_snapshot_at"):
            knowledge_snapshot_at = current_refresh.normal_source_cutoff_at
        elif current_refresh.knowledge_snapshot_at is None:
            raise ValueError(
                f"refresh run {current_refresh.id} is preparing without knowledge_snapshot_at"
            )
        else:
            knowledge_snapshot_at = current_refresh.knowledge_snapshot_at
        run = await service.create_run(
            edition_id=current_refresh.edition_id,
            publication_type=current_refresh.publication_type,
            source_cutoff_at=source_cutoff_at,
            snapshot_at=knowledge_snapshot_at,
            request_key=req_key,
            policy_ids=policy_set,
            config=config,
            metadata=run_metadata,
            conn=conn,
        )
        await service.seal_candidates(run.id, conn=conn)
        prepared_at = dt.datetime.now(dt.timezone.utc)
        await readiness_repo.mark_publication_queued(
            conn,
            refresh_run_id=refresh.id,
            publication_run_id=run.id,
            prepared_at=prepared_at,
        )
        logger.info(
            "publication_refresh_prepared",
            extra={
                "edition_id": refresh.edition_id,
                "refresh_run_id": refresh.id,
                "scheduled_slot_at": slot_key,
                "source_cutoff_at": source_cutoff_at.isoformat(),
                "snapshot_at": knowledge_snapshot_at.isoformat(),
                "publication_delay_seconds": max(
                    0, (knowledge_snapshot_at - refresh.slot_at).total_seconds()
                ),
            },
        )
        if defer_selection:
            await select_stories_for_publication.configure(connection=conn).defer_async(
                run_id=run.id
            )
        return run.id


def _is_final_publication_attempt(context: Any) -> bool:
    """Match Procrastinate's retry boundary for the current task attempt."""
    attempts = getattr(getattr(context, "job", None), "attempts", None)
    maximum = PUBLICATION_RETRY_STRATEGY.max_attempts
    return isinstance(attempts, int) and maximum is not None and attempts >= maximum


async def _mark_preparation_failed(intent_id: int, *, error_kind: str | None = None) -> None:
    """Atomically close a preparation intent and persist notification outbox rows."""
    from src.config_loader import load_config
    from src.publication.notifications import PublicationFailureNotificationService
    from src.publication.readiness_repository import PublicationReadinessRepository
    from src.runtime import get_runtime

    runtime = get_runtime()
    readiness_repo = PublicationReadinessRepository()
    notification_ids: list[int] = []
    async with runtime.uow.transaction() as conn:
        refresh = await readiness_repo.get_refresh_run(conn, intent_id, for_update=True)
        if refresh is None or refresh.status != "preparing":
            return
        await readiness_repo.transition_refresh(
            conn,
            intent_id,
            status="failed",
            error_kind=error_kind or "preparation_failed",
        )
        notification_ids = await PublicationFailureNotificationService(
            config=load_config(), readiness_repo=readiness_repo
        ).enqueue_for_failed_intent(
            conn,
            intent=refresh,
            failure_kind=error_kind or "preparation_failed",
            dispatch=False,
        )

    try:
        if notification_ids:
            async with runtime.uow.transaction() as conn:
                refresh = await readiness_repo.get_refresh_run(conn, intent_id)
                if refresh is not None:
                    await PublicationFailureNotificationService(
                        config=load_config(), readiness_repo=readiness_repo
                    ).dispatch_existing(
                        conn,
                        intent=refresh,
                        notification_ids=notification_ids,
                    )
    except Exception:
        logger.exception(
            "failed to dispatch preparation failure notification for intent %s", intent_id
        )


async def _mark_publication_intent_failed(
    run: Any | None,
    *,
    error_kind: str,
    notify: bool = True,
) -> None:
    """Close the intent after terminal generation failure and notify its owner.

    With `notify=False` the intent still closes, but the owner is not told: an
    automatic retry is already scheduled for the same slot.
    """
    from src.config_loader import load_config
    from src.publication.notifications import PublicationFailureNotificationService
    from src.publication.readiness_repository import PublicationReadinessRepository

    metadata = getattr(run, "metadata", None)
    refresh_run_id = metadata.get("refresh_run_id") if isinstance(metadata, dict) else None
    if not isinstance(refresh_run_id, int):
        return

    runtime = get_runtime()
    readiness_repo = PublicationReadinessRepository()
    notification_ids: list[int] = []
    async with runtime.uow.transaction() as conn:
        refresh = await readiness_repo.get_refresh_run(conn, refresh_run_id, for_update=True)
        if refresh is None or refresh.status == "failed":
            return
        await readiness_repo.transition_refresh(
            conn,
            refresh_run_id,
            status="failed",
            error_kind=error_kind,
        )
        if notify:
            notification_ids = await PublicationFailureNotificationService(
                config=load_config(), readiness_repo=readiness_repo
            ).enqueue_for_failed_intent(
                conn,
                intent=refresh,
                failure_kind=error_kind,
                dispatch=False,
            )

    if not notification_ids:
        return
    try:
        async with runtime.uow.transaction() as conn:
            refresh = await readiness_repo.get_refresh_run(conn, refresh_run_id)
            if refresh is not None:
                await PublicationFailureNotificationService(
                    config=load_config(), readiness_repo=readiness_repo
                ).dispatch_existing(
                    conn,
                    intent=refresh,
                    notification_ids=notification_ids,
                )
    except Exception:
        logger.exception(
            "failed to dispatch generation failure notification for intent %s", refresh_run_id
        )


async def _schedule_digest_retry(run: Any | None) -> bool:
    """Defer one more attempt for a failed scheduled digest slot when one still fits."""
    from src.config_loader import load_config
    from src.publication.orchestrator import calculate_intent_times

    if run is None or getattr(run, "publication_type", None) != DIGEST_PUBLICATION_TYPE:
        return False
    metadata = getattr(run, "metadata", None)
    if not isinstance(metadata, dict) or metadata.get("trigger") != "scheduled":
        return False
    slot = metadata.get("scheduled_slot_at")
    request_key = metadata.get("intent_request_key")
    if not isinstance(slot, str) or not isinstance(request_key, str):
        return False

    config = load_config()
    target_at = dt.datetime.fromisoformat(slot)
    _cutoff, deadline_at = calculate_intent_times(
        target_at,
        freshness_ttl_minutes=config.settings.publication_freshness_ttl_minutes,
        deadline_minutes=config.settings.publication_readiness_deadline_minutes,
    )
    attempt = digest_retry_attempt(request_key)
    delay = digest_retry_delay_seconds(
        attempt=attempt,
        now=dt.datetime.now(dt.timezone.utc),
        deadline_at=deadline_at,
    )
    if delay is None:
        return False
    run_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)
    await retry_digest_publication.configure(schedule_at=run_at).defer_async(
        edition_id=int(run.edition_id),
        target_at=slot,
        attempt=attempt + 1,
        base_request_key=digest_retry_base_key(request_key),
    )
    logger.warning(
        "digest slot %s failed (attempt %s); retry %s deferred by %s seconds",
        slot,
        attempt,
        attempt + 1,
        int(delay),
    )
    return True


@procrastinate_app.task(
    name=RETRY_DIGEST_PUBLICATION_TASK_NAME,
    queue=PUBLICATION_QUEUE,
    lock="retry_digest_publication:{edition_id}:{target_at}",
)
async def retry_digest_publication(
    edition_id: int,
    target_at: str,
    attempt: int,
    base_request_key: str,
) -> None:
    """Open the next attempt for a scheduled digest slot through the orchestrator."""
    from src.config_loader import load_config
    from src.publication.orchestrator import PublicationOrchestrator, calculate_intent_times

    runtime = get_runtime()
    config = load_config()
    target = dt.datetime.fromisoformat(target_at)
    now = dt.datetime.now(dt.timezone.utc)
    _cutoff, deadline_at = calculate_intent_times(
        target,
        freshness_ttl_minutes=config.settings.publication_freshness_ttl_minutes,
        deadline_minutes=config.settings.publication_readiness_deadline_minutes,
    )
    if now + dt.timedelta(seconds=DIGEST_RETRY_MIN_LEAD_SECONDS) > deadline_at:
        logger.warning("digest retry %s for slot %s skipped: past deadline", attempt, target_at)
        return
    async with runtime.uow.transaction() as conn:
        cursor = await conn.execute("SELECT slug FROM editions WHERE id = %s", (edition_id,))
        row = await cursor.fetchone()
    if row is None:
        logger.error("digest retry for unknown edition %s", edition_id)
        return
    await PublicationOrchestrator(uow=runtime.uow, config=config).request(
        edition_slug=str(row[0]),
        publication_type=DIGEST_PUBLICATION_TYPE,
        trigger="scheduled",
        target_at=target,
        request_key=f"retry-{attempt}:{base_request_key}",
        now=now,
    )


# Kept as an import-level compatibility name for code that only imported the
# old symbol.  It intentionally has the new intent-only signature and cannot
# accept the former edition/snapshot arguments.
create_scheduled_publication = prepare_publication_from_intent
