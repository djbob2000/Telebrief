"""Generate and print the latest Event-First Reader-First long-read Article."""

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
from src.publication.errors import ArticlePublicationRejected
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService
from src.repositories.editions import EditionRepository
from src.runtime import install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def main():
    from src.ai_providers import ProviderCascade

    ProviderCascade.reset_global_state()

    config = load_config()
    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    uow = infra.uow
    repo = PublicationRepository()

    now = dt.datetime.now(dt.timezone.utc)
    edition_slug = "berdyansk"

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

    snapshot_service = PublicationSnapshotService(uow=uow, repo=repo)
    selection_service = EditorialSelectionService(uow=uow, repo=repo, config=config)
    generation_service = PublicationGenerationService(uow=uow, config=config, repo=repo)

    run = await snapshot_service.create_run(
        edition_id=edition.id,
        publication_type="article",
        snapshot_at=now,
        request_key=f"cli:article:{now.isoformat()}",
        config=config,
    )
    await snapshot_service.seal_candidates(run.id)
    await selection_service.select(run.id, defer_generation=False)

    try:
        pub = await generation_service.generate(run.id, defer_delivery=True)
        print("\n" + "═" * 80)
        print(f"📰 СТАТЬЯ: {pub.title}")
        if pub.lead:
            print(f"\n{pub.lead}")
        print("═" * 80)
        print(f"\n{pub.body}")
        print("\n" + "═" * 80)
    except ArticlePublicationRejected as e:
        print("\n" + "═" * 80)
        print("🛑 ВАЛИДАЦИЯ ДОКАЗАТЕЛЬНОЙ ГРАНИЦЫ (Fail-Closed Rejection)")
        print(f"Причина: {e.reason} ({e.error_kind})")
        print("═" * 80)
        violations = e.metadata.get("violations", [])
        print(f"Всего нарушений доказательной границы: {len(violations)}")
        for v in violations[:15]:
            print(f"  • {v}")
        if "draft" in e.metadata:
            draft = e.metadata["draft"]
            print("\n" + "─" * 80)
            print("📝 ЧЕРНОВИК, СГЕНЕРИРОВАННЫЙ МОДЕЛЬЮ:")
            print("─" * 80)
            print(f"TITLE: {draft.get('title')}\n")
            print(f"LEAD: {draft.get('lead')}\n")
            if "paragraphs" in draft:
                for p in draft["paragraphs"]:
                    print(f"{p.get('text', '')}\n")
    finally:
        await infra.close()


if __name__ == "__main__":
    asyncio.run(main())
