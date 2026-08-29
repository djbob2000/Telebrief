"""Procrastinate tasks for Event-First pipeline processing and coalescing."""

from __future__ import annotations

import datetime as dt
import logging

from src.ai_providers import create_provider
from src.config_loader import load_config
from src.embedding_providers import create_embedding_provider
from src.jobs.app import procrastinate_app
from src.processing.embeddings import EmbeddingService
from src.processing.event_analysis import EventAnalysisService
from src.processing.event_clustering import EventClusteringService
from src.processing.event_triage import StoryTriageService
from src.processing.fragments import split_into_fragments
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.fragments import FragmentRepository
from src.repositories.stories import StoryRepository
from src.runtime import get_runtime

logger = logging.getLogger(__name__)


@procrastinate_app.task(queue="processing", name="process_event_revisions")
async def process_event_revisions_task(revision_ids: list[int]) -> dict[str, int]:
    """Ingest and cluster a batch of source item revisions."""
    runtime = get_runtime()
    config = load_config()
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

            new_frags = split_into_fragments(raw_text, max_chars=cfg.fragment_max_chars)
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
async def coalesce_dirty_stories_task(edition_id: int | None = None) -> dict[str, int]:
    """Coalesce dirty story clusters, triage low-support ones, and run rich analysis."""
    runtime = get_runtime()
    config = load_config()
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

    now = dt.datetime.now(dt.timezone.utc)
    stats = {"scanned": 0, "settled": 0, "triaged": 0, "analyzed": 0}

    async with runtime.uow.transaction() as conn:
        dirty_stories = await cluster_repo.list_dirty_cluster_states(
            conn, edition_id, limit=cfg.live_batch_size
        )
        stats["scanned"] = len(dirty_stories)
        if not dirty_stories:
            return stats

        # Filter settled stories: quiet window has passed since last fragment arrived
        quiet_delta = dt.timedelta(seconds=cfg.analysis_quiet_seconds)
        min_interval_delta = dt.timedelta(seconds=cfg.analysis_min_interval_seconds)

        settled: list = []
        for s in dirty_stories:
            # Check quiet window
            if (now - s.last_seen_at) < quiet_delta:
                continue
            # Check min interval between analysis runs
            if s.last_analyzed_at is not None and (now - s.last_analyzed_at) < min_interval_delta:
                continue
            settled.append(s)

        stats["settled"] = len(settled)
        if not settled:
            return stats

        # Partition into direct high-support and low-support triage candidates
        direct_stories: list = []
        low_support_stories: list = []

        for s in settled:
            if (
                s.fragment_count >= cfg.direct_analysis_min_fragments
                or s.unique_source_count >= cfg.direct_analysis_min_unique_sources
            ):
                direct_stories.append(s)
            else:
                low_support_stories.append(s)

        stories_to_analyze_ids: list[int] = [s.story_id for s in direct_stories]

        # Triage low support stories in batch
        if low_support_stories:
            approved_ids = await triage_service.triage_stories_batch(
                conn,
                low_support_stories,
                excerpt_chars=cfg.triage_excerpt_chars,
                min_ignore_confidence=cfg.triage_min_ignore_confidence,
            )
            stats["triaged"] = len(low_support_stories)
            stories_to_analyze_ids.extend(approved_ids)

        # Run rich event analysis for all approved stories
        for sid in set(stories_to_analyze_ids):
            rev = await analysis_service.analyze_story(
                conn,
                sid,
                max_representative_fragments=cfg.representative_fragment_limit,
                max_input_chars=cfg.analysis_max_input_chars,
            )
            if rev is not None:
                stats["analyzed"] += 1

    return stats
