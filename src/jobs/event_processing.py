"""Procrastinate tasks for Event-First pipeline processing and coalescing."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import uuid
from typing import Any

from src.ai_providers import create_provider
from src.config_loader import load_config
from src.embedding_providers import create_embedding_provider
from src.jobs.app import procrastinate_app
from src.processing.edition_scope import resolve_edition_scope, scope_config_hash
from src.processing.embeddings import EmbeddingService
from src.processing.event_analysis import EventAnalysisService
from src.processing.event_brief import EventBriefService
from src.processing.event_clustering import EventClusteringService
from src.processing.event_triage import StoryTriageService
from src.processing.fragments import split_into_fragments
from src.processing.retry_policy import decide_retry
from src.repositories.event_analysis_runs import EventAnalysisRunRepository
from src.repositories.event_clusters import EventClusterRepository, StoryClusterState
from src.repositories.event_processing_claims import (
    EventProcessingClaimRepository,
    EventProcessingCycleClaim,
)
from src.repositories.event_retries import EventProcessingRetryRepository
from src.repositories.fragments import FragmentRepository
from src.repositories.stories import StoryRepository
from src.runtime import get_runtime

logger = logging.getLogger(__name__)


@procrastinate_app.task(queue="processing", name="process_event_revisions")
async def process_event_revisions_task(revision_ids: list[int]) -> dict[str, int]:
    """Ingest and cluster a batch of source item revisions."""
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    cfg = config.settings.event_pipeline
    emb_cfg = config.embedding

    frag_repo = FragmentRepository()
    cluster_repo = EventClusterRepository()
    story_repo = StoryRepository()
    emb_service = EmbeddingService()
    clustering_service = EventClusteringService(cluster_repo=cluster_repo, story_repo=story_repo)

    emb_provider = create_embedding_provider(config, logger)

    stats = {"revisions": len(revision_ids), "fragments": 0, "candidates": 0, "assignments": 0}

    async with runtime.uow.transaction() as conn:
        # 1. Load revisions
        cursor = await conn.execute(
            """
            SELECT sir.id, sir.text_content, COALESCE(si.first_collected_at, now()), COALESCE(se.edition_id, 1)
            FROM source_item_revisions sir
            JOIN source_items si ON si.id = sir.source_item_id
            JOIN sources s ON s.id = si.source_id
            LEFT JOIN source_editions se ON se.source_id = s.id
            WHERE sir.id = ANY(%s)
            ORDER BY sir.id ASC
            """,
            (revision_ids,),
        )

        rows = await cursor.fetchall()
        all_candidate_frags = []
        frag_meta: dict[int, tuple[int, dt.datetime]] = {}  # frag_id -> (edition_id, timestamp)

        for row in rows:
            rev_id = int(row[0])
            raw_text = str(row[1]) if row[1] is not None else ""
            collected_at = row[2]
            edition_id = int(row[3])

            new_frags = split_into_fragments(
                raw_text,
                max_chars=cfg.fragment_max_chars,
            )

            persisted = await frag_repo.create_fragments(conn, rev_id, new_frags)
            stats["fragments"] += len(persisted)

            for f in persisted:
                if f.is_candidate:
                    stats["candidates"] += 1
                    all_candidate_frags.append(f)
                    frag_meta[f.id] = (edition_id, collected_at)

        if not all_candidate_frags:
            return stats

        # 2. Embed candidate fragments with deduplication
        embeddings_map = await emb_service.ensure_fragment_embeddings(
            conn,
            all_candidate_frags,
            provider=emb_provider,
            provider_name=emb_cfg.provider,
            model=emb_cfg.model,
            dimensions=emb_cfg.dimensions,
            batch_size=cfg.embedding_batch_size,
        )

        # 3. Stream each candidate fragment into story clustering
        for f in all_candidate_frags:
            if f.id not in embeddings_map:
                continue
            sfe_id, vec = embeddings_map[f.id]
            edition_id, collected_at = frag_meta[f.id]

            await clustering_service.process_fragment(
                conn,
                f,
                edition_id=edition_id,
                fragment_embedding_id=sfe_id,
                vector=vec,
                model=emb_cfg.model,
                dimensions=emb_cfg.dimensions,
                item_timestamp=collected_at,
                join_similarity=cfg.join_similarity,
                active_window_hours=cfg.active_window_hours,
                max_cluster_candidates=cfg.max_cluster_candidates,
            )
            stats["assignments"] += 1

    if stats["assignments"] > 0:
        try:
            await coalesce_dirty_stories_task.defer_async()
        except Exception as exc:
            logger.warning("could not defer coalesce_dirty_stories_task: %s", exc)

    return stats


@procrastinate_app.task(queue="processing", name="coalesce_dirty_stories")
async def coalesce_dirty_stories_task(
    edition_id: int | None = None,
    force_settled: bool = False,
    story_ids: list[int] | None = None,
) -> dict[str, int]:
    """Coalesce dirty story clusters, triage/scope them in batches, and route to brief or rich analysis."""
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    cfg = config.settings.event_pipeline

    cluster_repo = EventClusterRepository()
    story_repo = StoryRepository()
    fragment_repo = FragmentRepository()
    retry_repo = EventProcessingRetryRepository()
    analysis_runs_repo = EventAnalysisRunRepository()
    claim_repo = EventProcessingClaimRepository()

    ai_provider = getattr(runtime, "provider_cascade", None) or create_provider(
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

    triage_service = StoryTriageService(
        ai_cascade=ai_provider,
        cluster_repo=cluster_repo,
        model=config.settings.ai_model,
        uow=runtime.uow,
        max_output_tokens=cfg.triage_max_output_tokens,
        reasoning_effort=cfg.triage_reasoning_effort,
    )
    analysis_service = EventAnalysisService(
        ai_cascade=ai_provider,
        cluster_repo=cluster_repo,
        story_repo=story_repo,
        fragment_repo=fragment_repo,
        model=config.settings.ai_model,
        uow=runtime.uow,
        max_output_tokens=cfg.analysis_max_output_tokens,
        reasoning_effort=cfg.analysis_reasoning_effort,
    )
    brief_service = EventBriefService(
        story_repo=story_repo,
        cluster_repo=cluster_repo,
    )

    now = dt.datetime.now(dt.timezone.utc)
    stats = {
        "scanned": 0,
        "settled": 0,
        "gated": 0,
        "scope_local": 0,
        "scope_direct_impact": 0,
        "scope_out_of_scope": 0,
        "scope_uncertain": 0,
        "deferred": 0,
        "retry_backoff_skipped": 0,
        "retry_exhausted_skipped": 0,
        "triage_failures": 0,
        "analysis_failures": 0,
        "analysis_backoff_skipped": 0,
        "analysis_exhausted_skipped": 0,
        "analysis_budget_skipped": 0,
        "cycle_lease_skipped": 0,
        "cycle_lease_lost": 0,
        "stage_claim_skipped": 0,
        "triaged": 0,
        "analyzed": 0,
    }

    async with runtime.uow.transaction() as conn:
        if edition_id is not None:
            editions_to_process = [edition_id]
        else:
            editions_to_process = await cluster_repo.list_dirty_edition_ids(conn)

    if not editions_to_process:
        return stats
    triage_concurrency_str = os.environ.get("EVENT_PROCESSING_CONCURRENCY", "4")
    try:
        triage_concurrency = max(1, int(triage_concurrency_str))
    except (ValueError, TypeError):
        triage_concurrency = 4
    triage_sem = asyncio.Semaphore(triage_concurrency)

    async def _process_gate_batch(
        gate_batch: list[StoryClusterState],
        cur_ed_id: int,
        sc_cfg: Any,
        sc_hsh: str,
        cycle_claim: EventProcessingCycleClaim,
    ) -> dict[str, Any]:
        b_stats = {
            "gated": len(gate_batch),
            "triaged": 0,
            "deferred": 0,
            "retry_backoff_skipped": 0,
            "retry_exhausted_skipped": 0,
            "triage_failures": 0,
            "analysis_failures": 0,
            "analysis_backoff_skipped": 0,
            "analysis_exhausted_skipped": 0,
            "analysis_budget_skipped": 0,
            "cycle_lease_lost": 0,
            "stage_claim_skipped": 0,
            "scope_local": 0,
            "scope_direct_impact": 0,
            "scope_out_of_scope": 0,
            "scope_uncertain": 0,
            "analyzed": 0,
        }
        async with runtime.uow.transaction() as lease_conn:
            renewed_claim = await claim_repo.renew_cycle(
                lease_conn,
                cycle_claim,
                ttl_seconds=cfg.event_processing_cycle_lease_seconds,
            )
        if renewed_claim is None:
            b_stats["cycle_lease_lost"] = 1
            b_stats["deferred"] = len(gate_batch)
            return b_stats
        async with triage_sem:
            batch_result = await triage_service.triage_stories_batch(
                None,
                gate_batch,
                edition_id=cur_ed_id,
                scope_config=sc_cfg,
                scope_hash=sc_hsh,
                excerpt_chars=cfg.triage_excerpt_chars,
                min_ignore_confidence=cfg.triage_min_ignore_confidence,
            )
            b_stats["triaged"] = len(batch_result.results)
            results_by_id = {item.story_id: item for item in batch_result.results}

            deferred_set = set(batch_result.deferred_story_ids)
            if deferred_set:
                async with runtime.uow.transaction() as retry_conn:
                    retry_states = await retry_repo.get_for_assignments(
                        retry_conn,
                        [
                            (state.story_id, state.latest_assignment_id)
                            for state in gate_batch
                            if state.story_id in deferred_set
                        ],
                        stage="triage",
                    )
                    error_kind = batch_result.batch_error_kind or "other"
                    for state in gate_batch:
                        if state.story_id not in deferred_set:
                            continue
                        previous = retry_states.get((state.story_id, state.latest_assignment_id))
                        attempt_count = (previous.attempt_count if previous else 0) + 1
                        decision = decide_retry(
                            error_kind,
                            attempt_count=attempt_count,
                            max_attempts=cfg.triage_max_attempts_per_assignment,
                            base_backoff_seconds=cfg.provider_retry_backoff_seconds,
                            max_backoff_seconds=cfg.provider_retry_backoff_max_seconds,
                            now=now,
                        )
                        await retry_repo.record_failure(
                            retry_conn,
                            story_id=state.story_id,
                            latest_assignment_id=state.latest_assignment_id,
                            stage="triage",
                            error_kind=error_kind,
                            next_retry_at=decision.next_retry_at,
                            exhausted=decision.exhausted,
                            prompt_hash=batch_result.prompt_hash,
                        )
                        b_stats["triage_failures"] += 1

            stories_to_analyze: list[StoryClusterState] = []

            async with runtime.uow.transaction() as persist_conn:
                analysis_retry_states = await retry_repo.get_for_assignments(
                    persist_conn,
                    [(state.story_id, state.latest_assignment_id) for state in gate_batch],
                    stage="analysis",
                )
                for state in gate_batch:
                    if state.story_id in batch_result.deferred_story_ids:
                        b_stats["deferred"] += 1
                        continue

                    result = results_by_id.get(state.story_id)
                    if result is None:
                        b_stats["deferred"] += 1
                        continue

                    await retry_repo.clear(
                        persist_conn,
                        story_id=state.story_id,
                        latest_assignment_id=state.latest_assignment_id,
                        stage="triage",
                    )

                    if result.scope == "OUT_OF_SCOPE":
                        b_stats["scope_out_of_scope"] += 1
                        await cluster_repo.mark_cluster_processed_without_analysis(
                            persist_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )
                        continue

                    if result.scope == "UNCERTAIN":
                        b_stats["scope_uncertain"] += 1
                        await cluster_repo.mark_cluster_processed_without_analysis(
                            persist_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )
                        continue

                    if result.scope == "LOCAL":
                        b_stats["scope_local"] += 1
                    elif result.scope == "DIRECT_IMPACT":
                        b_stats["scope_direct_impact"] += 1
                    else:
                        raise AssertionError(f"validated unexpected scope {result.scope!r}")

                    if result.retention == "DROP":
                        await cluster_repo.mark_cluster_processed_without_analysis(
                            persist_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )
                        continue

                    # In-scope KEEP: persist brief revision first
                    await brief_service.persist_brief(
                        persist_conn,
                        story_id=state.story_id,
                        assignment_id=state.latest_assignment_id,
                        payload=result.brief_payload,
                    )

                    effective_enrichment = result.enrichment
                    if (
                        state.fragment_count >= cfg.direct_analysis_min_fragments
                        and state.unique_source_count >= cfg.direct_analysis_min_unique_sources
                    ):
                        effective_enrichment = "ANALYZE"

                    if effective_enrichment == "BRIEF":
                        await cluster_repo.mark_cluster_processed_without_analysis(
                            persist_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )
                        continue

                    # effective_enrichment == "ANALYZE"
                    analysis_retry_state = analysis_retry_states.get(
                        (state.story_id, state.latest_assignment_id)
                    )
                    if (
                        analysis_retry_state is not None
                        and analysis_retry_state.exhausted_at is not None
                    ):
                        b_stats["analysis_exhausted_skipped"] += 1
                        await cluster_repo.mark_cluster_processed_without_analysis(
                            persist_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )
                        continue
                    if (
                        analysis_retry_state is not None
                        and analysis_retry_state.next_retry_at is not None
                        and analysis_retry_state.next_retry_at > now
                    ):
                        b_stats["analysis_backoff_skipped"] += 1
                        continue

                    min_interval_delta = dt.timedelta(seconds=cfg.analysis_min_interval_seconds)
                    if (
                        state.last_analyzed_at is not None
                        and (now - state.last_analyzed_at) < min_interval_delta
                    ):
                        await cluster_repo.mark_cluster_processed_without_analysis(
                            persist_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )
                        continue

                    new_fragment_count = await cluster_repo.count_fragments_after_assignment(
                        persist_conn,
                        story_id=state.story_id,
                        last_assignment_id=state.last_analyzed_assignment_id,
                    )
                    if new_fragment_count < cfg.analysis_min_new_fragments:
                        await cluster_repo.mark_cluster_processed_without_analysis(
                            persist_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )
                        continue

                    calls_last_hour = await analysis_runs_repo.count_calls_since(
                        persist_conn,
                        story_id=state.story_id,
                        since=now - dt.timedelta(hours=1),
                    )
                    if calls_last_hour >= cfg.analysis_max_calls_per_story_per_hour:
                        await cluster_repo.mark_cluster_processed_without_analysis(
                            persist_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )
                        continue

                    if not await claim_repo.claim_rich_slot(
                        persist_conn,
                        cycle_claim,
                        max_calls=cfg.rich_analysis_max_calls_per_cycle,
                        ttl_seconds=cfg.event_processing_cycle_lease_seconds,
                    ):
                        b_stats["analysis_budget_skipped"] += 1
                        continue

                    stories_to_analyze.append(state)

            for state in stories_to_analyze:
                outcome = await analysis_service.analyze_story_outcome(
                    None,
                    state.story_id,
                    max_representative_fragments=cfg.representative_fragment_limit,
                    max_input_chars=cfg.analysis_max_input_chars,
                )
                if outcome.succeeded:
                    b_stats["analyzed"] += 1
                    async with runtime.uow.transaction() as success_conn:
                        await retry_repo.clear(
                            success_conn,
                            story_id=state.story_id,
                            latest_assignment_id=state.latest_assignment_id,
                            stage="analysis",
                        )
                    continue

                b_stats["analysis_failures"] += 1
                previous = analysis_retry_states.get((state.story_id, state.latest_assignment_id))
                attempt_count = (previous.attempt_count if previous else 0) + 1
                decision = decide_retry(
                    outcome.error_kind or "other",
                    attempt_count=attempt_count,
                    max_attempts=cfg.analysis_max_attempts_per_assignment,
                    base_backoff_seconds=cfg.provider_retry_backoff_seconds,
                    max_backoff_seconds=cfg.provider_retry_backoff_max_seconds,
                    now=now,
                )
                async with runtime.uow.transaction() as failure_conn:
                    await retry_repo.record_failure(
                        failure_conn,
                        story_id=state.story_id,
                        latest_assignment_id=state.latest_assignment_id,
                        stage="analysis",
                        error_kind=outcome.error_kind or "other",
                        next_retry_at=decision.next_retry_at,
                        exhausted=decision.exhausted,
                        prompt_hash=outcome.prompt_hash,
                    )
                    if decision.exhausted:
                        await cluster_repo.mark_cluster_processed_without_analysis(
                            failure_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )

        return b_stats

    for current_edition_id in editions_to_process:
        owner_id = f"pid-{os.getpid()}-{uuid.uuid4().hex}"
        async with runtime.uow.transaction() as claim_conn:
            cycle_claim = await claim_repo.acquire_cycle(
                claim_conn,
                edition_id=current_edition_id,
                owner_id=owner_id,
                ttl_seconds=cfg.event_processing_cycle_lease_seconds,
            )
        if cycle_claim is None:
            stats["cycle_lease_skipped"] += 1
            continue

        async with runtime.uow.transaction() as conn:
            fetch_limit = (
                max(cfg.live_batch_size, len(story_ids)) if story_ids else cfg.live_batch_size
            )
            dirty_stories = await cluster_repo.list_dirty_cluster_states(
                conn,
                current_edition_id,
                limit=fetch_limit,
                story_ids=story_ids,
            )
            _slug, scope_config = await resolve_edition_scope(conn, config, current_edition_id)
            scope_hash = scope_config_hash(scope_config)

        stats["scanned"] += len(dirty_stories)
        if not dirty_stories:
            async with runtime.uow.transaction() as release_conn:
                await claim_repo.release_cycle(release_conn, cycle_claim)
            continue

        # Filter settled stories: quiet window has passed since last fragment arrived (unless force_settled)
        if force_settled:
            settled = dirty_stories
        else:
            quiet_delta = dt.timedelta(seconds=cfg.analysis_quiet_seconds)
            settled = [s for s in dirty_stories if (now - s.last_seen_at) >= quiet_delta]

        stats["settled"] += len(settled)
        if not settled:
            async with runtime.uow.transaction() as release_conn:
                await claim_repo.release_cycle(release_conn, cycle_claim)
            continue

        async with runtime.uow.transaction() as retry_conn:
            retry_states = await retry_repo.get_for_assignments(
                retry_conn,
                [(state.story_id, state.latest_assignment_id) for state in settled],
                stage="triage",
            )

        eligible: list[StoryClusterState] = []
        for state in settled:
            retry_state = retry_states.get((state.story_id, state.latest_assignment_id))
            if retry_state is not None and retry_state.exhausted_at is not None:
                stats["retry_exhausted_skipped"] += 1
                continue
            if (
                retry_state is not None
                and retry_state.next_retry_at is not None
                and retry_state.next_retry_at > now
            ):
                stats["retry_backoff_skipped"] += 1
                continue
            eligible.append(state)

        if not eligible:
            async with runtime.uow.transaction() as release_conn:
                await claim_repo.release_cycle(release_conn, cycle_claim)
            continue

        # Prepare batches and run concurrently with Semaphore(4)
        batches = [
            eligible[start : start + cfg.triage_batch_size]
            for start in range(0, len(eligible), cfg.triage_batch_size)
        ]
        batch_results = await asyncio.gather(
            *[
                _process_gate_batch(
                    b,
                    current_edition_id,
                    scope_config,
                    scope_hash,
                    cycle_claim,
                )
                for b in batches
            ]
        )

        for b_stat in batch_results:
            for k in (
                "gated",
                "triaged",
                "deferred",
                "retry_backoff_skipped",
                "retry_exhausted_skipped",
                "triage_failures",
                "analysis_failures",
                "analysis_backoff_skipped",
                "analysis_exhausted_skipped",
                "analysis_budget_skipped",
                "cycle_lease_lost",
                "stage_claim_skipped",
                "scope_local",
                "scope_direct_impact",
                "scope_out_of_scope",
                "scope_uncertain",
                "analyzed",
            ):
                stats[k] += b_stat[k]

        async with runtime.uow.transaction() as release_conn:
            await claim_repo.release_cycle(release_conn, cycle_claim)

    return stats


@procrastinate_app.periodic(cron="*/5 * * * *", periodic_id="periodic-coalesce-dirty-stories")
@procrastinate_app.task(queue="maintenance", queueing_lock="coalesce_dirty_stories")
async def periodic_coalesce_dirty_stories(timestamp: int) -> None:
    """Periodically coalesce settled dirty stories every 5 minutes."""
    del timestamp
    try:
        await coalesce_dirty_stories_task.defer_async()
    except Exception as exc:
        logger.warning("periodic coalesce_dirty_stories deferral failed: %s", exc)
