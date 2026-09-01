"""Migration utility to re-fragment and re-process unsealed active revisions with FRAGMENTER_VERSION v2.

Preserves historical sealed publication runs byte-for-byte by strictly excluding
any revision referenced in publication_input_fragments.
Executes canonical reprocessing pipeline (process_event_revisions_task + coalesce_dirty_stories_task)
so embeddings, clustering assignments, and latest_assignment_id are truthfully established.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from src.bootstrap import build_infrastructure
from src.config_loader import load_config, load_database_config
from src.jobs.event_processing import coalesce_dirty_stories_task, process_event_revisions_task
from src.processing.fragments import FRAGMENTER_VERSION
from src.runtime import clear_runtime, install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_fragmenter_v2")


async def run_migration(*, batch_size: int = 32) -> tuple[int, int]:
    """Re-fragment and re-process unsealed revisions with FRAGMENTER_VERSION v2.

    Returns (processed_revisions_count, new_fragments_count).
    """
    db_config = load_database_config(require_enabled=True)
    full_config = load_config()
    infrastructure = await build_infrastructure(db_config)
    infrastructure.config = full_config
    install_runtime(infrastructure)

    try:
        # 1. Find all revisions that are NOT part of any sealed publication run
        query = """
            SELECT DISTINCT sir.id
            FROM source_item_revisions sir
            WHERE sir.id NOT IN (
                SELECT DISTINCT sf.source_item_revision_id
                FROM source_fragments sf
                JOIN publication_input_fragments pif ON pif.fragment_id = sf.id
            )
            ORDER BY sir.id ASC
        """
        async with infrastructure.uow.transaction() as conn:
            cursor = await conn.execute(query)
            unsealed_rev_ids = [int(row[0]) async for row in cursor]

        if not unsealed_rev_ids:
            logger.info("No unsealed revisions found to migrate.")
            return 0, 0

        logger.info(
            "Found %d unsealed revisions to migrate with %s.",
            len(unsealed_rev_ids),
            FRAGMENTER_VERSION,
        )

        # 2. Delete existing fragments for these unsealed revisions
        async with infrastructure.uow.transaction() as conn:
            for i in range(0, len(unsealed_rev_ids), 200):
                chunk = unsealed_rev_ids[i : i + 200]
                await conn.execute(
                    "DELETE FROM source_fragments WHERE source_item_revision_id = ANY(%s)",
                    (chunk,),
                )
            # Mark existing story clusters dirty
            await conn.execute("UPDATE story_cluster_state SET analysis_dirty = TRUE")

        logger.info("Deleted stale fragments for %d unsealed revisions.", len(unsealed_rev_ids))

        # 3. Canonical reprocessing via process_event_revisions_task
        total_frags = 0
        for i in range(0, len(unsealed_rev_ids), batch_size):
            chunk = unsealed_rev_ids[i : i + batch_size]
            stats = await process_event_revisions_task.func(chunk)
            total_frags += stats.get("fragments", 0)
            logger.info(
                "Processed batch %d-%d / %d: %s",
                i,
                i + len(chunk),
                len(unsealed_rev_ids),
                stats,
            )

        # 4. Drain coalesce dirty stories loop
        logger.info("Coalescing dirty stories into clusters...")
        coalesce_round = 0
        while True:
            coalesce_round += 1
            coalesce_stats = await coalesce_dirty_stories_task.func(edition_id=None)
            logger.info("Coalesce round %d: %s", coalesce_round, coalesce_stats)
            if coalesce_stats.get("scanned", 0) == 0 or coalesce_stats.get("settled", 0) == 0:
                break
            if coalesce_round >= 50:
                logger.warning("Reached max coalesce iterations (50)")
                break

        logger.info(
            "Successfully migrated %d revisions into %d fragments with %s.",
            len(unsealed_rev_ids),
            total_frags,
            FRAGMENTER_VERSION,
        )
        return len(unsealed_rev_ids), total_frags
    finally:
        clear_runtime(infrastructure)
        await infrastructure.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate unsealed revisions to Fragmenter v2")
    parser.add_argument(
        "--batch-size", type=int, default=32, help="Processing batch size (default: 32)"
    )
    args = parser.parse_args()

    asyncio.run(run_migration(batch_size=args.batch_size))


if __name__ == "__main__":
    main()
