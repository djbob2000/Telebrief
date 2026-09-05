"""Process newly ingested revisions, coalesce dirty stories, and generate 24h digests.

Produces:
1. Canonical Event-First digest -> digest_2026-09-04.md
2. Compact journalistic Telegram digest -> digest_journalistic_2026-09-04.md
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.jobs.event_processing import coalesce_dirty_stories_task, process_event_revisions_task
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService
from src.repositories.editions import EditionRepository
from src.runtime import install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("digest_pipeline")

EDITION_SLUG = "berdyansk"
ROOT_DIR = Path(__file__).resolve().parent.parent

JOURNALISTIC_PROMPT_TEMPLATE = """Вы — старший редактор регионального издания, готовящий ежедневный вечерний Telegram-дайджест города {city} за {date}.

ВАША ЦЕЛЬ:
Сформировать живой, связный, информативный и легко сканируемый Telegram-дайджест на русском языке на основе проверенных городских сообщений за последние 24 часа.

СТРУКТУРА ДАЙДЖЕСТА:
Дайджест · {date}

[3-4 ключевые тематические рубрики, например:
⚡️ Коммунальная обстановка и ЖКХ
🚌 Транспорт, связь и сервисы
🎓 Образование и городская жизнь
📌 События и быт]

Внутри каждой рубрики:
• **Краткий заголовок факта**: плотное информативное раскрытие сути события в 1-2 предложениях с сохранением ключевой конкретики (улицы, номера домов, графики, цены, решения жителей).

ПРАВИЛА И СТИЛЬ:
1. Журналистский стиль: чистый, энергичный русский язык. Если исходное сообщение на украинском языке — точно и грамотно переведите его на русский.
2. Синтез и объединение: не пишите 10 отдельных пунктов про одно и то же отключение света. Объедините их в один цельный пункт, указав затронутые районы, улицы и характер проблемы.
3. Микродетали: сохраняйте конкретные улицы, микрорайоны (АКЗ, Колония, Слободка, Лиски, Центр), номера маршрутов, цены, технические подробности (генераторы, оптоволокно, врезки в подвалах).
4. Очистка от мусора: полностью исключайте флуд в чатах, спам телефонов, рекламу частных услуг, списки банков и длинные каталоги врачей.
5. Никаких шаблонных повторов («По сообщениям жителей... По сообщениям жителей...»). Вводные конструкции используйте естественно и разнообразно («По словам горожан...», «В местных чатах отмечают...», «Как рассказали жители...»).
6. СТРОГОЕ ОГРАНИЧЕНИЕ ДЛИНЫ (ОДНО СООБЩЕНИЕ TELEGRAM):
   - Дайджест ДОЛЖЕН целиком помещаться в ОДНО сообщение Telegram (жесткий лимит Telegram — 4096 символов).
   - Общий объём текста должен быть строго в диапазоне 2500–3500 знаков.
   - Сформируйте ровно 3–4 рубрики, в каждой — строго по 2–3 самых важных пункта. Никаких бесконечных списков!

МАТЕРИАЛЫ ДНЯ ДЛЯ ДАЙДЖЕСТА:
{content}
"""

CONDENSE_PROMPT_TEMPLATE = """Вы — выпускающий редактор регионального Telegram-канала города {city}.
Перед вами черновик вечернего дайджеста за {date}, который превышает допустимый лимит одного сообщения Telegram ({current_len} знаков при лимите {max_chars} знаков).

ВАША ЗАДАЧА:
Отредактировать и уплотнить текст так, чтобы его итоговая длина составила строго от 2500 до {target_chars} знаков, сохранив абсолютно ВСЕ факты, темы и рубрики.

ПРАВИЛА РЕДАКТУРЫ И КОМПРЕССИИ:
1. НЕ УДАЛЯЙТЕ события, рубрики или пункты. Все новости и темы должны остаться!
2. Уплотняйте синтаксис: убирайте многословие, вводные конструкции, пространные рассуждения и повторы.
3. Сохраняйте ВСЕ микродетали: названия улиц, номера домов, время, цены, имена, учреждения.
4. Объединяйте сложноподчиненные предложения в краткие, энергичные фразы.
5. Сохраните формат Telegram Markdown (заголовок, рубрики, эмодзи, маркеры • **Заголовок**: суть).
6. Верните ТОЛЬКО готовый отредактированный текст без вступительных или заключительных реплик.

