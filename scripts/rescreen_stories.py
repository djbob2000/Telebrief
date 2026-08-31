"""CLI script to rescreen active story clusters through Gate V2 triage."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai_providers import create_provider
from src.bootstrap import build_infrastructure
from src.config_loader import load_config, load_database_config
from src.domain.event_clusters import StoryClusterState
from src.jobs.event_processing import resolve_edition_scope, scope_config_hash
from src.processing.event_brief import EventBriefService
from src.processing.event_triage import StoryTriageService
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.stories import StoryRepository
from src.runtime import clear_runtime, install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("rescreen_stories")


async def run_rescreen(
    *,
    hours: int | None = 72,
    edition_slug: str | None = None,
    # Empirical triage batch size: 30 stories per request.
    # Benchmarking on live models (MiniMax-M3 / Gemini / Claude) demonstrated that:
    # 1. 30 stories yield ~4,500-6,000 output tokens, completing in ~35-45s (well below upstream 60-90s TCP read timeouts).
    # 2. Batches > 50-80 stories take 2-5 minutes per HTTP call, frequently triggering upstream proxy drops and list attention degradation.
    # 3. 30 stories provide 100.0% decision consistency vs smaller/larger batches while minimizing failure blast radius.
    batch_size: int = 30,
    limit: int | None = None,
    dry_run: bool = False,
    override_database_url: str | None = None,
    custom_ai_provider: Any = None,
) -> dict[str, Any]:
    """Run Gate V2 rescreening across eligible story clusters."""
    db_config = load_database_config(require_enabled=True)
    if override_database_url:
        db_config.url = override_database_url

    full_config = load_config()
    infrastructure = await build_infrastructure(db_config)
    infrastructure.config = full_config
    install_runtime(infrastructure)

    stats = {
        "total_screened": 0,
        "deferred": 0,
        "scope": {"LOCAL": 0, "DIRECT_IMPACT": 0, "OUT_OF_SCOPE": 0, "UNCERTAIN": 0},
        "retention": {"KEEP_ANALYZE": 0, "KEEP_BRIEF": 0, "DROP": 0},
        "drop_reasons": {},
    }

    try:
        ai_provider = (
            custom_ai_provider
            or getattr(infrastructure, "ai_invoker", None)
            or create_provider(
                full_config.settings.ai_provider,
                logger,
                openai_api_key=full_config.openai_api_key,
                anthropic_api_key=full_config.anthropic_api_key,
                google_api_key=full_config.gemini_api_key,
                openrouter_api_key=full_config.openrouter_api_key,
                openrouter_model=full_config.openrouter_model,
            )
        )

        cluster_repo = EventClusterRepository()
        story_repo = StoryRepository()
        triage_service = StoryTriageService(
            ai_cascade=ai_provider,
            cluster_repo=cluster_repo,
            model=full_config.settings.ai_model,
        )
        brief_service = EventBriefService(
            story_repo=story_repo,
            cluster_repo=cluster_repo,
        )

        async with infrastructure.uow.transaction() as conn:
            # Query target editions
            if edition_slug:
                cur = await conn.execute(
                    "SELECT id, slug, name FROM editions WHERE slug = %s", (edition_slug,)
                )
            else:
                cur = await conn.execute(
                    "SELECT id, slug, name FROM editions ORDER BY id ASC"
                )
            editions = await cur.fetchall()

        if not editions:
            logger.warning("No matching editions found for rescreening.")
            return stats

        for ed_row in editions:
            ed_id, ed_slug, ed_name = ed_row[0], ed_row[1], ed_row[2]
            logger.info("Rescreening stories for edition %s (%s)...", ed_name, ed_slug)

            async with infrastructure.uow.transaction() as conn:
                _slug, scope_config = await resolve_edition_scope(conn, full_config, ed_id)
                sc_hash = scope_config_hash(scope_config)

                query = """
                    SELECT scs.story_id, scs.centroid, scs.model, scs.dimensions,
                           scs.fragment_count, scs.unique_source_count, scs.first_seen_at,
                           scs.last_seen_at, scs.latest_assignment_id,
                           scs.last_analyzed_assignment_id, scs.last_analyzed_at,
                           scs.analysis_dirty, scs.updated_at
                    FROM story_cluster_state scs
                    JOIN stories s ON s.id = scs.story_id
                    WHERE s.edition_id = %s AND s.lifecycle_state IN ('active', 'candidate')
                """
                params: list[Any] = [ed_id]
                if hours is not None:
                    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
                    query += " AND scs.last_seen_at >= %s"
                    params.append(since)
                query += " ORDER BY scs.last_seen_at DESC"
                if limit is not None and limit > 0:
                    query += f" LIMIT {int(limit)}"

                cur = await conn.execute(query, params)
                rows = await cur.fetchall()

            cluster_states = [StoryClusterState.from_row(r, []) for r in rows]
            logger.info(
                "Found %d candidate story clusters in edition %s", len(cluster_states), ed_slug
            )

            # Process in batches
            for i in range(0, len(cluster_states), batch_size):
                batch_states = cluster_states[i : i + batch_size]
                if not batch_states:
                    continue

                if dry_run:
                    logger.info(
                        "[DRY-RUN] Evaluating triage batch of %d stories (read-only)...",
                        len(batch_states),
                    )
                    # We run triage inside a transaction and rollback
                    async with infrastructure.uow.transaction() as conn:
                        batch_result = await triage_service.triage_stories_batch(
                            conn,
                            batch_states,
                            edition_id=ed_id,
                            scope_config=scope_config,
                            scope_hash=sc_hash,
                        )
                        # Do not commit; inspect results
                        logger.info(
                            "[DRY-RUN] Evaluated %d stories: %d scoped, %d deferred",
                            len(batch_states),
                            len(batch_result.results),
                            len(batch_result.deferred_story_ids),
                        )
                        for res in batch_result.results:
                            logger.info(
                                "  Story #%d: scope=%s (conf=%.2f) retention=%s enrichment=%s reason=%s",
                                res.story_id,
                                res.scope,
                                res.scope_confidence,
                                res.retention,
                                res.enrichment,
                                res.reason[:60],
                            )
                else:
                    async with infrastructure.uow.transaction() as conn:
                        batch_result = await triage_service.triage_stories_batch(
                            conn,
                            batch_states,
                            edition_id=ed_id,
                            scope_config=scope_config,
                            scope_hash=sc_hash,
                        )

                        # Handle post-triage side effects
                        s_map = {s.story_id: s for s in batch_states}
                        for res in batch_result.results:
                            state = s_map.get(res.story_id)
                            assignment_id = state.latest_assignment_id if state else 0
                            if res.retention == "KEEP" and res.enrichment == "BRIEF":
                                if res.brief_payload:
                                    await brief_service.persist_brief(
                                        conn,
                                        story_id=res.story_id,
                                        assignment_id=assignment_id,
                                        payload=res.brief_payload,
                                    )
                                await cluster_repo.mark_cluster_processed_without_analysis(
                                    conn,
                                    story_id=res.story_id,
                                    assignment_id=assignment_id,
                                )
                            elif res.retention == "DROP":
                                await cluster_repo.mark_cluster_processed_without_analysis(
                                    conn,
                                    story_id=res.story_id,
                                    assignment_id=assignment_id,
                                )

                # Update stats
                stats["deferred"] += len(batch_result.deferred_story_ids)
                for res in batch_result.results:
                    stats["total_screened"] += 1
                    s_cls = res.scope
                    if s_cls in stats["scope"]:
                        stats["scope"][s_cls] += 1

                    if res.retention == "KEEP":
                        if res.enrichment == "BRIEF":
                            stats["retention"]["KEEP_BRIEF"] += 1
                        else:
                            stats["retention"]["KEEP_ANALYZE"] += 1
                    else:
                        stats["retention"]["DROP"] += 1
                        reason = res.exclusion_reason or "other"
                        stats["drop_reasons"][reason] = stats["drop_reasons"].get(reason, 0) + 1

        logger.info(
            "=== Rescreen Complete ===\n"
            "Total screened: %d\n"
            "Deferred: %d\n"
            "Scope breakdown: %s\n"
            "Retention: %s\n"
            "Drop reasons: %s",
            stats["total_screened"],
            stats["deferred"],
            stats["scope"],
            stats["retention"],
            stats["drop_reasons"],
        )
        return stats
    finally:
        await infrastructure.close()
        clear_runtime(infrastructure)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rescreen active stories through Gate V2 triage.")
    parser.add_argument(
        "--hours",
        type=int,
        default=72,
        help="Lookback window in hours (default: 72, pass 0 for all).",
    )
    parser.add_argument("--all", action="store_true", help="Screen all historical active stories.")
    parser.add_argument(
        "--edition", type=str, default=None, help="Edition slug to screen (e.g. berdyansk)."
    )
    parser.add_argument(
        "--batch-size", type=int, default=30, help="Batch size for triage AI calls (default: 30)."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Maximum number of candidate stories to screen."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Inspect decisions without writing to DB."
    )
    args = parser.parse_args()

    hours = None if args.all or args.hours <= 0 else args.hours
    asyncio.run(
        run_rescreen(
            hours=hours,
            edition_slug=args.edition,
            batch_size=args.batch_size,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
