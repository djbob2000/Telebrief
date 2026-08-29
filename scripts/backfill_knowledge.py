#!/usr/bin/env python3
"""CLI tool to monitor knowledge pipeline status and enqueue backfill jobs."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.jobs.processing import (
    backfill_claims,
    backfill_story_matching,
    evaluate_relevance,
)
from src.processing.claims import ClaimExtractionPolicyService
from src.processing.relevance import (
    RELEVANCE_PROMPT_VERSION,
    RelevancePolicyService,
    resolve_relevance_config_hash,
)
from src.processing.story_matching import StoryMatchingPolicyService
from src.runtime import install_runtime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_knowledge")


async def print_status(
    conn: psycopg.AsyncConnection, edition_id: int, since_hours: int | None = None
) -> None:
    """Print the counts and backlog across all knowledge processing stages."""
    print("=" * 70)
    title_suffix = f", Past {since_hours}h" if since_hours else ", All Time"
    print(f"📊 KNOWLEDGE PIPELINE STATUS (Edition: {edition_id}{title_suffix})")
    print("=" * 70)

    # 1. Raw items & revisions
    if since_hours is not None:
        cur = await conn.execute(
            """
            SELECT count(*) FROM source_items si
            WHERE COALESCE(si.published_at, si.first_collected_at) >= now() - (%s * interval '1 hour')
            """,
            (since_hours,),
        )
        source_items_count = (await cur.fetchone())[0]  # type: ignore[index]

        cur = await conn.execute(
            """
            SELECT count(*)
            FROM source_item_revisions sir
            JOIN source_items si ON si.id = sir.source_item_id
            WHERE COALESCE(si.published_at, si.first_collected_at) >= now() - (%s * interval '1 hour')
            """,
            (since_hours,),
        )
        revisions_count = (await cur.fetchone())[0]  # type: ignore[index]

        # 2. Relevance Decisions
        cur = await conn.execute(
            """
            SELECT count(d.id), count(CASE WHEN d.status = 'relevant' THEN 1 END)
            FROM edition_relevance_decisions d
            JOIN source_item_revisions sir ON sir.id = d.source_item_revision_id
            JOIN source_items si ON si.id = sir.source_item_id
            WHERE d.edition_id = %s
              AND COALESCE(si.published_at, si.first_collected_at) >= now() - (%s * interval '1 hour')
            """,
            (edition_id, since_hours),
        )
        rel_row = await cur.fetchone()
        total_relevance_decisions = rel_row[0]  # type: ignore[index]
        relevant_decisions = rel_row[1]  # type: ignore[index]

        cur = await conn.execute(
            """
            SELECT count(sir.id)
            FROM source_item_revisions sir
            JOIN source_items si ON si.id = sir.source_item_id
            JOIN source_editions se ON se.source_id = si.source_id
            WHERE se.edition_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM edition_relevance_decisions d
                  WHERE d.source_item_revision_id = sir.id AND d.edition_id = se.edition_id
              )
              AND COALESCE(si.published_at, si.first_collected_at) >= now() - (%s * interval '1 hour')
            """,
            (edition_id, since_hours),
        )
        missing_relevance = (await cur.fetchone())[0]  # type: ignore[index]

        # 3. Claims
        cur = await conn.execute(
            """
            SELECT count(c.id)
            FROM claims c
            JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
            JOIN source_items si ON si.id = sir.source_item_id
            WHERE c.edition_id = %s
              AND COALESCE(si.published_at, si.first_collected_at) >= now() - (%s * interval '1 hour')
            """,
            (edition_id, since_hours),
        )
        claims_count = (await cur.fetchone())[0]  # type: ignore[index]

        # 4. Stories
        cur = await conn.execute(
            """
            SELECT count(DISTINCT sc.story_id)
            FROM story_claims sc
            JOIN claims c ON c.id = sc.claim_id
            JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
            JOIN source_items si ON si.id = sir.source_item_id
            WHERE c.edition_id = %s
              AND COALESCE(si.published_at, si.first_collected_at) >= now() - (%s * interval '1 hour')
            """,
            (edition_id, since_hours),
        )
        stories_count = (await cur.fetchone())[0]  # type: ignore[index]
    else:
        cur = await conn.execute("SELECT count(*) FROM source_items si")
        source_items_count = (await cur.fetchone())[0]  # type: ignore[index]

        cur = await conn.execute("SELECT count(*) FROM source_item_revisions sir")
        revisions_count = (await cur.fetchone())[0]  # type: ignore[index]

        cur = await conn.execute(
            """
            SELECT count(d.id), count(CASE WHEN d.status = 'relevant' THEN 1 END)
            FROM edition_relevance_decisions d
            WHERE d.edition_id = %s
            """,
            (edition_id,),
        )
        rel_row = await cur.fetchone()
        total_relevance_decisions = rel_row[0]  # type: ignore[index]
        relevant_decisions = rel_row[1]  # type: ignore[index]

        cur = await conn.execute(
            """
            SELECT count(sir.id)
            FROM source_item_revisions sir
            JOIN source_items si ON si.id = sir.source_item_id
            JOIN source_editions se ON se.source_id = si.source_id
            WHERE se.edition_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM edition_relevance_decisions d
                  WHERE d.source_item_revision_id = sir.id AND d.edition_id = se.edition_id
              )
            """,
            (edition_id,),
        )
        missing_relevance = (await cur.fetchone())[0]  # type: ignore[index]

        cur = await conn.execute(
            "SELECT count(c.id) FROM claims c WHERE c.edition_id = %s",
            (edition_id,),
        )
        claims_count = (await cur.fetchone())[0]  # type: ignore[index]

        cur = await conn.execute(
            """
            SELECT count(DISTINCT sc.story_id)
            FROM story_claims sc
            JOIN claims c ON c.id = sc.claim_id
            WHERE c.edition_id = %s
            """,
            (edition_id,),
        )
        stories_count = (await cur.fetchone())[0]  # type: ignore[index]

    # 5. Procrastinate Queued Jobs
    cur = await conn.execute(
        """
        SELECT queue_name, task_name, status, count(*)
        FROM procrastinate_jobs
        WHERE status IN ('todo', 'doing')
        GROUP BY queue_name, task_name, status
        ORDER BY queue_name, task_name;
        """
    )
    active_jobs = await cur.fetchall()

    print(f"1. Source Items (messages):       {source_items_count}")
    print(f"   Source Item Revisions:         {revisions_count}")
    print(f"2. Relevance Decisions Total:     {total_relevance_decisions}")
    print(f"   • Relevant Verdicts:           {relevant_decisions}")
    print(f"   • Missing Relevance Verdict:   {missing_relevance} ⚠️")
    print(f"3. Extracted Claims:              {claims_count}")
    print(f"4. Stories with Claims:           {stories_count}")
    print("-" * 70)
    print("Active Procrastinate Queue Jobs (todo / doing):")
    if active_jobs:
        for q, task, status, count in active_jobs:
            print(f"   • [{q}] {task} ({status}): {count}")
    else:
        print("   (queue is empty)")
    print("=" * 70)


async def enqueue_backfill(
    conn: psycopg.AsyncConnection,
    *,
    edition_id: int,
    stage: str,
    since_hours: int | None,
    limit: int,
) -> int:
    """Enqueue backfill jobs into Procrastinate."""
    total_queued = 0

    if stage in ("relevance", "all"):
        rel_hash = resolve_relevance_config_hash()
        rel_pol = await RelevancePolicyService().ensure_current(
            conn,
            edition_id=edition_id,
            config_hash=rel_hash,
            prompt_version=RELEVANCE_PROMPT_VERSION,
        )
        if since_hours is not None:
            cur = await conn.execute(
                """
                SELECT sir.id
                FROM source_item_revisions sir
                JOIN source_items si ON si.id = sir.source_item_id
                JOIN source_editions se ON se.source_id = si.source_id
                WHERE se.edition_id = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM edition_relevance_decisions d
                      WHERE d.source_item_revision_id = sir.id
                        AND d.edition_id = se.edition_id
                        AND d.relevance_policy_id = %s
                        AND d.parent_decision_id IS NULL
                  )
                  AND COALESCE(si.published_at, si.first_collected_at) >= now() - (%s * interval '1 hour')
                ORDER BY sir.id DESC
                LIMIT %s
                """,
                (edition_id, rel_pol.id, since_hours, limit),
            )
        else:
            cur = await conn.execute(
                """
                SELECT sir.id
                FROM source_item_revisions sir
                JOIN source_items si ON si.id = sir.source_item_id
                JOIN source_editions se ON se.source_id = si.source_id
                WHERE se.edition_id = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM edition_relevance_decisions d
                      WHERE d.source_item_revision_id = sir.id
                        AND d.edition_id = se.edition_id
                        AND d.relevance_policy_id = %s
                        AND d.parent_decision_id IS NULL
                  )
                ORDER BY sir.id DESC
                LIMIT %s
                """,
                (edition_id, rel_pol.id, limit),
            )
        rev_ids = [r[0] for r in await cur.fetchall()]
        for rev_id in rev_ids:
            await evaluate_relevance.configure(connection=conn).defer_async(
                source_item_revision_id=rev_id,
                edition_id=edition_id,
                policy_id=rel_pol.id,
            )
        logger.info("Enqueued %d evaluate_relevance jobs (policy_id=%d)", len(rev_ids), rel_pol.id)
        total_queued += len(rev_ids)

    if stage in ("claims", "all"):
        claim_pol = await ClaimExtractionPolicyService().ensure_current(conn, edition_id=edition_id)
        queued_claims = await backfill_claims(
            edition_id=edition_id,
            extraction_policy_id=claim_pol.id,
            batch_size=limit,
        )
        logger.info("Enqueued %d extract_claims jobs (policy_id=%d)", queued_claims, claim_pol.id)
        total_queued += queued_claims

    if stage in ("stories", "all"):
        cfg = load_config()
        emb_model = getattr(cfg.settings, "embedding_model", "text-embedding-3-small") or "none"
        emb_dims = getattr(cfg.settings, "embedding_dimensions", 1536) or 1536
        story_pol = await StoryMatchingPolicyService().ensure_current(
            conn,
            edition_id=edition_id,
            embedding_model=emb_model,
            embedding_dimensions=emb_dims,
        )
        queued_stories = await backfill_story_matching(
            edition_id=edition_id,
            policy_id=story_pol.id,
            batch_size=limit,
        )
        logger.info("Enqueued %d match_claim jobs (policy_id=%d)", queued_stories, story_pol.id)
        total_queued += queued_stories

    return total_queued


async def main() -> None:
    parser = argparse.ArgumentParser(description="Knowledge pipeline backfill tool")
    parser.add_argument(
        "--status", action="store_true", help="Print current pipeline stats and backlog"
    )
    parser.add_argument(
        "--enqueue",
        choices=["relevance", "claims", "stories", "all"],
        help="Stage to enqueue backfill jobs for",
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=None,
        help="Filter items within past N hours (e.g. 96)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum number of items to enqueue per run (default: 500)",
    )
    parser.add_argument(
        "--edition-id", type=int, default=None, help="Edition ID (default: active edition)"
    )

    args = parser.parse_args()

    if not args.status and not args.enqueue:
        parser.print_help()
        sys.exit(1)

    config = load_config()
    infra = await build_infrastructure(config.database)
    await infra.open()
    install_runtime(infra)

    async with infra.pool.connection() as conn:
        edition_id = args.edition_id
        if edition_id is None:
            cur = await conn.execute(
                "SELECT id FROM editions WHERE enabled = true ORDER BY id LIMIT 1;"
            )
            row = await cur.fetchone()
            edition_id = int(row[0]) if row else 2

        if args.status:
            await print_status(conn, edition_id, args.since_hours)

        if args.enqueue:
            queued = await enqueue_backfill(
                conn,
                edition_id=edition_id,
                stage=args.enqueue,
                since_hours=args.since_hours,
                limit=args.limit,
            )
            print(f"\n🚀 Successfully enqueued {queued} jobs into Procrastinate.")
            print("Run worker to process them:")
            print("  uv run python -m src.worker --concurrency=2\n")

    await infra.close()


if __name__ == "__main__":
    asyncio.run(main())