ЧЕРНОВИК ДАЙДЖЕСТА ДЛЯ КОМПРЕССИИ:
{draft_text}
"""


def enforce_telegram_limit(text: str, max_chars: int = 3900) -> str:
    if len(text) <= max_chars:
        return text
    lines = text.splitlines(keepends=True)
    acc: list[str] = []
    cur_len = 0
    for line in lines:
        if cur_len + len(line) <= max_chars:
            acc.append(line)
            cur_len += len(line)
        else:
            break
    return "".join(acc).strip()


async def main():
    config = load_config()
    infra = await build_infrastructure(config.database)
    infra.config = config
    install_runtime(infra)
    uow = infra.uow

    async with uow.transaction() as conn:
        edition = await EditionRepository().get_by_slug(conn, EDITION_SLUG)
        if edition is None:
            raise RuntimeError(f"Edition {EDITION_SLUG} not found")

    # 1. Fragment + cluster unfragmented revisions
    logger.info("=== STEP 1: Fragmenting + clustering new revisions ===")
    async with uow.transaction() as conn:
        cur = await conn.execute(
            """
            SELECT sir.id
            FROM source_item_revisions sir
            JOIN source_items si ON si.id = sir.source_item_id
            LEFT JOIN source_fragments f ON f.source_item_revision_id = sir.id
            WHERE f.id IS NULL
              AND si.published_at >= now() - interval '48 hours'
              AND length(trim(coalesce(sir.text_content, ''))) > 0
            ORDER BY sir.id ASC
            """
        )
        rev_ids = [r[0] for r in await cur.fetchall()]

    if rev_ids:
        batch_size = 64
        logger.info("Processing %d unfragmented revisions in batches of %d...", len(rev_ids), batch_size)
        for i in range(0, len(rev_ids), batch_size):
            batch = rev_ids[i : i + batch_size]
            stats = await process_event_revisions_task(batch)
            logger.info("  Batch %d-%d: %s", i + 1, min(i + batch_size, len(rev_ids)), stats)
    else:
        logger.info("No unfragmented revisions found.")

    # 2. Coalesce dirty stories through Gate triage
    logger.info("=== STEP 2: Coalescing dirty stories through Gate triage ===")
    fast_ep = dataclasses.replace(config.settings.event_pipeline, analysis_quiet_seconds=0)
    fast_settings = dataclasses.replace(config.settings, event_pipeline=fast_ep)
    config = dataclasses.replace(config, settings=fast_settings)
    infra.config = config

    for pass_num in range(1, 20):
        stats = await coalesce_dirty_stories_task(edition_id=edition.id)
        logger.info("Coalesce pass %d: %s", pass_num, stats)
        if stats.get("scanned", 0) == 0 or stats.get("gated", 0) == 0:
            logger.info("Coalesce complete.")
            break

    # 3. Create snapshot for 24h window
    now = dt.datetime.now(dt.timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    date_human = now.strftime("%d.%m.%Y")
    logger.info("=== STEP 3: Creating publication run for 24h window (snapshot %s) ===", now.isoformat())

    repo = PublicationRepository()
    snapshot_service = PublicationSnapshotService(uow=uow, repo=repo)
    selection_service = EditorialSelectionService(uow=uow, repo=repo, config=config)
    generation_service = PublicationGenerationService(uow=uow, config=config, repo=repo)

    digest_run = await snapshot_service.create_run(
        edition_id=edition.id,
        publication_type="digest_grouped",
        snapshot_at=now,
        request_key=f"cli:digest:24h:{now.isoformat()}",
        config=config,
        lookback_hours_override=24,
    )
    await snapshot_service.seal_candidates(digest_run.id)
    await selection_service.select(digest_run.id, defer_generation=False)

    # 4. Generate canonical Event-First digest (defer_delivery=True to avoid sending to Telegram)
    logger.info("=== STEP 4: Generating canonical Event-First digest ===")
    pub_digest = await generation_service.generate(digest_run.id, defer_delivery=True)
    canonical_path = ROOT_DIR / f"digest_{date_str}.md"
    canonical_content = f"# {pub_digest.title}\n\n"
    if pub_digest.lead and pub_digest.lead.strip():
        canonical_content += f"{pub_digest.lead.strip()}\n\n"
    canonical_content += pub_digest.body.strip() + "\n"
    canonical_path.write_text(canonical_content, encoding="utf-8")
    logger.info("Canonical digest written to %s (%d chars)", canonical_path.name, len(canonical_content))

    # 5. Generate journalistic Telegram digest
    logger.info("=== STEP 5: Generating Journalistic Telegram Digest ===")
    adapter = EventEditorialAdapter(uow=uow)
    async with uow.transaction() as conn:
        frozen = await adapter.adapt_inputs_on(conn, digest_run.id)

    cards = frozen.analysis.cards
    logger.info("Loaded %d candidate cards for journalistic digest", len(cards))

    cards_text_blocks = []
    for c in cards:
        if not c.topic:
            continue
        facts = [f.text for f in getattr(c, "hard_facts", ()) if f.text]
        obs = [o.text for o in getattr(c, "community_observations", ()) if o.text]
        all_details = facts + obs
        details_str = "; ".join(all_details[:3]) if all_details else (c.summary or "")
        block = f"- [{c.topic}] {details_str}"
        cards_text_blocks.append(block)

    content_for_llm = "\n".join(cards_text_blocks[:45])

    city = "Бердянск"
    prompt = JOURNALISTIC_PROMPT_TEMPLATE.format(
        city=city,
        date=date_human,
        content=content_for_llm,
    )

    provider = getattr(generation_service.generator, "provider", None)
    chat_kwargs = {
        "messages": [{"role": "user", "content": prompt}],
        "model": "minimax/minimax-m3:free:floor",
    }
    t0 = time.time()
    raw_response = await provider.chat_completion(**chat_kwargs)
    elapsed = time.time() - t0
    logger.info("Journalistic digest pass 1 took %.2f seconds (%d chars)", elapsed, len(raw_response or ""))

    clean_draft = (raw_response or "").strip()
    if clean_draft.startswith("```markdown"):
        clean_draft = clean_draft[len("```markdown"):].strip()
    elif clean_draft.startswith("```"):
        clean_draft = clean_draft[3:].strip()
    if clean_draft.endswith("```"):
        clean_draft = clean_draft[:-3].strip()

    final_journalistic = clean_draft
    if len(final_journalistic) > 3900:
        logger.info("Condensing journalistic digest to fit single Telegram message...")
        condense_prompt = CONDENSE_PROMPT_TEMPLATE.format(
            city=city,
            date=date_human,
            current_len=len(final_journalistic),
            max_chars=3900,
            target_chars=3500,
            draft_text=final_journalistic,
        )
        condensed_res = await provider.chat_completion(
            messages=[{"role": "user", "content": condense_prompt}],
            model="minimax/minimax-m3:free:floor",
        )
        condensed_clean = (condensed_res or "").strip()
        if condensed_clean.startswith("```markdown"):
            condensed_clean = condensed_clean[len("```markdown"):].strip()
        elif condensed_clean.startswith("```"):
            condensed_clean = condensed_clean[3:].strip()
        if condensed_clean.endswith("```"):
            condensed_clean = condensed_clean[:-3].strip()
        final_journalistic = condensed_clean

    if len(final_journalistic) > 3900:
        final_journalistic = enforce_telegram_limit(final_journalistic, max_chars=3900)

    journo_path = ROOT_DIR / f"digest_journalistic_{date_str}.md"
    journo_path.write_text(final_journalistic + "\n", encoding="utf-8")
    logger.info("Journalistic digest written to %s (%d chars)", journo_path.name, len(final_journalistic))

    print("\n" + "═" * 72)
    print(f"📌 ИТОГОВЫЙ ЖУРНАЛИСТСКИЙ ДАЙДЖЕСТ ({date_human})")
    print(f"Файл: {journo_path.name} ({len(final_journalistic)} знаков)")
    print("═" * 72)
    print(final_journalistic)
    print("═" * 72 + "\n")

    await infra.close()


if __name__ == "__main__":
    asyncio.run(main())
