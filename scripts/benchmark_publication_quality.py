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
        if r.status == "failed"
        and (
            r.error_kind in ("article_validation_rejected", "article_writer_rejected")
            or any(a.kind == "writer" and a.status == "failed" for a in r.attempts)
        )
    )
    article_publications = sum(
        1 for r in article_runs if r.publication_id is not None or r.status == "succeeded"
    )
    article_full_fallbacks = sum(
        1
        for r in article_runs
        for a in r.attempts
        if a.status == "succeeded"
        and (
            a.kind in ("story_renderer_fallback", "deterministic_fallback")
            or a.metadata.get("winning_kind") == "event_article_deterministic_fallback"
            or a.metadata.get("deterministic_article_fallback_used") is True
        )
    )
    article_supplements = sum(
        1
        for r in article_runs
        for a in r.attempts
        if a.status == "succeeded"
        and a.metadata.get("winning_kind") == "event_article_writer_with_supplement"
    )
    article_fallback_content_attempts = article_full_fallbacks
    article_writer_success_rate = (
        article_writer_successes / article_writer_attempts if article_writer_attempts > 0 else 0.0
    )
    article_full_fallback_rate = (
        article_full_fallbacks / article_publications if article_publications > 0 else 0.0
    )
    article_supplement_rate = (
        article_supplements / article_publications if article_publications > 0 else 0.0
    )
    max_article_writer_calls_per_run = max(
        [sum(1 for a in r.attempts if a.kind == "writer") for r in article_runs],
        default=0,
    )

    published_article_story_coverage: float = 1.0
    unsupported_final_claim_count: int = 0
    leaked_directory_payload_count: int = 0

    for r in article_runs:
        if r.publication_id is not None or r.status == "succeeded":
            succ_attempts = [a for a in r.attempts if a.status == "succeeded"]
            if succ_attempts:
                last_succ = succ_attempts[-1]
                meta = last_succ.metadata or {}
                cov = meta.get("final_story_coverage")
                if cov is not None:
                    published_article_story_coverage = min(
                        published_article_story_coverage, float(cov)
                    )
                unsupported_final_claim_count += int(meta.get("unsupported_final_claim_count", 0))
                leaked_directory_payload_count += int(meta.get("leaked_directory_payload_count", 0))

    # Digest Metrics
    digest_runs = [r for r in runs if r.publication_type in ("digest_grouped", "digest_channel")]
    digest_publications = sum(
        1 for r in digest_runs if r.publication_id is not None or r.status == "succeeded"
    )
    digest_full_fallbacks = sum(
        1
        for r in digest_runs
        for a in r.attempts
        if a.status == "succeeded"
        and (
            a.kind in ("story_renderer_fallback", "deterministic_fallback")
            or a.metadata.get("deterministic_digest_fallback_used") is True
        )
    )
    digest_full_fallback_rate = (
        digest_full_fallbacks / digest_publications if digest_publications > 0 else 0.0
    )
    published_digest_story_coverage: float = 1.0
    planned_story_count = 0
    dashboard_only_count = 0
    detail_only_count = 0
    dashboard_and_drilldown_count = 0

    for r in digest_runs:
        if r.publication_id is not None or r.status == "succeeded":
            succ_attempts = [a for a in r.attempts if a.status == "succeeded"]
            if succ_attempts:
                last_succ = succ_attempts[-1]
                meta = last_succ.metadata or {}
                cov = meta.get("final_digest_story_coverage")
                if cov is not None:
                    published_digest_story_coverage = min(
                        published_digest_story_coverage, float(cov)
                    )
                planned_story_count += int(meta.get("planned_story_count", 0))
                dashboard_only_count += int(meta.get("dashboard_only_count", 0))
                detail_only_count += int(meta.get("detail_only_count", 0))
                dashboard_and_drilldown_count += int(meta.get("dashboard_and_drilldown_count", 0))

    return {
        "runs": runs,
        "article_runs": len(article_runs),
        "article_writer_attempts": article_writer_attempts,
        "article_writer_successes": article_writer_successes,
        "article_rejections": article_rejections,
        "article_publications": article_publications,
        "article_full_fallbacks": article_full_fallbacks,
        "article_supplements": article_supplements,
        "article_fallback_content_attempts": article_fallback_content_attempts,
        "article_writer_success_rate": article_writer_success_rate,
        "article_full_fallback_rate": article_full_fallback_rate,
        "article_supplement_rate": article_supplement_rate,
        "max_article_writer_calls_per_run": max_article_writer_calls_per_run,
        "final_story_coverage": published_article_story_coverage,
        "unsupported_final_claim_count": unsupported_final_claim_count,
        "leaked_directory_payload_count": leaked_directory_payload_count,
        "digest_runs": len(digest_runs),
        "digest_publications": digest_publications,
        "digest_full_fallbacks": digest_full_fallbacks,
        "digest_full_fallback_rate": digest_full_fallback_rate,
        "final_digest_story_coverage": published_digest_story_coverage,
        "planned_story_count": planned_story_count,
        "dashboard_only_count": dashboard_only_count,
        "detail_only_count": detail_only_count,
        "dashboard_and_drilldown_count": dashboard_and_drilldown_count,
    }


