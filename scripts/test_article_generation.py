import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService
from src.repositories.editions import EditionRepository
from src.runtime import install_runtime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_article")

async def main():
    config = load_config()
    infra = await build_infrastructure(config.database)
    infra.config = config
    install_runtime(infra)
    uow = infra.uow

    now = dt.datetime.now(dt.timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    async with uow.transaction() as conn:
        edition = await EditionRepository().get_by_slug(conn, "berdyansk")
    if not edition:
        print("Edition not found")
        return

    repo = PublicationRepository()
    snapshot_service = PublicationSnapshotService(uow=uow, repo=repo)
    selection_service = EditorialSelectionService(uow=uow, repo=repo, config=config)
    generation_service = PublicationGenerationService(uow=uow, config=config, repo=repo)

    logger.info("Creating article snapshot (24h lookback)...")
    article_run = await snapshot_service.create_run(
        edition_id=edition.id,
        publication_type="article",
        snapshot_at=now,
        request_key=f"test:article:{now.isoformat()}",
        config=config,
        lookback_hours_override=24,
    )
    logger.info("Sealing candidates...")
    await snapshot_service.seal_candidates(article_run.id)
    logger.info("Selecting stories for article...")
    await selection_service.select(article_run.id, defer_generation=False)
    logger.info("Generating article...")
    pub_article = await generation_service.generate(article_run.id, defer_delivery=False)
    logger.info("Article generated successfully: %s", pub_article.title)

    out_file = Path(f"article_fresh_{date_str}.md")
    out_file.write_text(f"# {pub_article.title}\n\n{pub_article.body}", encoding="utf-8")
    logger.info("Saved to %s (%d chars)", out_file, len(pub_article.body))

    await infra.close()

if __name__ == "__main__":
    asyncio.run(main())
