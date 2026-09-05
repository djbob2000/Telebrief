"""Pull fresh Telegram data, coalesce events, generate digest and article, save to MD files.

Pipeline:
  1. Scan all Telegram sources (fresh messages)
  2. Fragment + embed + cluster new revisions
  3. Coalesce dirty stories through Gate triage (quiet-window bypassed)
  4. Generate digest_grouped → save digest_YYYY-MM-DD.md
  5. Generate article → save article_YYYY-MM-DD.md
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.domain.editions import NewEdition
from src.ingestion.registry import SourceRegistry
from src.jobs.event_processing import coalesce_dirty_stories_task, process_event_revisions_task
from src.jobs.ingestion import collector_registry, scan_source
from src.publication.errors import ArticlePublicationRejected
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService
from src.repositories.editions import EditionRepository
from src.repositories.sources import SourceRepository
from src.runtime import install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fresh_pipeline")

EDITION_SLUG = "berdyansk"
OUTPUT_DIR = Path(__file__).resolve().parent.parent  # workspace root


async def main() -> None:
    config = load_config()
    infra = await build_infrastructure(config.database)
    infra.config = config
    install_runtime(infra)

    uow = infra.uow

    # ── 1. Ensure edition and sources exist ──────────────────────────────────
    logger.info("=== STEP 1: Bootstrap edition and sources ===")
    async with uow.transaction() as conn:
        edition = await EditionRepository().get_by_slug(conn, EDITION_SLUG)
        if edition is None:
            edition = await EditionRepository().create(
                conn,
                NewEdition(
                    slug=EDITION_SLUG,
                    name="Бердянск",
                    timezone="Europe/Zaporozhye",
                    language="ru",
                ),
            )
        reg = SourceRegistry()
        res = await reg.bootstrap_from_config(conn, config)
        logger.info(
            "Bootstrap: %d created, %d updated, %d bindings",
            res.sources_created,
            res.sources_updated,
            res.bindings_created,
        )
        sources = await SourceRepository().list_enabled(conn)
        logger.info("Enabled sources: %d", len(sources))

    # ── 2. Scan all sources ──────────────────────────────────────────────────
    logger.info("=== STEP 2: Scanning Telegram sources (concurrency 10) ===")
    scan_sem = asyncio.Semaphore(10)

    async def _scan_one(s):
        async with scan_sem:
            try:
                logger.info("  Scanning %s (%s)...", s.name, s.external_id)
                await scan_source(s.id, "manual")
            except Exception as exc:
                logger.warning("  Failed to scan %s: %s", s.name, exc)

    await asyncio.gather(*[_scan_one(s) for s in sources])

    # ── 3. Fragment + cluster all unfragmented revisions ─────────────────────
    logger.info("=== STEP 3: Fragmenting + clustering new revisions ===")
    async with uow.transaction() as conn:
        cur = await conn.execute(
            """
            SELECT sir.id
            FROM source_item_revisions sir
            JOIN source_items si ON si.id = sir.source_item_id
            LEFT JOIN source_fragments f ON f.source_item_revision_id = sir.id
            WHERE f.id IS NULL
              AND si.published_at >= now() - interval '24 hours'
              AND length(trim(coalesce(sir.text_content, ''))) > 0
            ORDER BY sir.id ASC
            """
        )
        rev_ids = [r[0] for r in await cur.fetchall()]

    if rev_ids:
        batch_size = 128
        logger.info(
            "Processing %d unfragmented revisions in batches of %d...", len(rev_ids), batch_size
        )
        for i in range(0, len(rev_ids), batch_size):
            batch = rev_ids[i : i + batch_size]
            stats = await process_event_revisions_task(batch)
            logger.info(
                "  Batch %d-%d: %s",
                i + 1,
                min(i + batch_size, len(rev_ids)),
                stats,
            )
    else:
        logger.info("No unfragmented revisions found.")

    # ── 4. Coalesce dirty stories (Gate triage, quiet-window bypassed) ───────
    logger.info("=== STEP 4: Coalescing dirty stories through Gate triage ===")
    # Bypass the quiet-window so freshly-arrived fragments are processed immediately
    fast_ep = dataclasses.replace(config.settings.event_pipeline, analysis_quiet_seconds=0)
    fast_settings = dataclasses.replace(config.settings, event_pipeline=fast_ep)
    config = dataclasses.replace(config, settings=fast_settings)
    infra.config = config

    for pass_num in range(1, 20):
        stats = await coalesce_dirty_stories_task(edition_id=edition.id)
        logger.info("Coalesce pass %d: %s", pass_num, stats)
        if stats.get("scanned", 0) == 0 or stats.get("gated", 0) == 0:
            logger.info("No more dirty stories — coalesce complete.")
            break

    # ── 5. Generate digest ───────────────────────────────────────────────────
    # Take fresh snapshot_at AFTER all triage decisions have been written so
    # the authority gap check in seal_candidates sees them.
    now = dt.datetime.now(dt.timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    local_now_str = (now + dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")  # Zaporozhye UTC+3

    logger.info("=== STEP 5: Generating Event-First Digest (snapshot %s) ===", now.isoformat())
    repo = PublicationRepository()
    snapshot_service = PublicationSnapshotService(uow=uow, repo=repo)
    selection_service = EditorialSelectionService(uow=uow, repo=repo, config=config)
    generation_service = PublicationGenerationService(uow=uow, config=config, repo=repo)

    digest_run = await snapshot_service.create_run(
        edition_id=edition.id,
        publication_type="digest_grouped",
        snapshot_at=now,
        request_key=f"cli:digest:{now.isoformat()}",
        config=config,
        lookback_hours_override=24,
    )
    await snapshot_service.seal_candidates(digest_run.id)
    await selection_service.select(digest_run.id, defer_generation=False)
    pub_digest = await generation_service.generate(digest_run.id, defer_delivery=False)
    logger.info("Digest generated: %s", pub_digest.title)

    # Save digest
    digest_path = OUTPUT_DIR / f"digest_{date_str}.md"
    digest_lines = [f"# {pub_digest.title}", ""]
    if pub_digest.lead and pub_digest.lead.strip():
        digest_lines += [pub_digest.lead.strip(), ""]
    digest_lines.append(pub_digest.body.strip())
    digest_path.write_text("\n".join(digest_lines), encoding="utf-8")
    logger.info("Digest written → %s (%d bytes)", digest_path, digest_path.stat().st_size)

    # ── 6. Generate article ──────────────────────────────────────────────────
    logger.info("=== STEP 6: Generating Event-First Long-Read Article ===")
    article_run = await snapshot_service.create_run(
        edition_id=edition.id,
        publication_type="article",
        snapshot_at=now,
        request_key=f"cli:article:{now.isoformat()}",
        config=config,
        lookback_hours_override=24,
    )
    await snapshot_service.seal_candidates(article_run.id)
    await selection_service.select(article_run.id, defer_generation=False)

    article_path = OUTPUT_DIR / f"article_{date_str}.md"
    try:
        pub_article = await generation_service.generate(article_run.id, defer_delivery=False)
        logger.info("Article generated: %s", pub_article.title)

        article_lines = [f"# {pub_article.title}", ""]
        if pub_article.lead and pub_article.lead.strip():
            article_lines += [pub_article.lead.strip(), ""]
        article_lines.append(pub_article.body.strip())
        article_path.write_text("\n".join(article_lines), encoding="utf-8")
        logger.info("Article written → %s (%d bytes)", article_path, article_path.stat().st_size)

    except ArticlePublicationRejected as exc:
        logger.error("Article rejected by Evidence Boundary: %s (%s)", exc.reason, exc.error_kind)
        violations = exc.metadata.get("violations", [])
        logger.error("Violations (%d): %s", len(violations), violations[:5])
        article_path.write_text(
            f"# РЕДАКЦИОННАЯ ПАУЗА\n\nПричина: {exc.reason} ({exc.error_kind})\n\n"
            + "\n".join(f"- {v}" for v in violations),
            encoding="utf-8",
        )

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "═" * 72)
    print(f"📌 ДАЙДЖЕСТ  →  {digest_path.name}")
    print(f"   Заголовок : {pub_digest.title}")
    print(f"   Объём     : {len(pub_digest.body)} симв.")
    print("─" * 72)
    try:
        print(f"📰 СТАТЬЯ    →  {article_path.name}")
        print(f"   Заголовок : {pub_article.title}")
        print(f"   Объём     : {len(pub_article.body)} симв.")
    except NameError:
        print(f"📰 СТАТЬЯ    →  {article_path.name}  (РЕДАКЦИОННАЯ ПАУЗА)")
    print(f"   Снимок    : {local_now_str} (UTC+3)")
    print("═" * 72 + "\n")

    await collector_registry.aclose()
    await infra.close()


if __name__ == "__main__":
    asyncio.run(main())
