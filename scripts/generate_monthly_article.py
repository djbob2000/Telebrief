"""Generate and publish the Event-First Monthly Panorama Long-Read (Панорама месяца).

Usage:
    python scripts/generate_monthly_article.py [--edition berdyansk] [--month YYYY-MM] [--deliver] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.domain.editions import NewEdition
from src.publication.delivery import PublicationDeliveryService
from src.publication.errors import ArticlePublicationRejected
from src.publication.generation import PublicationGenerationService
from src.publication.renderers import render_longitudinal_telegram_teaser
from src.publication.repository import PublicationRepository
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService
from src.repositories.editions import EditionRepository
from src.runtime import install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Event-First Monthly Panorama Article (Панорама месяца)."
    )
    parser.add_argument(
        "--edition",
        default="berdyansk",
        help="Edition slug (default: berdyansk)",
    )
    parser.add_argument(
        "--month",
        default=None,
        help="Target month (YYYY-MM, defaults to current month)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Target snapshot date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--deliver",
        action="store_true",
        help="Deliver full article to Telegraph and teaser card to Telegram",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and inspect article without queuing delivery",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    from src.ai_providers import ProviderCascade

    ProviderCascade.reset_global_state()

    config = load_config()
    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    uow = infra.uow
    repo = PublicationRepository()

    tz = ZoneInfo(config.settings.timezone)
    if args.date:
        parsed_date = dt.date.fromisoformat(args.date)
        snapshot_at = dt.datetime(
            parsed_date.year, parsed_date.month, parsed_date.day, 20, 0, tzinfo=tz
        )
    elif args.month:
        parts = args.month.split("-")
        year, month = int(parts[0]), int(parts[1])
        # Next month 1st 20:00 to cover the target month
        if month == 12:
            snapshot_at = dt.datetime(year + 1, 1, 1, 20, 0, tzinfo=tz)
        else:
            snapshot_at = dt.datetime(year, month + 1, 1, 20, 0, tzinfo=tz)
    else:
        snapshot_at = dt.datetime.now(tz)

    edition_slug = args.edition

    async with uow.transaction() as conn:
        edition = await EditionRepository().get_by_slug(conn, edition_slug)
        if edition is None:
            edition = await EditionRepository().create(
                conn,
                NewEdition(
                    slug=edition_slug,
                    name="Бердянск",
                    timezone=config.settings.timezone,
                    language="ru",
                ),
            )

    snapshot_service = PublicationSnapshotService(uow=uow, repo=repo)
    selection_service = EditorialSelectionService(uow=uow, repo=repo, config=config)
    generation_service = PublicationGenerationService(uow=uow, config=config, repo=repo)
    delivery_service = PublicationDeliveryService(uow=uow, pub_repo=repo)

    req_key = f"cli:monthly_article:{edition_slug}:{snapshot_at.date().isoformat()}"

    print(f"\n🚀 Запуск генерации панорамы месяца для издания '{edition.name}'...")
    print(f"📅 Окно среза: {snapshot_at.isoformat()} (720 часов lookback)")

    run = await snapshot_service.create_run(
        edition_id=edition.id,
        publication_type="monthly_article",
        snapshot_at=snapshot_at,
        request_key=req_key,
        config=config,
    )
    await snapshot_service.seal_candidates(run.id)
    await selection_service.select(run.id, defer_generation=False)

    try:
        pub = await generation_service.generate(run.id, defer_delivery=True)
        print("\n" + "═" * 80)
        print(f"🏛 ПАНОРАМА МЕСЯЦА: {pub.title}")
        if pub.lead and not pub.body.strip().startswith(pub.lead.strip()):
            print(f"\n{pub.lead}")
        print("═" * 80)
        print(f"\n{pub.body}")
        print("\n" + "═" * 80)

        # Generate Telegram teaser preview
        teaser = render_longitudinal_telegram_teaser(
            publication_type="monthly_article",
            title=pub.title or "",
            lead=pub.lead or "",
            body=pub.body or "",
            telegraph_url=pub.metadata.get("telegraph_url", "https://telegra.ph/..."),
        )
        print("📱 ТИЗЕР-КАРТОЧКА ДЛЯ TELEGRAM:")
        print("─" * 80)
        print(teaser)
        print("═" * 80)

        if args.deliver and not args.dry_run:
            print("\n📤 Выполняется публикация в Telegra.ph и Telegram-канал...")
            deliveries = await delivery_service.prepare_payloads(pub.id)
            for d in deliveries:
                res = await delivery_service.deliver(d.id)
                print(
                    f"  ✅ Доставка [{res.id}]: статус={res.status}, ext_id={res.external_delivery_id}"
                )
            print("🎉 Панорама месяца успешно опубликована!")
        elif args.dry_run:
            print("\nℹ️ Режим dry-run: публикация не выполнялась.")
        else:
            print("\n💡 Добавьте флаг --deliver для публикации в Telegra.ph и Telegram.")

    except ArticlePublicationRejected as e:
        print("\n" + "═" * 80)
        print("🛑 ВАЛИДАЦИЯ ДОКАЗАТЕЛЬНОЙ ГРАНИЦЫ (Fail-Closed Rejection)")
        print(f"Причина: {e.reason} ({e.error_kind})")
        print("═" * 80)
        violations = e.metadata.get("violations", [])
        print(f"Всего нарушений доказательной границы: {len(violations)}")
        for v in violations[:15]:
            print(f"  • {v}")
    finally:
        await infra.close()


if __name__ == "__main__":
    asyncio.run(main())
