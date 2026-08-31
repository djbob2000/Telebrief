"""CLI benchmark evaluating publication quality and AI budget for 24-32h digest & article."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg_pool import AsyncConnectionPool

from src.config_loader import Config, load_config
from src.db.uow import DatabaseUnitOfWork
from src.publication.errors import ArticlePublicationRejected
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("benchmark")


@dataclass
class BenchmarkAttemptRecord:
    kind: str
    status: str
    error_kind: str | None = None
    provider: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BenchmarkRunRecord:
    run_id: int
    publication_type: str
    status: str
    error_kind: str | None = None
    publication_id: int | None = None
    attempts: list[BenchmarkAttemptRecord] = field(default_factory=list)


def calculate_benchmark_metrics(runs: list[BenchmarkRunRecord]) -> dict[str, Any]:
    article_runs = [r for r in runs if r.publication_type == "article"]
    article_writer_attempts = sum(1 for r in article_runs for a in r.attempts if a.kind == "writer")
    article_writer_successes = sum(
        1
        for r in article_runs
        for a in r.attempts
        if a.kind == "writer" and a.status == "succeeded"
    )
    article_rejections = sum(
        1
        for r in article_runs
        if r.error_kind in ("article_validation_rejected", "article_writer_rejected")
        or (
            r.status == "failed"
            and any(a.kind == "writer" and a.status == "failed" for a in r.attempts)
        )
    )
    article_publications = sum(
        1 for r in article_runs if r.publication_id is not None or r.status == "succeeded"
    )
    article_fallback_content_attempts = sum(
        1 for r in article_runs for a in r.attempts if a.kind == "story_renderer_fallback"
    )
    article_writer_success_rate = (
        article_writer_successes / article_writer_attempts if article_writer_attempts > 0 else 0.0
    )
    max_article_writer_calls_per_run = max(
        [sum(1 for a in r.attempts if a.kind == "writer") for r in article_runs],
        default=0,
    )
    return {
        "runs": runs,
        "article_writer_attempts": article_writer_attempts,
        "article_writer_successes": article_writer_successes,
        "article_rejections": article_rejections,
        "article_publications": article_publications,
        "article_fallback_content_attempts": article_fallback_content_attempts,
        "article_writer_success_rate": article_writer_success_rate,
        "max_article_writer_calls_per_run": max_article_writer_calls_per_run,
    }


def evaluate_digest_short_read_quality(
    digest_body: str,
    narrative_draft: Any | None = None,
) -> dict[str, Any]:
    """Evaluates short-read quality constraints on a generated publication digest."""
    headline_lengths: list[int] = []
    situation_lengths: list[int] = []
    detail_lengths: list[int] = []
    violations: list[str] = []

    if narrative_draft is not None:
        for s_item in getattr(narrative_draft, "situation_items", ()):
            body_text = getattr(s_item, "body", "").strip()
            situation_lengths.append(len(body_text))
            if len(body_text) > 360:
                violations.append(
                    f"Situation item '{getattr(s_item, 'label', '')}' exceeds 360 chars: {len(body_text)}"
                )

        for block in getattr(narrative_draft, "blocks", ()):
            for item in getattr(block, "items", ()):
                hl = getattr(item, "headline", "").strip()
                bd = getattr(item, "body", "").strip()
                headline_lengths.append(len(hl))
                detail_lengths.append(len(bd))
                if len(hl) > 140:
                    violations.append(f"Scan headline exceeds 140 chars: {len(hl)} ('{hl[:30]}...')")
                if len(bd) > 900:
                    violations.append(f"Detail body exceeds 900 chars: {len(bd)}")

    return {
        "max_headline_len": max(headline_lengths, default=0),
        "avg_headline_len": sum(headline_lengths) / len(headline_lengths) if headline_lengths else 0,
        "max_situation_len": max(situation_lengths, default=0),
        "max_detail_len": max(detail_lengths, default=0),
        "violations": violations,
        "is_valid": len(violations) == 0,
    }


def validate_benchmark_gates(metrics: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    if metrics.get("article_fallback_content_attempts", 0) != 0:
        violations.append(
            f"article_fallback_content_attempts != 0 ({metrics['article_fallback_content_attempts']})"
        )
    if metrics.get("article_publications", 0) > metrics.get("article_writer_successes", 0):
        violations.append(
            f"article_publications ({metrics['article_publications']}) > "
            f"article_writer_successes ({metrics['article_writer_successes']})"
        )
    if metrics.get("max_article_writer_calls_per_run", 0) > 1:
        violations.append(
            f"max_article_writer_calls_per_run ({metrics['max_article_writer_calls_per_run']}) > 1"
        )
    for r in metrics.get("runs", []):
        if r.publication_type == "article" and (
            r.publication_id is not None or r.status == "succeeded"
        ):
            if not any(a.kind == "writer" and a.status == "succeeded" for a in r.attempts):
                violations.append(
                    f"published article run {r.run_id} has no succeeded writer attempt"
                )
    return violations


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

        digest_win_kind = "none"
        digest_win_meta = {}
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

            if digest_pub is not None:
                cur = await conn.execute(
                    "SELECT kind, metadata FROM publication_generation_attempts WHERE id = %s",
                    (digest_pub.winning_generation_attempt_id,),
                )
                win_row = await cur.fetchone()
                digest_win_kind = win_row[0] if win_row else "unknown"
                digest_win_meta = win_row[1] if win_row else {}

            cur = await conn.execute(
                "SELECT status, error_kind FROM publication_runs WHERE id = %s",
                (digest_run.id,),
            )
            d_row = await cur.fetchone()
            digest_status = d_row[0] if d_row else "unknown"
            digest_error_kind = d_row[1] if d_row else None

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
            try:
                article_pub = await generation_service.generate(
                    article_run.id, defer_delivery=False
                )
            except ArticlePublicationRejected as exc:
                logger.warning(
                    "Article rejected during benchmark: %s (%s)", exc.reason, exc.error_kind
                )

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

            cur = await conn.execute(
                "SELECT status, error_kind FROM publication_runs WHERE id = %s",
                (article_run.id,),
            )
            a_row = await cur.fetchone()
            article_status = a_row[0] if a_row else "unknown"
            article_error_kind = a_row[1] if a_row else None

        article_chat_calls = sum(
            1
            for a in article_attempts
            if a[1] in ("writer", "editorializer") and a[4] not in ("deterministic", None)
        )
        claim_trace_count = (
            len(win_meta.get("claim_trace", [])) if isinstance(win_meta, dict) else 0
        )

        # 3. Candidate and Cluster Diagnostics
        async with uow.transaction() as conn:
            cur = await conn.execute(
                """
                SELECT c.id, c.story_id, r.title, r.summary, r.event_payload
                FROM publication_candidates c
                JOIN story_revisions r ON r.id = c.story_revision_id
                WHERE c.publication_run_id = %s
                """,
                (digest_run.id,),
            )
            cand_rows = await cur.fetchall()

        scope_counts: dict[str, int] = {}
        candidate_titles: list[str] = []
        for crow in cand_rows:
            p = crow[4]
            sc = p.get("geographic_scope", "UNKNOWN") if isinstance(p, dict) else "UNKNOWN"
            scope_counts[sc] = scope_counts.get(sc, 0) + 1
            if crow[2]:
                candidate_titles.append(crow[2])

        frag_pairs: list[tuple[str, str, float]] = []
        for i in range(len(candidate_titles)):
            tokens_i = set(candidate_titles[i].lower().split())
            for j in range(i + 1, len(candidate_titles)):
                tokens_j = set(candidate_titles[j].lower().split())
                if tokens_i and tokens_j:
                    jacc = len(tokens_i & tokens_j) / len(tokens_i | tokens_j)
                    if jacc >= 0.50:
                        frag_pairs.append((candidate_titles[i], candidate_titles[j], jacc))

        # Build records for structured metrics
        runs_records = [
            BenchmarkRunRecord(
                run_id=digest_run.id,
                publication_type="digest_grouped",
                status=digest_status,
                error_kind=digest_error_kind,
                publication_id=digest_pub.id if digest_pub else None,
                attempts=[
                    BenchmarkAttemptRecord(
                        kind=a[1],
                        status=a[2],
                        error_kind=a[3],
                        provider=a[4],
                        model=a[5],
                        metadata=a[6] or {},
                    )
                    for a in digest_attempts
                ],
            ),
            BenchmarkRunRecord(
                run_id=article_run.id,
                publication_type="article",
                status=article_status,
                error_kind=article_error_kind,
                publication_id=article_pub.id if article_pub else None,
                attempts=[
                    BenchmarkAttemptRecord(
                        kind=a[1],
                        status=a[2],
                        error_kind=a[3],
                        provider=a[4],
                        model=a[5],
                        metadata=a[6] or {},
                    )
                    for a in article_attempts
                ],
            ),
        ]

        metrics = calculate_benchmark_metrics(runs_records)
        gate_violations = validate_benchmark_gates(metrics)

        def _classify_outcome(
            attempts: list[Any],
            win_kind: str,
            win_meta: dict[str, Any],
            mode: str,
            run_error_kind: str | None = None,
        ) -> str:
            if run_error_kind == "article_validation_rejected":
                return "article_validation_rejected"
            if run_error_kind == "article_writer_rejected":
                return "article_writer_rejected"
            if mode == "deterministic":
                return "deterministic_selected"
            if win_kind == "writer" and win_meta.get("status") == "writer_success":
                return "writer_success"
            if any(
                a[1] == "writer" and a[2] == "failed" and a[3] == "article_validation_rejected"
                for a in attempts
            ):
                return "article_validation_rejected"
            if any(a[1] == "writer" and a[2] == "failed" for a in attempts):
                return "article_writer_rejected"
            if win_kind == "story_renderer_fallback":
                return "story_renderer_fallback"
            return "deterministic_selected"

        digest_mode = getattr(
            getattr(config.settings, "publication_editorial", None),
            "digest_narrative_mode",
            "deterministic",
        )
        digest_outcome = _classify_outcome(
            digest_attempts,
            digest_win_kind,
            digest_win_meta,
            digest_mode,
            digest_error_kind,
        )
        article_outcome = _classify_outcome(
            article_attempts,
            win_kind,
            win_meta,
            "single_call",
            article_error_kind,
        )

        # Print Benchmark Report
        print("\n" + "=" * 70)
        print("  PUBLICATION QUALITY & AI BUDGET BENCHMARK REPORT")
        print("=" * 70)
        print(f"Edition:             {name} ({slug})")
        print(f"Lookback Window:     {hours} hours")
        print(f"Timestamp:           {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("-" * 70)
        print("CANDIDATE & CLUSTER DIAGNOSTICS:")
        print(f"  Total Candidates:  {len(cand_rows)}")
        print(f"  Scope Breakdown:   {scope_counts}")
        print(f"  Potential Cluster Frags (Jaccard >= 0.50): {len(frag_pairs)}")
        for t1, t2, jscore in frag_pairs[:3]:
            print(f"    - [{jscore:.2f}] '{t1[:35]}...' vs '{t2[:35]}...'")
        print("-" * 70)
        print("DIGEST RESULTS:")
        print(f"  Candidates:        {len(digest_candidates)}")
        print(f"  Selected:          {len(digest_inputs)}")
        print(f"  Publication ID:    {digest_pub.id if digest_pub else 'N/A (no inputs)'}")
        print(f"  Length (chars):    {len(digest_pub.body or '') if digest_pub else 0}")
        print(f"  Mode:              {digest_mode}")
        print(f"  Outcome Status:    {digest_outcome}")
        print(f"  Winning Attempt:   {digest_win_kind}")
        if digest_win_meta and "block_count" in digest_win_meta:
            print(f"  Narrative Blocks:  {digest_win_meta['block_count']}")
        print(
            f"  Chat LLM Calls:    {digest_chat_calls} (Target: <= 1 in single_call, 0 in deterministic)"
        )
        print(f"  Duration:          {t_digest:.2f}s")
        print("-" * 70)
        print("ARTICLE RESULTS:")
        print(f"  Candidates:        {len(article_candidates)}")
        print(f"  Selected:          {len(article_inputs)}")
        print(
            f"  Publication ID:    {article_pub.id if article_pub else 'N/A (rejected or no inputs)'}"
        )
        print(f"  Title:             {article_pub.title if article_pub else 'N/A'}")
        print(
            f"  Word count:        {len((article_pub.body or '').split()) if article_pub else 0} words"
        )
        print(f"  Outcome Status:    {article_outcome}")
        print(f"  Winning Attempt:   {win_kind}")
        print(f"  Claim Trace Units: {claim_trace_count}")
        print(f"  Chat LLM Calls:    {article_chat_calls} (Target: <= 1)")
        print(f"  Duration:          {t_article:.2f}s")
        print("-" * 70)
        print("ARTICLE RELIABILITY & BUDGET:")
        print(f"  Article writer attempts:      {metrics['article_writer_attempts']}")
        print(
            f"  Article writer successes:     {metrics['article_writer_successes']} "
            f"({metrics['article_writer_success_rate'] * 100:.1f}%)"
        )
        print(f"  Article rejections:           {metrics['article_rejections']}")
        print(f"  Article fallback attempts:    {metrics['article_fallback_content_attempts']}")
        print(f"  Article max writer calls/run: {metrics['max_article_writer_calls_per_run']}")
        if gate_violations:
            print(f"  GATE VIOLATIONS:              {gate_violations}")

        if article_pub and article_pub.body:
            from src.publication.article_models import (
                ArticleParagraph,
                ArticleSection,
                StructuredArticleDraft,
            )
            from src.publication.narrative_quality import evaluate_article_narrative

            raw_paras = [
                p.strip()
                for p in article_pub.body.split("\n\n")
                if p.strip() and not p.startswith("#")
            ]
            draft_for_diag = StructuredArticleDraft(
                title=article_pub.title or "",
                title_support_ids=(),
                lead=article_pub.lead or "",
                lead_support_ids=(),
                sections=(
                    ArticleSection(
                        heading="",
                        paragraphs=tuple(ArticleParagraph(text=p) for p in raw_paras),
                    ),
                ),
            )
            diag_rep = evaluate_article_narrative(draft_for_diag)
            print("-" * 70)
            print("NARRATIVE DIAGNOSTICS (OBSERVABILITY):")
            print(f"  Paragraphs:        {diag_rep.paragraph_count}")
            print(f"  Synthesis Ratio:   {diag_rep.synthesis_ratio:.2f}")
            print(f"  Attribution Starts:{diag_rep.repeated_attribution_starts}")
            print(f"  DB Label Patterns: {diag_rep.database_label_patterns}")
            print(f"  Direct Quotes:     {diag_rep.direct_quote_count}")
            print(f"  Avg Sent / Para:   {diag_rep.avg_sentences_per_paragraph:.2f}")
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
