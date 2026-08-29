"""CLI benchmark evaluating publication quality and AI budget for 24-32h digest & article."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import time

from psycopg_pool import AsyncConnectionPool

from src.config_loader import Config, load_config
from src.db.uow import DatabaseUnitOfWork
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("benchmark")


async def run_benchmark(
    hours: int = 24,
    edition_slug: str = "berdyansk",
    database_url: str | None = None,
) -> None:
    db_url = (
        database_url
        or os.environ.get("DATABASE_URL")
        or os.environ.get("TELEBRIEF_TEST_DATABASE_URL")
        or "postgresql://telebrief:telebrief@localhost:5432/telebrief_test"
    )

    config: Config = load_config()
    now = dt.datetime.now(dt.timezone.utc)

    async with AsyncConnectionPool(db_url, min_size=1, max_size=5, timeout=30.0) as pool:
        uow = DatabaseUnitOfWork(pool)
        repo = PublicationRepository()

        async with uow.transaction() as conn:
            cur = await conn.execute(
                "SELECT id, slug, name FROM editions WHERE slug = %s", (edition_slug,)
            )
            row = await cur.fetchone()
            if row is None:
                cur2 = await conn.execute(
                    "INSERT INTO editions (slug, name) VALUES (%s, %s) RETURNING id, slug, name",
                    (edition_slug, edition_slug.capitalize()),
                )
                row2 = await cur2.fetchone()
                if row2 is None:
                    raise RuntimeError("Failed to create edition")
                row = row2
            edition_id, slug, name = row[0], row[1], row[2]

        logger.info(
            "Starting benchmark for edition '%s' (id=%d) over past %d hours",
            slug,
            edition_id,
            hours,
        )

        snapshot_service = PublicationSnapshotService(uow=uow, repo=repo)
        selection_service = EditorialSelectionService(uow=uow, repo=repo, config=config)
        generation_service = PublicationGenerationService(uow=uow, config=config, repo=repo)

        # 1. Digest Benchmark
        t0 = time.perf_counter()
        req_key_digest = f"bench:digest:{edition_id}:{now.isoformat()}"
        digest_run = await snapshot_service.create_run(
            edition_id=edition_id,
            publication_type="digest_grouped",
            snapshot_at=now,
            request_key=req_key_digest,
            config=config,
            lookback_hours_override=hours,
        )
        digest_candidates = await snapshot_service.seal_candidates(digest_run.id)
        digest_inputs = await selection_service.select(digest_run.id, defer_generation=False)
        digest_pub = None
        if digest_inputs:
            digest_pub = await generation_service.generate(digest_run.id, defer_delivery=False)
        t_digest = time.perf_counter() - t0

        async with uow.transaction() as conn:
            cur = await conn.execute(
                """
                SELECT id, kind, status, error_kind, provider, model, metadata
                FROM publication_generation_attempts
                WHERE publication_run_id = %s
                ORDER BY attempt_no ASC
                """,
                (digest_run.id,),
            )
            digest_attempts = await cur.fetchall()

        digest_chat_calls = sum(
            1
            for a in digest_attempts
            if a[1] in ("writer", "editorializer") and a[4] not in ("deterministic", None)
        )

        # 2. Article Benchmark
        t0 = time.perf_counter()
        req_key_article = f"bench:article:{edition_id}:{now.isoformat()}"
        article_run = await snapshot_service.create_run(
            edition_id=edition_id,
            publication_type="article",
            snapshot_at=now,
            request_key=req_key_article,
            config=config,
            lookback_hours_override=hours,
        )
        article_candidates = await snapshot_service.seal_candidates(article_run.id)
        article_inputs = await selection_service.select(article_run.id, defer_generation=False)
        article_pub = None
        win_kind = "none"
        win_meta = {}
        if article_inputs:
            article_pub = await generation_service.generate(article_run.id, defer_delivery=False)

        t_article = time.perf_counter() - t0

        async with uow.transaction() as conn:
            cur = await conn.execute(
                """
                SELECT id, kind, status, error_kind, provider, model, metadata
                FROM publication_generation_attempts
                WHERE publication_run_id = %s
                ORDER BY attempt_no ASC
                """,
                (article_run.id,),
            )
            article_attempts = await cur.fetchall()

            if article_pub is not None:
                cur = await conn.execute(
                    "SELECT kind, metadata FROM publication_generation_attempts WHERE id = %s",
                    (article_pub.winning_generation_attempt_id,),
                )
                win_row = await cur.fetchone()
                win_kind = win_row[0] if win_row else "unknown"
                win_meta = win_row[1] if win_row else {}

        article_chat_calls = sum(
            1
            for a in article_attempts
            if a[1] in ("writer", "editorializer") and a[4] not in ("deterministic", None)
        )
        claim_trace_count = (
            len(win_meta.get("claim_trace", [])) if isinstance(win_meta, dict) else 0
        )

        # Print Benchmark Report
        print("\n" + "=" * 70)
        print("  PUBLICATION QUALITY & AI BUDGET BENCHMARK REPORT")
        print("=" * 70)
        print(f"Edition:             {name} ({slug})")
        print(f"Lookback Window:     {hours} hours")
        print(f"Timestamp:           {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("-" * 70)
        print("DIGEST RESULTS:")
        print(f"  Candidates:        {len(digest_candidates)}")
        print(f"  Selected:          {len(digest_inputs)}")
        print(f"  Publication ID:    {digest_pub.id if digest_pub else 'N/A (no inputs)'}")
        print(f"  Length (chars):    {len(digest_pub.body or '') if digest_pub else 0}")
        print(f"  Chat LLM Calls:    {digest_chat_calls} (Target: <= 1)")
        print(f"  Duration:          {t_digest:.2f}s")
        print("-" * 70)
        print("ARTICLE RESULTS:")
        print(f"  Candidates:        {len(article_candidates)}")
        print(f"  Selected:          {len(article_inputs)}")
        print(f"  Publication ID:    {article_pub.id if article_pub else 'N/A (no inputs)'}")
        print(f"  Title:             {article_pub.title if article_pub else 'N/A'}")
        print(
            f"  Word count:        {len((article_pub.body or '').split()) if article_pub else 0} words"
        )
        print(f"  Winning Attempt:   {win_kind}")
        print(f"  Claim Trace Units: {claim_trace_count}")
        print(f"  Chat LLM Calls:    {article_chat_calls} (Target: <= 1)")
        print(f"  Duration:          {t_article:.2f}s")
        print("=" * 70 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publication Quality Benchmark")
    parser.add_argument("--hours", type=int, default=24, help="Lookback hours (default: 24)")
    parser.add_argument("--edition", type=str, default="berdyansk", help="Edition slug")
    parser.add_argument("--database-url", type=str, default=None, help="Database connection URL")
    args = parser.parse_args()

    asyncio.run(
        run_benchmark(
            hours=args.hours,
            edition_slug=args.edition,
            database_url=args.database_url,
        )
    )


if __name__ == "__main__":
    main()
