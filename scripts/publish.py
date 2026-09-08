"""Unified publication runner for digests and articles (CLI)."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai_providers import ProviderCascade
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

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")


async def run_cli_publication(
    publication_type: str = "digest_grouped",
    edition_slug: str = "berdyansk",
    save_markdown: bool = True,
) -> int:
    """Run end-to-end publication pipeline for CLI tools."""
    ProviderCascade.reset_global_state()
    config = load_config()
    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    uow = infra.uow
    repo = PublicationRepository()
    now = dt.datetime.now(dt.timezone.utc)

    try:
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
            publication_type=publication_type,
            snapshot_at=now,
            request_key=f"cli:{publication_type}:{now.isoformat()}",
            config=config,
        )
        await snapshot_service.seal_candidates(run.id)
        await selection_service.select(run.id, defer_generation=False)

        pub = await generation_service.generate(
            run.id, defer_delivery=(publication_type == "article")
        )

        label = "📰 СТАТЬЯ" if publication_type == "article" else "📌 ДАЙДЖЕСТ"
        print("\n" + "═" * 70)
        print(f"{label}: {pub.title}")
        if pub.lead and not pub.body.strip().startswith(pub.lead.strip()):
            print(f"\n{pub.lead}")
        print("═" * 70)
        print(f"\n{pub.body}")
        print("\n" + "═" * 70)

        if save_markdown:
            date_str = now.strftime("%Y-%m-%d")
            prefix = "article" if publication_type == "article" else "digest"
            out_file = Path(__file__).resolve().parent.parent / f"{prefix}_{date_str}.md"
            doc_text = f"# {pub.title}\n\n"
            if pub.lead and pub.lead.strip():
                doc_text += f"{pub.lead.strip()}\n\n"
            doc_text += pub.body.strip() + "\n"
            out_file.write_text(doc_text, encoding="utf-8")
            print(f"\n[💾 Сохранено в файл: {out_file.name}]")

        async with uow.transaction() as conn:
            cur = await conn.execute(
                "SELECT kind, status, metadata FROM publication_generation_attempts WHERE publication_run_id = %s ORDER BY attempt_no ASC",
                (run.id,),
            )
            attempts = await cur.fetchall()

        if attempts:
            last_attempt = attempts[-1]
            kind, status, meta = last_attempt
            print(f"\n[Метаданные попытки #{len(attempts)}: {kind} -> {status}]")
            if meta and isinstance(meta, dict) and "prose_quality_audit" in meta:
                print(f"Диагностика качества текста: {meta['prose_quality_audit']}")

        return 0
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
        return 1
    except Exception as e:
        print(f"\n❌ Ошибка генерации публикации: {e}")
        return 1
    finally:
        await infra.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telebrief Publication CLI Runner")
    parser.add_argument(
        "--type",
        choices=["digest", "article"],
        default="digest",
        help="Publication type to generate (default: digest)",
    )
    parser.add_argument(
        "--edition",
        default="berdyansk",
        help="Edition slug (default: berdyansk)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not save markdown file to disk",
    )
    args = parser.parse_args()
    pub_type = "article" if args.type == "article" else "digest_grouped"
    exit_code = asyncio.run(
        run_cli_publication(
            publication_type=pub_type,
            edition_slug=args.edition,
            save_markdown=not args.no_save,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
