"""Migration utility to re-fragment unsealed active revisions with FRAGMENTER_VERSION v2.

Preserves historical sealed publication runs byte-for-byte by strictly excluding
any revision referenced in publication_input_fragments.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import psycopg

from src.processing.fragments import FRAGMENTER_VERSION, split_into_fragments
from src.repositories.fragments import FragmentRepository

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_fragmenter_v2")


async def migrate_active_revisions(conn: psycopg.AsyncConnection) -> tuple[int, int]:
    """Re-fragment unsealed revisions to FRAGMENTER_VERSION v2.

    Returns (processed_revisions_count, new_fragments_count).
    """
    # 1. Find all revisions that are NOT part of any sealed publication run
    query = """
        SELECT sir.id, sir.text_content
        FROM source_item_revisions sir
        WHERE sir.id NOT IN (
            SELECT DISTINCT sf.source_item_revision_id
            FROM source_fragments sf
            JOIN publication_input_fragments pif ON pif.source_fragment_id = sf.id
        )
        ORDER BY sir.id ASC
    """
    cursor = await conn.execute(query)
    unsealed_revisions = [(row[0], row[1]) async for row in cursor]

    if not unsealed_revisions:
        logger.info("No unsealed revisions found to migrate.")
        return 0, 0

    logger.info("Found %d unsealed revisions to re-fragment with %s.", len(unsealed_revisions), FRAGMENTER_VERSION)

    repo = FragmentRepository()
    total_new_frags = 0

    async with conn.transaction():
        for rev_id, raw_text in unsealed_revisions:
            if not raw_text:
                continue

            # Delete existing fragments for this unsealed revision
            await conn.execute(
                "DELETE FROM source_fragments WHERE source_item_revision_id = %s",
                (rev_id,),
            )

            # Generate v2 fragments
            new_frags = split_into_fragments(raw_text, fragmenter_version=FRAGMENTER_VERSION)
            if new_frags:
                persisted = await repo.create_fragments(conn, rev_id, new_frags)
                total_new_frags += len(persisted)

        # Mark all event clusters dirty so they re-cluster and re-triage on next run
        await conn.execute("UPDATE story_cluster_state SET analysis_dirty = TRUE")

    logger.info(
        "Successfully re-fragmented %d revisions into %d fragments with %s.",
        len(unsealed_revisions),
        total_new_frags,
        FRAGMENTER_VERSION,
    )
    return len(unsealed_revisions), total_new_frags


async def main() -> None:
    dsn = sys.argv[1] if len(sys.argv) > 1 else "postgresql://localhost:5432/telebrief"
    logger.info("Connecting to %s...", dsn)
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await migrate_active_revisions(conn)


if __name__ == "__main__":
    asyncio.run(main())
