"""Optional assignment-scoped Rich Analysis jobs."""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid

from src.ai_providers import classify_provider_failure, create_provider
from src.config_loader import load_config
from src.jobs.app import procrastinate_app
from src.processing.event_analysis import EventAnalysisService
from src.processing.retry_policy import decide_retry
from src.repositories.event_analysis_runs import EventAnalysisRunRepository
from src.repositories.event_authority import EventAuthorityRepository
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.event_processing_claims import EventProcessingClaimRepository
from src.repositories.event_retries import EventProcessingRetryRepository
from src.repositories.fragments import FragmentRepository
from src.repositories.stories import StoryRepository
from src.runtime import get_runtime

logger = logging.getLogger(__name__)
ENRICHMENT_PRIORITY = -10


@procrastinate_app.task(
    queue="processing",
    name="process_event_enrichment",
    lock="event-enrichment:{story_id}:{assignment_id}",
)
async def process_event_enrichment_task(story_id: int, assignment_id: int) -> None:
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    cfg = config.settings.event_pipeline
    now = dt.datetime.now(dt.timezone.utc)
    cluster_repo = EventClusterRepository()
    retry_repo = EventProcessingRetryRepository()
    analysis_runs_repo = EventAnalysisRunRepository()
    claim_repo = EventProcessingClaimRepository()
    owner_id = f"enrichment-{os.getpid()}-{uuid.uuid4().hex}"
    retry_state = None

    async with runtime.uow.transaction() as conn:
        state = await cluster_repo.get_cluster_state(conn, story_id)
        if state is None or state.latest_assignment_id != assignment_id:
            return
        retry_state = (
            await retry_repo.get_for_assignments(
                conn, [(story_id, assignment_id)], stage="analysis"
            )
        ).get((story_id, assignment_id))
        if retry_state is not None and retry_state.exhausted_at is not None:
            await cluster_repo.mark_cluster_processed_without_analysis(
                conn, story_id=story_id, assignment_id=assignment_id
            )
            return
        if retry_state is not None and retry_state.next_retry_at is not None:
            if retry_state.next_retry_at > now:
                return
        if state.last_analyzed_at is not None and (now - state.last_analyzed_at) < dt.timedelta(
            seconds=cfg.analysis_min_interval_seconds
        ):
            await cluster_repo.mark_cluster_processed_without_analysis(
                conn, story_id=story_id, assignment_id=assignment_id
            )
            return
        new_fragment_count = await cluster_repo.count_fragments_after_assignment(
            conn, story_id=story_id, last_assignment_id=state.last_analyzed_assignment_id
        )
        if new_fragment_count < cfg.analysis_min_new_fragments:
            await cluster_repo.mark_cluster_processed_without_analysis(
                conn, story_id=story_id, assignment_id=assignment_id
            )
            return
        calls_last_hour = await analysis_runs_repo.count_calls_since(
            conn, story_id=story_id, since=now - dt.timedelta(hours=1)
        )
        if calls_last_hour >= cfg.analysis_max_calls_per_story_per_hour:
            await cluster_repo.mark_cluster_processed_without_analysis(
                conn, story_id=story_id, assignment_id=assignment_id
            )
            return
        claim = await claim_repo.try_claim_stage(
            conn,
            story_id=story_id,
            latest_assignment_id=assignment_id,
            stage="analysis",
            owner_id=owner_id,
            ttl_seconds=cfg.event_processing_stage_lease_seconds,
        )
    if claim is None:
        return

    provider = getattr(runtime, "provider_cascade", None) or create_provider(
        config.settings.ai_provider,
        logger,
        openai_api_key=config.openai_api_key,
        anthropic_api_key=config.anthropic_api_key,
        google_api_key=config.gemini_api_key,
        openrouter_api_key=config.openrouter_api_key,
        openrouter_model=config.openrouter_model,
        openrouter_model_2=getattr(config, "openrouter_model_2", ""),
        openrouter_models=getattr(config, "openrouter_models", None),
    )
    analysis_service = EventAnalysisService(
        ai_cascade=provider,
        cluster_repo=cluster_repo,
        story_repo=StoryRepository(),
        fragment_repo=FragmentRepository(),
        model=config.settings.ai_model,
        uow=runtime.uow,
        max_output_tokens=cfg.analysis_max_output_tokens,
        reasoning_effort=cfg.analysis_reasoning_effort,
    )
    try:
        outcome = await analysis_service.analyze_story_outcome(
            None,
            story_id,
            max_representative_fragments=cfg.representative_fragment_limit,
            max_input_chars=cfg.analysis_max_input_chars,
        )
        async with runtime.uow.transaction() as conn:
            if (
                await claim_repo.renew_stage(
                    conn, claim, ttl_seconds=cfg.event_processing_stage_lease_seconds
                )
                is None
            ):
                return
            if outcome.succeeded or outcome.error_kind in {
                "no_evidence",
                "story_not_found",
                "superseded",
            }:
                await retry_repo.clear(
                    conn,
                    story_id=story_id,
                    latest_assignment_id=assignment_id,
                    stage="analysis",
                )
                if not outcome.succeeded and outcome.error_kind in {
                    "no_evidence",
                    "story_not_found",
                }:
                    await cluster_repo.mark_cluster_processed_without_analysis(
                        conn, story_id=story_id, assignment_id=assignment_id
                    )
                return
            previous_attempts = retry_state.attempt_count if retry_state is not None else 0
            retry = decide_retry(
                outcome.error_kind or classify_provider_failure(RuntimeError("analysis failed")),
                attempt_count=previous_attempts + 1,
                max_attempts=cfg.analysis_max_attempts_per_assignment,
                base_backoff_seconds=cfg.provider_retry_backoff_seconds,
                max_backoff_seconds=cfg.provider_retry_backoff_max_seconds,
                now=now,
            )
            await retry_repo.record_failure(
                conn,
                story_id=story_id,
                latest_assignment_id=assignment_id,
                stage="analysis",
                error_kind=outcome.error_kind or "other",
                next_retry_at=retry.next_retry_at,
                exhausted=retry.exhausted,
                prompt_hash=outcome.prompt_hash,
            )
            if retry.exhausted:
                await cluster_repo.mark_cluster_processed_without_analysis(
                    conn, story_id=story_id, assignment_id=assignment_id
                )
    finally:
        async with runtime.uow.transaction() as conn:
            await claim_repo.release_stage(conn, claim)


async def defer_event_enrichment(story_id: int, assignment_id: int) -> None:
    """Queue one enrichment assignment idempotently."""
    from procrastinate.exceptions import AlreadyEnqueued

    try:
        await process_event_enrichment_task.configure(
            priority=ENRICHMENT_PRIORITY,
            queueing_lock=f"event-enrichment:{story_id}:{assignment_id}",
        ).defer_async(story_id=story_id, assignment_id=assignment_id)
    except AlreadyEnqueued:
        return


@procrastinate_app.periodic(cron="*/5 * * * *", periodic_id="periodic-event-enrichment-dispatch")
@procrastinate_app.task(
    queue="maintenance",
    name="periodic_event_enrichment_dispatch",
    queueing_lock="periodic-event-enrichment-dispatch",
)
async def periodic_event_enrichment_dispatch(timestamp: int) -> None:
    del timestamp
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    now = dt.datetime.now(dt.timezone.utc)
    async with runtime.uow.transaction() as conn:
        due = await EventAuthorityRepository().list_due_enrichment_assignments(
            conn,
            now=now,
            limit=config.settings.event_pipeline.rich_analysis_max_calls_per_cycle,
        )
    for story_id, assignment_id in due:
        await defer_event_enrichment(story_id, assignment_id)
