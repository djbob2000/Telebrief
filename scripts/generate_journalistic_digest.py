"""Generate journalistic digest directly from Event-First knowledge."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.runtime import install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("journalistic_digest")

DIGEST_PROMPT_TEMPLATE = """Вы — старший редактор регионального издания, готовящий ежедневный вечерний Telegram-дайджест города {city} за {date}.

ВАША ЦЕЛЬ:
Сформировать живой, связный, информативный и легко сканируемый Telegram-дайджест на русском языке на основе проверенных городских сообщений за последние 24 часа.

СТРУКТУРА ДАЙДЖЕСТА:
Дайджест · {date}

[3-4 ключевые тематические рубрики, например:
⚡️ Коммунальная обстановка и ЖКХ
🚌 Транспорт, связь и сервисы
🎓 Образование и школьная жизнь
📌 Городские события и быт]

Внутри каждой рубрики:
• **Краткий заголовок факта**: плотное информативное раскрытие сути события в 1-2 предложениях с сохранением ключевой конкретики (улицы, номера домов, графики, цены, решения жителей).

ПРАВИЛА И СТИЛЬ:
1. Журналистский стиль: чистый, энергичный русский язык. Если исходное сообщение на украинском языке — точно и грамотно переведите его на русский.
2. Синтез и объединение: не пишите 10 отдельных пунктов про одно и то же отключение света. Объедините их в один цельный пункт, указав затронутые районы, улицы и характер проблемы.
3. Микродетали: сохраняйте конкретные улицы (Шмидта, Баха, Первомайская, Карла Маркса), номера маршрутов, цены (скидка до 17 руб.), технические подробности (генераторы, оптоволокно, врезки в подвалах).
4. Очистка от мусора: полностью исключайте флуд в чатах («отстегнись», «пока света нет»), пустые ссылки («посилання на ютуб»), спам телефонов и частные объявления.
5. Никаких шаблонных повторов («По сообщениям жителей... По сообщениям жителей...»). Вводные конструкции используйте естественно и разнообразно («По словам горожан...», «В местных чатах отмечают...», «Как рассказали жители...»).
6. СТРОГОЕ ОГРАНИЧЕНИЕ ДЛИНЫ (ОДНО СООБЩЕНИЕ TELEGRAM):
   - Дайджест ДОЛЖЕН целиком помещаться в ОДНО сообщение Telegram (жесткий лимит Telegram — 4096 символов).
   - Общий объём текста должен быть строго в диапазоне 2500–3500 знаков.
   - Сформируйте ровно 3–4 рубрики, в каждой — строго по 2–3 самых важных пункта. Никаких бесконечных списков!

МАТЕРИАЛЫ ДНЯ ДЛЯ ДАЙДЖЕСТА:
{content}
"""


async def main():
    config = load_config("config.yaml")
    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    uow = infra.uow
    run_id = 85  # Latest digest run

    logger.info("Loading editorial inputs for run %d...", run_id)
    adapter = EventEditorialAdapter(uow=uow)
    async with uow.transaction() as conn:
        frozen = await adapter.adapt_inputs_on(conn, run_id)

    cards = frozen.analysis.cards
    logger.info("Loaded %d candidate cards", len(cards))

    # Prepare readable cards summary for prompt
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

    content_for_llm = "\n".join(cards_text_blocks[:45])  # Top substantive stories

    city = "Бердянск"
    date_str = "03.09.2026"
    prompt = DIGEST_PROMPT_TEMPLATE.format(
        city=city,
        date=date_str,
        content=content_for_llm,
    )

    logger.info("Generating journalistic digest via OpenRouter/MiniMax...")
    t0 = time.time()

    gen_service = PublicationGenerationService(uow=uow, config=config, repo=PublicationRepository())
    provider = getattr(gen_service.generator, "provider", None)
    chat_kwargs = {
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "model": "minimax/minimax-m3:free:floor",
    }
    raw_response = await provider.chat_completion(**chat_kwargs)
    elapsed = time.time() - t0

    logger.info("Generation completed in %.2f seconds! Output length: %d chars", elapsed, len(raw_response or ""))

    clean_text = raw_response.strip()
    if clean_text.startswith("```markdown"):
        clean_text = clean_text[len("```markdown"):].strip()
    elif clean_text.startswith("```"):
        clean_text = clean_text[3:].strip()
    if clean_text.endswith("```"):
        clean_text = clean_text[:-3].strip()

    def enforce_telegram_single_message_limit(text: str, max_chars: int = 3950) -> str:
        if len(text) <= max_chars:
            return text
        # Truncate lowest-priority lines/paragraphs until within limit
        lines = text.splitlines(keepends=True)
        acc = []
        cur_len = 0
        for line in lines:
            if cur_len + len(line) <= max_chars:
                acc.append(line)
                cur_len += len(line)
            else:
                break
        res = "".join(acc).strip()
        return res

    final_text = enforce_telegram_single_message_limit(clean_text, max_chars=3950)
    out_file = Path("digest_journalistic_2026-09-03.md")
    out_file.write_text(final_text, encoding="utf-8")
    logger.info("Saved journalistic digest to %s (%d chars)", out_file, len(final_text))

    print("\n" + "═" * 70)
    print("📌 ЖУРНАЛИСТСКИЙ ВАРИАНТ ДАЙДЖЕСТА (время генерации: %.2f сек)" % elapsed)
    print("═" * 70)
    print(clean_text)
    print("═" * 70)


if __name__ == "__main__":
    asyncio.run(main())
