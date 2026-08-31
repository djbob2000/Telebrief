"""Experiment to test decision consistency and completeness between large vs small batch sizes."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
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
logger = logging.getLogger("batch_consistency")


async def run_consistency_experiment(edition_slug: str = "berdyansk", sample_size: int = 60) -> None:
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

        async with infrastructure.uow.transaction() as conn:
            cur = await conn.execute("SELECT id, name FROM editions WHERE slug = %s", (edition_slug,))
            row = await cur.fetchone()
            if not row:
                print(f"Edition {edition_slug} not found")
                return
            ed_id, ed_name = row[0], row[1]
            _slug, scope_config = await resolve_edition_scope(conn, full_config, ed_id)
            sc_hash = scope_config_hash(scope_config)

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
                ORDER BY scs.last_seen_at DESC
                LIMIT %s
                """,
                (ed_id, sample_size),
            )
            rows = await cur.fetchall()
            pool_states = [StoryClusterState.from_row(r, []) for r in rows]

        print(f"\nEvaluating {len(pool_states)} stories in two modes:")
        print("Mode A: 1 single batch of 30 stories")
        print("Mode B: 2 separate batches of 15 stories\n")

        # Mode A: 1 single batch of 30
        print("Running Mode A (Single Batch of 30)...")
        async with infrastructure.uow.transaction() as conn:
            res_a = await triage_service.triage_stories_batch(
                conn, pool_states, edition_id=ed_id, scope_config=scope_config, scope_hash=sc_hash
            )

        # Mode B: 2 batches of 15
        print("Running Mode B (Two Batches of 15)...")
        res_b_list = []
        batch_size = 15
        for i in range(0, len(pool_states), batch_size):
            sub_batch = pool_states[i : i + batch_size]
            async with infrastructure.uow.transaction() as conn:
                sub_res = await triage_service.triage_stories_batch(
                    conn, sub_batch, edition_id=ed_id, scope_config=scope_config, scope_hash=sc_hash
                )
                res_b_list.extend(sub_res.results)

        # Comparison
        map_a = {r.story_id: r for r in res_a.results}
        map_b = {r.story_id: r for r in res_b_list}

        all_ids = [s.story_id for s in pool_states]
        print("\n" + "=" * 90)
        print(f"{'Story ID':<10} | {'Scope (Batch 30)':<18} | {'Scope (2x15)':<18} | {'Retention (30)':<15} | {'Retention (2x15)':<15} | {'Match?':<8}")
        print("=" * 90)

        scope_matches = 0
        retention_matches = 0
        total_compared = 0

        for sid in all_ids:
            item_a = map_a.get(sid)
            item_b = map_b.get(sid)

            scope_a = item_a.scope if item_a else "MISSING"
            scope_b = item_b.scope if item_b else "MISSING"
            ret_a = item_a.retention if item_a else "MISSING"
            ret_b = item_b.retention if item_b else "MISSING"

            is_match = (scope_a == scope_b) and (ret_a == ret_b) and (scope_a != "MISSING")
            if scope_a == scope_b and scope_a != "MISSING":
                scope_matches += 1
            if ret_a == ret_b and ret_a != "MISSING":
                retention_matches += 1
            total_compared += 1

            match_sym = "✅ YES" if is_match else ("⚠️ DIFF" if scope_a != "MISSING" and scope_b != "MISSING" else "❌ MISS")
            print(f"{sid:<10} | {scope_a:<18} | {scope_b:<18} | {ret_a:<15} | {ret_b:<15} | {match_sym:<8}")

        print("=" * 90)
        print(f"Summary Comparison on {total_compared} stories:")
        print(f"  - Mode A returned: {len(map_a)}/{len(pool_states)} stories")
        print(f"  - Mode B returned: {len(map_b)}/{len(pool_states)} stories")
        print(f"  - Scope classification agreement: {scope_matches}/{total_compared} ({scope_matches/total_compared*100:.1f}%)")
        print(f"  - Retention decision agreement: {retention_matches}/{total_compared} ({retention_matches/total_compared*100:.1f}%)\n")

    finally:
        await infrastructure.close()
        clear_runtime(infrastructure)


if __name__ == "__main__":
    asyncio.run(run_consistency_experiment())
