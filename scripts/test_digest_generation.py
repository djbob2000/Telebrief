"""Generate and save the fresh digest without Telegram delivery."""

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
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService
from src.repositories.editions import EditionRepository
from src.runtime import install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_digest")


async def main():
    config = load_config("config.yaml")
    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    uow = infra.uow
    repo = PublicationRepository()

    now = dt.datetime.now(dt.timezone.utc)
    edition_slug = "berdyansk"

    async with uow.transaction() as conn:
        edition = await EditionRepository().get_by_slug(conn, edition_slug)
        if edition is None:
            logger.error("Edition %s not found", edition_slug)
            return

    snapshot_service = PublicationSnapshotService(uow=uow, repo=repo)
    selection_service = EditorialSelectionService(uow=uow, repo=repo, config=config)
    generation_service = PublicationGenerationService(uow=uow, config=config, repo=repo)

    date_str = now.strftime("%Y-%m-%d")
    logger.info("Creating digest snapshot (24h lookback)...")
    digest_run = await snapshot_service.create_run(
        edition_id=edition.id,
        publication_type="digest_grouped",
        snapshot_at=now,
        request_key=f"test:digest:{now.isoformat()}",
        config=config,
        lookback_hours_override=24,
    )

    logger.info("Sealing candidates...")
    await snapshot_service.seal_candidates(digest_run.id)

    logger.info("Selecting stories for digest...")
    await selection_service.select(digest_run.id, defer_generation=False)

    logger.info("Generating digest (defer_delivery=True)...")
    pub_digest = await generation_service.generate(digest_run.id, defer_delivery=True)

    logger.info("Digest generated successfully: %s", pub_digest.title)

    out_file = Path(f"digest_fresh_{date_str}.md")
    content = f"# {pub_digest.title}\n\n"
    if pub_digest.lead:
        content += f"{pub_digest.lead}\n\n"
    content += pub_digest.body

    out_file.write_text(content, encoding="utf-8")
    logger.info("Saved fresh digest to %s (%d chars)", out_file, len(content))

    print("\n" + "═" * 70)
    print(f"📌 {pub_digest.title}")
    if pub_digest.lead:
        print(f"\n{pub_digest.lead}")
    print("═" * 70)
    print(f"\n{pub_digest.body}")
    print("═" * 70)


if __name__ == "__main__":
    asyncio.run(main())
