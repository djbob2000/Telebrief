"""Generate and compare long-form editorial articles: New Event-First Approach vs Old Custom Approach."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.article_generator import ArticleGenerator
from src.bootstrap import build_infrastructure
from src.collector import Message
from src.config_loader import Config, load_config
from src.publication.errors import ArticlePublicationRejected
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService
from src.repositories.editions import EditionRepository
from src.runtime import install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("compare_articles")


async def generate_new_approach_article(infra, config: Config) -> tuple[str, str, str, dict]:
    """Generate long-form article using current Event-First Reader-First approach."""
    from src.ai_providers import ProviderCascade

    ProviderCascade._global_slot_cooldowns.clear()

    uow = infra.uow
    repo = PublicationRepository()

    now = dt.datetime.now(dt.timezone.utc)
    edition_slug = "berdyansk"

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
        request_key=f"compare:article:new:{now.isoformat()}",
        config=config,
        lookback_hours_override=48,
    )
    await snapshot_service.seal_candidates(run.id)
    await selection_service.select(run.id, defer_generation=False)
    pub = await generation_service.generate(run.id, defer_delivery=False)

    meta = {}
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

    return pub.title or "", pub.lead or "", pub.body or "", meta


async def generate_old_custom_article(infra, config: Config) -> tuple[str, str, str]:
    """Generate article using old/custom approach (EditorialAnalyzer + StoryCards + EditorialWriter)."""
    uow = infra.uow
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)

    async with uow.transaction() as conn:
        cur = await conn.execute(
            """
            SELECT s.name, i.external_id, i.published_at, i.canonical_url, r.text_content
            FROM source_items i
            JOIN sources s ON s.id = i.source_id
            JOIN source_item_revisions r ON r.source_item_id = i.id
            WHERE i.published_at >= %s AND r.text_content IS NOT NULL AND LENGTH(TRIM(r.text_content)) > 20
            ORDER BY i.published_at DESC
            LIMIT 60
            """,
            (cutoff,),
        )
        rows = await cur.fetchall()

    if not rows:
        return (
            "Нет свежих сообщений",
            "Нет данных",
            "Нет сообщений за 48 часов для генерации старой статьи.",
        )

    messages = [
        Message(
            text=text,
            sender=src_name,
            channel_name=src_name,
            timestamp=pub_at,
            message_id=int(ext_id) if str(ext_id).isdigit() else idx,
            link=url or "#",
        )
        for idx, (src_name, ext_id, pub_at, url, text) in enumerate(rows, start=1)
    ]

    from collections import defaultdict

    messages_by_channel: dict[str, list[Message]] = defaultdict(list)
    for msg in messages:
        messages_by_channel[msg.channel_name].append(msg)

    from src.ai_providers import ProviderCascade

    ProviderCascade._global_slot_cooldowns.clear()

    generator = ArticleGenerator(config=config, logger=logger)
    title, lead, body = await generator.generate_article(dict(messages_by_channel))
    return title, lead, body


async def main():
    config = load_config()
    print("\n" + "=" * 80, flush=True)
    print("🚀 GENERATING ARTICLES FOR COMPARISON", flush=True)
    print(f"Provider: {config.settings.ai_provider} | Model: {config.settings.ai_model}", flush=True)
    print("=" * 80 + "\n", flush=True)

    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    print(
        "⏳ [1/2] Generating Article with NEW Approach (Event-First Evidence-Bound + Adaptive Length)...",
        flush=True,
    )
    t0 = asyncio.get_event_loop().time()
    new_title, new_lead, new_body = "", "", ""
    try:
        new_title, new_lead, new_body, new_meta = await generate_new_approach_article(infra, config)
        new_time = asyncio.get_event_loop().time() - t0
        print(f"✅ NEW Article Generated in {new_time:.2f}s\n", flush=True)
    except ArticlePublicationRejected as e:
        new_time = asyncio.get_event_loop().time() - t0
        new_title = "🛑 ОТКЛОНЕНО ВАЛИДАЦИЕЙ (Fail-Closed Rejection)"
        new_lead = f"Причина отказа: {e.reason} ({e.error_kind})"
        violations = e.metadata.get("violations", [])
        new_body = (
            f"Статья отклонена, публикация не создана (0 строк в БД).\n"
            f"Нарушения доказательной границы ({len(violations)}):\n"
            + "\n".join(f"  - {v}" for v in violations[:10])
        )
        print(f"🛑 NEW Article REJECTED (Fail-Closed) in {new_time:.2f}s: {e.reason}\n", flush=True)
    except Exception as e:
        logger.exception("Failed to generate new article: %s", e)
        new_title = "❌ ОШИБКА ГЕНЕРАЦИИ"
        new_body = str(e)

    print(
        "⏳ [2/2] Generating Article with OLD Approach (Custom branch Story Cards + Multi-pass Writer)...",
        flush=True,
    )
    t0 = asyncio.get_event_loop().time()
    old_title, old_lead, old_body = "", "", ""
    try:
        old_title, old_lead, old_body = await generate_old_custom_article(infra, config)
        old_time = asyncio.get_event_loop().time() - t0
        print(f"✅ OLD Article Generated in {old_time:.2f}s\n", flush=True)
    except Exception as e:
        logger.exception("Failed to generate old article: %s", e)
        old_title = "❌ ОШИБКА СТАРОГО ПОДХОДА"
        old_body = str(e)

    # Print New Article
    print("\n" + "█" * 80, flush=True)
    print("  📰 [НОВЫЙ ПОДХОД: Reader-First Selective Long-Read Article]", flush=True)
    print("█" * 80 + "\n", flush=True)
    print(f"TITLE: {new_title}\n", flush=True)
    print(f"LEAD: {new_lead}\n", flush=True)
    print(new_body, flush=True)
    print("\n" + "─" * 80, flush=True)

    # Print Old Article
    print("\n" + "█" * 80, flush=True)
    print("  📜 [СТАРЫЙ ПОДХОД С ВЕТКИ CUSTOM: Story Card Article]", flush=True)
    print("█" * 80 + "\n", flush=True)
    print(f"TITLE: {old_title}\n", flush=True)
    print(f"LEAD: {old_lead}\n", flush=True)
    print(old_body, flush=True)
    print("\n" + "─" * 80, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
