"""Procrastinate tasks for Event-First pipeline processing and coalescing."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
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
from src.repositories.event_clusters import EventClusterRepository, StoryClusterState
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

    return stats


@procrastinate_app.task(queue="processing", name="coalesce_dirty_stories")
async def coalesce_dirty_stories_task(
    edition_id: int | None = None,
    force_settled: bool = False,
) -> dict[str, int]:
    """Coalesce dirty story clusters, triage/scope them in batches, and route to brief or rich analysis."""
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    cfg = config.settings.event_pipeline

    cluster_repo = EventClusterRepository()
    story_repo = StoryRepository()
    fragment_repo = FragmentRepository()

    ai_provider = getattr(runtime, "provider_cascade", None) or create_provider(
        config.settings.ai_provider,
        logger,
        openai_api_key=config.openai_api_key,
        anthropic_api_key=config.anthropic_api_key,
        google_api_key=config.gemini_api_key,
        openrouter_api_key=config.openrouter_api_key,
        openrouter_model=config.openrouter_model,
        openrouter_model_2=getattr(config, "openrouter_model_2", ""),
    )

    triage_service = StoryTriageService(
        ai_cascade=ai_provider,
        cluster_repo=cluster_repo,
        model=config.settings.ai_model,
    )
    analysis_service = EventAnalysisService(
        ai_cascade=ai_provider,
        cluster_repo=cluster_repo,
        story_repo=story_repo,
        fragment_repo=fragment_repo,
        model=config.settings.ai_model,
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

    triage_sem = asyncio.Semaphore(1)

    async def _process_gate_batch(
        gate_batch: list[StoryClusterState],
        cur_ed_id: int,
        sc_cfg: Any,
        sc_hsh: str,
    ) -> dict[str, Any]:
        b_stats = {
            "gated": len(gate_batch),
            "triaged": 0,
            "deferred": 0,
            "scope_local": 0,
            "scope_direct_impact": 0,
            "scope_out_of_scope": 0,
            "scope_uncertain": 0,
            "analyzed": 0,
        }
        async with triage_sem:
            async with runtime.uow.transaction() as batch_conn:
                batch_result = await triage_service.triage_stories_batch(
                    batch_conn,
                    gate_batch,
                    edition_id=cur_ed_id,
                    scope_config=sc_cfg,
                    scope_hash=sc_hsh,
                    excerpt_chars=cfg.triage_excerpt_chars,
                    min_ignore_confidence=cfg.triage_min_ignore_confidence,
                )
                b_stats["triaged"] = len(batch_result.results)
                results_by_id = {item.story_id: item for item in batch_result.results}

                for state in gate_batch:
                    if state.story_id in batch_result.deferred_story_ids:
                        b_stats["deferred"] += 1
                        continue

                    result = results_by_id.get(state.story_id)
                    if result is None:
                        b_stats["deferred"] += 1
                        continue

                    if result.scope == "OUT_OF_SCOPE":
                        b_stats["scope_out_of_scope"] += 1
                        await cluster_repo.mark_cluster_processed_without_analysis(
                            batch_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )
                        continue

                    if result.scope == "UNCERTAIN":
                        b_stats["scope_uncertain"] += 1
                        await cluster_repo.mark_cluster_processed_without_analysis(
                            batch_conn,
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
                            batch_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )
                        continue

                    # In-scope KEEP: persist brief revision first
                    await brief_service.persist_brief(
                        batch_conn,
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
                            batch_conn,
                            story_id=state.story_id,
                            assignment_id=state.latest_assignment_id,
                        )
                        continue

                    # effective_enrichment == "ANALYZE"
                    min_interval_delta = dt.timedelta(seconds=cfg.analysis_min_interval_seconds)
                    if (
                        state.last_analyzed_at is not None
                        and (now - state.last_analyzed_at) < min_interval_delta
                    ):
                        continue

                    rev = await analysis_service.analyze_story(
                        batch_conn,
                        state.story_id,
                        max_representative_fragments=cfg.representative_fragment_limit,
                        max_input_chars=cfg.analysis_max_input_chars,
                    )
                    if rev is not None:
                        b_stats["analyzed"] += 1

        return b_stats

    for current_edition_id in editions_to_process:
        async with runtime.uow.transaction() as conn:
            dirty_stories = await cluster_repo.list_dirty_cluster_states(
                conn, current_edition_id, limit=cfg.live_batch_size
            )
            _slug, scope_config = await resolve_edition_scope(conn, config, current_edition_id)
            scope_hash = scope_config_hash(scope_config)

        stats["scanned"] += len(dirty_stories)
        if not dirty_stories:
            continue

        # Filter settled stories: quiet window has passed since last fragment arrived (unless force_settled)
        if force_settled:
            settled = dirty_stories
        else:
            quiet_delta = dt.timedelta(seconds=cfg.analysis_quiet_seconds)
            settled = [s for s in dirty_stories if (now - s.last_seen_at) >= quiet_delta]

        stats["settled"] += len(settled)
        if not settled:
            continue

        # Prepare batches and run concurrently with Semaphore(4)
        batches = [
            settled[start : start + cfg.triage_batch_size]
            for start in range(0, len(settled), cfg.triage_batch_size)
        ]
        batch_results = await asyncio.gather(
            *[_process_gate_batch(b, current_edition_id, scope_config, scope_hash) for b in batches]
        )

        for b_stat in batch_results:
            for k in (
                "gated",
                "triaged",
                "deferred",
                "scope_local",
                "scope_direct_impact",
                "scope_out_of_scope",
                "scope_uncertain",
                "analyzed",
            ):
                stats[k] += b_stat[k]

    return stats