def validate_benchmark_gates(metrics: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    max_calls = metrics.get("max_article_writer_calls_per_run", 0)
    if max_calls > 1:
        violations.append(f"max_article_writer_calls_per_run={max_calls} exceeds 1")

    if metrics.get("article_publications", 0) > 0:
        cov = metrics.get("final_story_coverage", 1.0)
        if cov != 1.0:
            violations.append(f"final_story_coverage={cov} expected 1.0")
        unsupp = metrics.get("unsupported_final_claim_count", 0)
        if unsupp != 0:
            violations.append(f"unsupported_final_claim_count={unsupp} expected 0")
        leaked = metrics.get("leaked_directory_payload_count", 0)
        if leaked != 0:
            violations.append(f"leaked_directory_payload_count={leaked} expected 0")

    if metrics.get("digest_publications", 0) > 0:
        d_cov = metrics.get("final_digest_story_coverage", 1.0)
        if d_cov != 1.0:
            violations.append(f"final_digest_story_coverage={d_cov} expected 1.0")

    for r in metrics.get("runs", []):
        if r.status == "failed":
            if r.error_kind in ("ArticleFinalizationInvariantError", "DigestCoverageInvariantError"):
                violations.append(f"hard invariant failure in run {r.run_id}: {r.error_kind}")

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
        print("DIGEST CORRECTNESS:")
        print(f"  Candidates:                 {len(digest_candidates)}")
        print(f"  Selected:                   {len(digest_inputs)}")
        print(f"  Publication ID:             {digest_pub.id if digest_pub else 'N/A (no inputs)'}")
        print(f"  Final Digest Story Coverage:{metrics['final_digest_story_coverage'] * 100:.1f}%")
        print(f"  Planned Story Count:        {metrics['planned_story_count']}")
        print(f"  Dashboard Only:             {metrics['dashboard_only_count']}")
        print(f"  Detail Only:                {metrics['detail_only_count']}")
        print(f"  Dashboard & Drilldown:      {metrics['dashboard_and_drilldown_count']}")
        print("-" * 70)
        print("DIGEST WRITER QUALITY / SLO:")
        print(f"  Mode:                       {digest_mode}")
        print(f"  Outcome Status:             {digest_outcome}")
        print(f"  Winning Attempt:            {digest_win_kind}")
        print(f"  Full Fallback Rate:         {metrics['digest_full_fallback_rate'] * 100:.1f}%")
        if digest_win_meta and "block_count" in digest_win_meta:
            print(f"  Narrative Blocks:           {digest_win_meta['block_count']}")
        print(
            f"  Chat LLM Calls:             {digest_chat_calls} (Target: <= 1 in single_call, 0 in deterministic)"
        )
        print(f"  Duration:                   {t_digest:.2f}s")
        print("-" * 70)
        print("ARTICLE CORRECTNESS:")
        print(f"  Candidates:                 {len(article_candidates)}")
        print(f"  Selected:                   {len(article_inputs)}")
        print(
            f"  Publication ID:             {article_pub.id if article_pub else 'N/A (rejected or no inputs)'}"
        )
        print(f"  Final Story Coverage:       {metrics['final_story_coverage'] * 100:.1f}%")
        print(f"  Unsupported Final Claims:   {metrics['unsupported_final_claim_count']}")
        print(f"  Leaked Directory Payloads:  {metrics['leaked_directory_payload_count']}")
        print(f"  Max Writer Calls/Run:       {metrics['max_article_writer_calls_per_run']}")
        print("-" * 70)
        print("ARTICLE WRITER QUALITY / SLO:")
        print(f"  Outcome Status:             {article_outcome}")
        print(f"  Winning Attempt:            {win_kind}")
        print(f"  Claim Trace Units:          {claim_trace_count}")
        print(f"  Article Writer Attempts:    {metrics['article_writer_attempts']}")
        print(
            f"  Article Writer Successes:   {metrics['article_writer_successes']} "
            f"({metrics['article_writer_success_rate'] * 100:.1f}%)"
        )
        print(
            f"  Article Full Fallbacks:     {metrics['article_full_fallbacks']} "
            f"({metrics['article_full_fallback_rate'] * 100:.1f}%)"
        )
        print(
            f"  Article Supplements:        {metrics['article_supplements']} "
            f"({metrics['article_supplement_rate'] * 100:.1f}%)"
        )
        print(f"  Chat LLM Calls:             {article_chat_calls} (Target: <= 1)")
        print(f"  Duration:                   {t_article:.2f}s")
        if gate_violations:
            print(f"  GATE VIOLATIONS:            {gate_violations}")

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
