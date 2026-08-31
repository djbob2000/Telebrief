"""Benchmark Gate V2 triage batch sizes to find the optimal throughput and reliability sweet spot."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai_providers import create_provider
from src.bootstrap import build_infrastructure
from src.config_loader import load_config, load_database_config
from src.domain.event_clusters import StoryClusterState
from src.jobs.event_processing import resolve_edition_scope, scope_config_hash
from src.processing.event_triage import StoryTriageService
from src.repositories.event_clusters import EventClusterRepository
from src.runtime import clear_runtime, install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("benchmark_batch_size")


async def benchmark_batch_sizes(
    batch_sizes: list[int] | None = None, edition_slug: str = "berdyansk"
) -> None:
    if batch_sizes is None:
        batch_sizes = [50, 70, 85, 100, 120, 150]
    db_config = load_database_config(require_enabled=True)
    full_config = load_config()
    infrastructure = await build_infrastructure(db_config)
    infrastructure.config = full_config
    install_runtime(infrastructure)

    try:
        ai_provider = create_provider(
            full_config.settings.ai_provider,
            logger,
            openai_api_key=full_config.openai_api_key,
            anthropic_api_key=full_config.anthropic_api_key,
            google_api_key=full_config.gemini_api_key,
            openrouter_api_key=full_config.openrouter_api_key,
            openrouter_model=full_config.openrouter_model,
        )

        cluster_repo = EventClusterRepository()
        triage_service = StoryTriageService(
            ai_cascade=ai_provider,
            cluster_repo=cluster_repo,
            model=full_config.settings.ai_model,
        )

        total_needed = sum(batch_sizes) + 50
        async with infrastructure.uow.transaction() as conn:
            cur = await conn.execute("SELECT id, name FROM editions WHERE slug = %s", (edition_slug,))
            row = await cur.fetchone()
            if not row:
                print(f"Edition {edition_slug} not found")
                return
            ed_id = row[0]
            _slug, scope_config = await resolve_edition_scope(conn, full_config, ed_id)
            sc_hash = scope_config_hash(scope_config)

            # Pull stories without v7 triage decision to test real LLM calls
            cur = await conn.execute(
                """
                SELECT scs.story_id, scs.centroid, scs.model, scs.dimensions,
                       scs.fragment_count, scs.unique_source_count, scs.first_seen_at,
                       scs.last_seen_at, scs.latest_assignment_id,
                       scs.last_analyzed_assignment_id, scs.last_analyzed_at,
                       scs.analysis_dirty, scs.updated_at
                FROM story_cluster_state scs
                JOIN stories s ON s.id = scs.story_id
                WHERE s.edition_id = %s AND s.lifecycle_state IN ('active', 'candidate')
                  AND s.id NOT IN (
                      SELECT story_id FROM story_event_triage_decisions WHERE triage_version = 'v7'
                  )
                ORDER BY scs.last_seen_at DESC
                LIMIT %s
                """,
                (ed_id, total_needed),
            )
            rows = await cur.fetchall()
            pool_states = [StoryClusterState.from_row(r, []) for r in rows]

        print(f"\nLoaded pool of {len(pool_states)} uncached stories for stress-testing batch sizes: {batch_sizes}\n")
        print("=" * 100)
        print(f"{'Batch Size':<12} | {'Time (s)':<10} | {'Throughput':<14} | {'Returned / Requested':<22} | {'Status / Diagnostics':<25}")
        print("=" * 100)

        offset = 0
        for b_size in batch_sizes:
            test_batch = pool_states[offset : offset + b_size]
            offset += b_size
            if len(test_batch) < b_size:
                print(f"Skipping {b_size}: not enough stories (have {len(test_batch)})")
                continue

            t0 = time.perf_counter()
            error = None
            success_count = 0
            deferred_count = 0

            try:
                async with infrastructure.uow.transaction() as conn:
                    batch_res = await triage_service.triage_stories_batch(
                        conn,
                        test_batch,
                        edition_id=ed_id,
                        scope_config=scope_config,
                        scope_hash=sc_hash,
                    )
                    success_count = len(batch_res.results)
                    deferred_count = len(batch_res.deferred_story_ids)
            except Exception as e:
                error = str(e)

            duration = time.perf_counter() - t0
            throughput = success_count / duration if duration > 0 else 0
            ret_str = f"{success_count}/{b_size} ({(success_count/b_size)*100:.0f}%)"

            if not error and deferred_count == 0:
                diag = "🟢 100% OK"
            elif deferred_count > 0 and success_count > 0:
                diag = f"🟡 Omitted {deferred_count} stories"
            else:
                diag = f"🔴 FAILED: {error or 'All deferred'}"

            print(f"{b_size:<12} | {duration:<10.2f} | {throughput:<14.2f} st/s | {ret_str:<22} | {diag:<25}")

        print("=" * 100 + "\n")

    finally:
        await infrastructure.close()
        clear_runtime(infrastructure)


if __name__ == "__main__":
    asyncio.run(benchmark_batch_sizes())
