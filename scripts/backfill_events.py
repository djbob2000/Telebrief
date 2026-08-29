"""CLI script to backfill historical source item revisions into the Event-First pipeline."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config, load_database_config
from src.jobs.event_processing import coalesce_dirty_stories_task, process_event_revisions_task
from src.runtime import clear_runtime, install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("backfill_events")


async def run_backfill(
    *,
    hours: int = 72,
    limit: int = 500,
    batch_size: int = 32,
    edition_slug: str | None = None,
    dry_run: bool = False,
) -> None:
    db_config = load_database_config(require_enabled=True)
    full_config = load_config()
    infrastructure = await build_infrastructure(db_config)
    infrastructure.config = full_config
    install_runtime(infrastructure)

    try:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
        async with infrastructure.uow.transaction() as conn:
            # Query unfragmented revisions to backfill (gap-driven)
            query = """
                SELECT sir.id
                FROM source_item_revisions sir
                JOIN source_items si ON si.id = sir.source_item_id
                JOIN sources s ON s.id = si.source_id
                LEFT JOIN source_editions se ON se.source_id = s.id
                LEFT JOIN editions e ON e.id = se.edition_id
                LEFT JOIN source_fragments sf
                  ON sf.source_item_revision_id = sir.id
                 AND sf.fragmenter_version = 'v1'
                WHERE COALESCE(si.published_at, si.first_collected_at, now()) >= %s
                  AND sf.id IS NULL
            """
            params: list = [since]
            if edition_slug:
                query += " AND e.slug = %s"
                params.append(edition_slug)
            query += " ORDER BY COALESCE(si.published_at, si.first_collected_at) ASC, sir.id ASC LIMIT %s"
            params.append(limit)

            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            rev_ids = [int(r[0]) for r in rows]

        logger.info("Found %d unfragmented revisions to process (since %s)", len(rev_ids), since)
        if not rev_ids or dry_run:
            if dry_run:
                logger.info("Dry run complete (no modifications made)")
            return

        total_stats = {"revisions": 0, "fragments": 0, "candidates": 0, "assignments": 0}
        # Process in batches
        for i in range(0, len(rev_ids), batch_size):
            chunk = rev_ids[i : i + batch_size]
            stats = await process_event_revisions_task.func(chunk)
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v
            logger.info("Batch %d-%d processed: %s", i, i + len(chunk), stats)

        # Coalesce dirty stories in a drain loop
        logger.info("Coalescing and triaging dirty story clusters until backlog is drained...")
        coalesce_round = 0
        total_coalesce = {"scanned": 0, "settled": 0, "triaged": 0, "analyzed": 0}
        while True:
            coalesce_round += 1
            coalesce_stats = await coalesce_dirty_stories_task.func(edition_id=None)
            for k, v in coalesce_stats.items():
                total_coalesce[k] = total_coalesce.get(k, 0) + v
            logger.info("Coalesce round %d: %s", coalesce_round, coalesce_stats)
            if coalesce_stats.get("scanned", 0) == 0 or coalesce_stats.get("settled", 0) == 0:
                break
            if coalesce_round >= 50:
                logger.warning("Reached max coalesce iterations (50)")
                break

        logger.info("Coalesce complete: %s", total_coalesce)
        logger.info("Total backfill summary: %s", total_stats)

    finally:
        clear_runtime(infrastructure)
        await infrastructure.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill historical revisions into Event-First pipeline"
    )
    parser.add_argument(
        "--hours", type=int, default=72, help="Lookback window in hours (default: 72)"
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="Max revisions to process (default: 500)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=32, help="Embedding batch size (default: 32)"
    )
    parser.add_argument("--edition", type=str, default=None, help="Optional edition slug filter")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview revisions without processing"
    )
    args = parser.parse_args()

    asyncio.run(
        run_backfill(
            hours=args.hours,
            limit=args.limit,
            batch_size=args.batch_size,
            edition_slug=args.edition,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    main()
