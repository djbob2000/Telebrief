"""Generate and print the latest Event-First Reader-First Telegram digest."""

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

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")


async def main():
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
        publication_type="digest_grouped",
        snapshot_at=now,
        request_key=f"cli:digest:{now.isoformat()}",
        config=config,
    )
    await snapshot_service.seal_candidates(run.id)
    await selection_service.select(run.id, defer_generation=False)
    pub = await generation_service.generate(run.id, defer_delivery=False)

    async with uow.transaction() as conn:
        cur = await conn.execute(
            "SELECT kind, status, metadata FROM publication_generation_attempts WHERE publication_run_id = %s ORDER BY attempt_no ASC",
            (run.id,),
        )
        attempts = await cur.fetchall()

    print("\n" + "═" * 70)
    print(f"📌 ДАЙДЖЕСТ: {pub.title}")
    if pub.lead:
        print(f"\n{pub.lead}")
    print("═" * 70)
    print(f"\n{pub.body}")
    print("\n" + "═" * 70)

    date_str = now.strftime("%Y-%m-%d")
    out_file = Path(__file__).resolve().parent.parent / f"digest_{date_str}.md"
    digest_text = f"# {pub.title}\n\n"
    if pub.lead and pub.lead.strip():
        digest_text += f"{pub.lead.strip()}\n\n"
    digest_text += pub.body.strip() + "\n"
    out_file.write_text(digest_text, encoding="utf-8")
    print(f"\n[💾 Сохранено в файл: {out_file.name}]")

    if attempts:
        last_attempt = attempts[-1]
        kind, status, meta = last_attempt
        print(f"\n[Метаданные попытки #{len(attempts)}: {kind} -> {status}]")
        if meta and isinstance(meta, dict):
            if "prose_quality_audit" in meta:
                print(f"Диагностика качества текста: {meta['prose_quality_audit']}")


if __name__ == "__main__":
    asyncio.run(main())
