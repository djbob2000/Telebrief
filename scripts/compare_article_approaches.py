"""Compare Event-First article publication against frozen legacy semantic floor."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import Config, load_config
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService
from src.repositories.editions import EditionRepository
from src.runtime import install_runtime
from scripts.publication_regression import LegacyCoverageCase, evaluate_case

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("compare_articles")


async def generate_event_first_article(
    infra: Any,
    config: Config,
    edition_slug: str = "berdyansk",
    lookback_hours: int = 48,
) -> tuple[Any, dict[str, Any]]:
    """Generate long-form article using current Event-First publication pipeline."""
    from src.ai_providers import ProviderCascade

    ProviderCascade._global_slot_cooldowns.clear()

    uow = infra.uow
    repo = PublicationRepository()
    now = dt.datetime.now(dt.timezone.utc)

    async with uow.transaction() as conn:
        edition = await EditionRepository().get_by_slug(conn, edition_slug)
        if edition is None:
            raise ValueError(f"Edition {edition_slug} not found")

    snapshot_service = PublicationSnapshotService(uow=uow, repo=repo)
    selection_service = EditorialSelectionService(uow=uow, repo=repo, config=config)
    generation_service = PublicationGenerationService(uow=uow, config=config, repo=repo)

    run = await snapshot_service.create_run(
        edition_id=edition.id,
        publication_type="article",
        snapshot_at=now,
        request_key=f"compare:article:event_first:{now.isoformat()}",
        config=config,
        lookback_hours_override=lookback_hours,
    )
    await snapshot_service.seal_candidates(run.id)
    await selection_service.select(run.id, defer_generation=False)
    pub = await generation_service.generate(run.id, defer_delivery=False)

    meta: dict[str, Any] = {}
    async with uow.transaction() as conn:
        cur = await conn.execute(
            """
            SELECT kind, status, error_kind, metadata
            FROM publication_generation_attempts
            WHERE publication_run_id = %s
            ORDER BY attempt_no ASC
            """,
            (run.id,),
        )
        meta["attempts"] = await cur.fetchall()

    return pub, meta


async def run_comparison(
    legacy_fixture_path: str | Path,
    edition_slug: str = "berdyansk",
    lookback_hours: int = 48,
) -> None:
    config = load_config()
    fixture = LegacyCoverageCase.load_json(legacy_fixture_path)

    print("\n" + "=" * 80, flush=True)
    print("🚀 EVENT-FIRST PUBLICATION VS FROZEN LEGACY SEMANTIC FLOOR", flush=True)
    print(f"Edition: {edition_slug} | Hours: {lookback_hours} | Fixture: {fixture.id}", flush=True)
    print(f"Provider: {config.settings.ai_provider} | Model: {config.settings.ai_model}", flush=True)
    print("=" * 80 + "\n", flush=True)

    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    print("⏳ Generating Event-First Article...", flush=True)
    t0 = asyncio.get_event_loop().time()
    pub, meta = await generate_event_first_article(
        infra, config, edition_slug=edition_slug, lookback_hours=lookback_hours
    )
    dur = asyncio.get_event_loop().time() - t0
    print(f"✅ Generated in {dur:.2f}s\n", flush=True)

    # Extract trace from publication metadata
    pub_meta = pub.metadata if isinstance(pub.metadata, dict) else (json.loads(pub.metadata) if pub.metadata else {})
    claim_trace = pub_meta.get("article_claim_trace", {})
    trace_supports: list[str] = []
    if isinstance(claim_trace, dict):
        for u in claim_trace.get("units", []):
            trace_supports.extend(u.get("support_ids", []))

    exported_view = {
        "final_trace_supports": trace_supports,
        "final_trace_refs": trace_supports,
    }
    report = evaluate_case(fixture, exported_view)

    print("█" * 80, flush=True)
    print("  📰 EVENT-FIRST ARTICLE PUBLICATION", flush=True)
    print("█" * 80 + "\n", flush=True)
    print(f"TITLE: {pub.title}\n", flush=True)
    print(f"LEAD: {pub.lead}\n", flush=True)
    print(pub.body, flush=True)
    print("\n" + "─" * 80, flush=True)

    print("\n" + "█" * 80, flush=True)
    print("  📊 FROZEN LEGACY SEMANTIC FLOOR AUDIT", flush=True)
    print("█" * 80 + "\n", flush=True)
    print(f"Legacy substantive units:    {len(fixture.coverage_units)}")
    print(f"Legacy floor coverage:        {report.legacy_floor_coverage * 100:.1f}%")
    print(f"Microdetail retention:        {report.legacy_microdetail_retention * 100:.1f}%")
    print(f"Winning generation kind:      {pub_meta.get('winning_kind', 'N/A')}")
    print(f"Deterministic fallback used:  {pub_meta.get('deterministic_article_fallback_used', False)}")

    losses_by_stage: dict[str, list[str]] = {}
    for u in report.units:
        losses_by_stage.setdefault(u.loss.value, []).append(u.unit_id)

    print("\nUnits by pipeline stage:")
    for stage, uids in losses_by_stage.items():
        print(f"  [{stage}]: {len(uids)} units ({', '.join(uids[:4])}{'...' if len(uids) > 4 else ''})")
    print("=" * 80 + "\n", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare Event-First article against frozen legacy floor")
    parser.add_argument(
        "--legacy-fixture",
        type=str,
        default="tests/fixtures/berdyansk_2026_08_31_legacy_floor.json",
        help="Path to legacy floor JSON fixture",
    )
    parser.add_argument("--edition", type=str, default="berdyansk", help="Edition slug")
    parser.add_argument("--hours", type=int, default=48, help="Lookback hours")
    args = parser.parse_args()

    asyncio.run(
        run_comparison(
            legacy_fixture_path=args.legacy_fixture,
            edition_slug=args.edition,
            lookback_hours=args.hours,
        )
    )


if __name__ == "__main__":
    main()
