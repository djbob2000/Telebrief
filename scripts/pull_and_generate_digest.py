"""Pull latest messages from Telegram channels, process events, and generate digest."""

from __future__ import annotations

import asyncio
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
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService
from src.repositories.editions import EditionRepository
from src.repositories.sources import SourceRepository
from src.runtime import install_runtime
from src.utils import setup_logging

setup_logging("INFO")
logger = logging.getLogger("pull_and_generate")


async def main():
    config = load_config()
    infra = await build_infrastructure(config.database)
    infra.config = config
    install_runtime(infra)

    uow = infra.uow
    edition_slug = "berdyansk"

    logger.info("1. Bootstrapping sources and editions from config...")
    async with uow.transaction() as conn:
        edition = await EditionRepository().get_by_slug(conn, edition_slug)
        if edition is None:
            edition = await EditionRepository().create(
                conn,
                NewEdition(
                    slug=edition_slug,
                    name="Бердянск",
                    timezone="Europe/Zaporozhye",
                    language="ru",
                ),
            )
        reg = SourceRegistry()
        res = await reg.bootstrap_from_config(conn, config)
        logger.info(
            "Bootstrap result: %d sources created, %d updated, %d bindings created",
            res.sources_created,
            res.sources_updated,
            res.bindings_created,
        )

        sources = await SourceRepository().list_enabled(conn)
        logger.info("Found %d enabled sources for collection", len(sources))

    logger.info("2. Scanning sources via Telegram collector (concurrency 10)...")
    scan_sem = asyncio.Semaphore(10)

    async def _scan_one(s):
        async with scan_sem:
            try:
                logger.info("Scanning source %s (%s)...", s.name, s.external_id)
                await scan_source(s.id, "manual")
            except Exception as e:
                logger.warning("Failed to scan source %s: %s", s.name, e)

    await asyncio.gather(*[_scan_one(s) for s in sources])

    # 3. Find unfragmented revisions
    logger.info("3. Finding new revisions to process...")
    async with uow.transaction() as conn:
        cur = await conn.execute(
            """
            SELECT sir.id
            FROM source_item_revisions sir
            JOIN source_items si ON si.id = sir.source_item_id
            LEFT JOIN source_fragments f ON f.source_item_revision_id = sir.id
            WHERE f.id IS NULL AND si.published_at >= now() - interval '24 hours'
            ORDER BY sir.id ASC
            """
        )
        rows = await cur.fetchall()
        rev_ids = [r[0] for r in rows]

    if rev_ids:
        logger.info(
            "Processing %d new revisions (fragmentation, embeddings, clustering)...", len(rev_ids)
        )
        batch_size = 64
        for i in range(0, len(rev_ids), batch_size):
            batch = rev_ids[i : i + batch_size]
            logger.info(
                "Processing revision batch %d-%d of %d...",
                i + 1,
                min(i + batch_size, len(rev_ids)),
                len(rev_ids),
            )
            await process_event_revisions_task(batch)

    # 4. Coalesce dirty story clusters (Gate triage and analysis)
    logger.info("4. Coalescing dirty story clusters (Gate triage and analysis)...")
    import dataclasses

    new_ep = dataclasses.replace(config.settings.event_pipeline, analysis_quiet_seconds=0)
    new_settings = dataclasses.replace(config.settings, event_pipeline=new_ep)
    config = dataclasses.replace(config, settings=new_settings)
    infra.config = config

    for pass_num in range(1, 10):
        stats = await coalesce_dirty_stories_task(edition_id=edition.id)
        logger.info("Pass %d coalesce stats: %s", pass_num, stats)
        if stats.get("scanned", 0) == 0 or stats.get("gated", 0) == 0:
            break

    # 5. Generate digest
    logger.info("5. Generating Event-First Telegram Digest...")
    now = dt.datetime.now(dt.timezone.utc)
    repo = PublicationRepository()
    snapshot_service = PublicationSnapshotService(uow=uow, repo=repo)
    selection_service = EditorialSelectionService(uow=uow, repo=repo, config=config)
    generation_service = PublicationGenerationService(uow=uow, config=config, repo=repo)

    run = await snapshot_service.create_run(
        edition_id=edition.id,
        publication_type="digest_grouped",
        snapshot_at=now,
        request_key=f"cli:digest:fresh:{now.isoformat()}",
        config=config,
        lookback_hours_override=24,
    )
    await snapshot_service.seal_candidates(run.id)
    await selection_service.select(run.id, defer_generation=False)
    pub = await generation_service.generate(run.id, defer_delivery=False)

    print("\n" + "═" * 70)
    print(f"📌 ДАЙДЖЕСТ: {pub.title}")
    if pub.lead:
        print(f"\n{pub.lead}")
    print("═" * 70)
    print(f"\n{pub.body}")
    print("\n" + "═" * 70)

    await collector_registry.aclose()
    await infra.close()


if __name__ == "__main__":
    asyncio.run(main())
