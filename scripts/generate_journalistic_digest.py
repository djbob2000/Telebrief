"""Generate journalistic digest directly from Event-First knowledge."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.publication.digest_narrative import (
    DIGEST_CONDENSE_PROMPT_TEMPLATE,
    DIGEST_PROMPT_TEMPLATE,
    enforce_telegram_single_message_limit,
)
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.generation import PublicationGenerationService
from src.publication.repository import PublicationRepository
from src.runtime import install_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("journalistic_digest")


async def condense_journalistic_digest(
    provider: Any,
    draft_text: str,
    *,
    city: str,
    date_str: str,
    model: str = "minimax/minimax-m3:free:floor",
    max_chars: int = 3900,
    target_chars: int = 3500,
) -> str:
    current_len = len(draft_text)
    if current_len <= max_chars:
        logger.info("Draft length (%d chars) is within Telegram limit (%d chars). Skipping condensation.", current_len, max_chars)
        return draft_text

    logger.info(
        "Draft length (%d chars) exceeds Telegram limit (%d chars). Triggering AI Editorial Condenser...",
        current_len,
        max_chars,
    )
    prompt = DIGEST_CONDENSE_PROMPT_TEMPLATE.format(
        city=city,
        date=date_str,
        current_len=current_len,
        max_chars=max_chars,
        target_chars=target_chars,
        draft_text=draft_text,
    )
    t0 = time.time()
    raw_condensed = await provider.chat_completion(
        messages=[{"role": "user", "content": prompt}],
        model=model,
    )
    elapsed = time.time() - t0
    logger.info("AI Editorial Condenser completed in %.2f seconds! Output length: %d chars", elapsed, len(raw_condensed or ""))

    clean = (raw_condensed or "").strip()
    if clean.startswith("```markdown"):
        clean = clean[len("```markdown"):].strip()
    elif clean.startswith("```"):
        clean = clean[3:].strip()
    if clean.endswith("```"):
        clean = clean[:-3].strip()

    # Final deterministic safety net (just in case model condensation still exceeds max_chars)
    if len(clean) > max_chars:
        logger.warning("Condensed text (%d chars) still exceeds limit; applying deterministic safety net.", len(clean))
        clean = enforce_telegram_single_message_limit(clean, max_chars=max_chars)

    return clean


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

    logger.info("Pass 1: Generating full-text journalistic digest draft via OpenRouter/MiniMax...")
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
    elapsed_draft = time.time() - t0

    logger.info("Pass 1 completed in %.2f seconds! Draft length: %d chars", elapsed_draft, len(raw_response or ""))

    clean_draft = raw_response.strip()
    if clean_draft.startswith("```markdown"):
        clean_draft = clean_draft[len("```markdown"):].strip()
    elif clean_draft.startswith("```"):
        clean_draft = clean_draft[3:].strip()
    if clean_draft.endswith("```"):
        clean_draft = clean_draft[:-3].strip()

    # Pass 2 (Conditional): If draft exceeds 3900 chars, condense with AI Editor
    final_text = await condense_journalistic_digest(
        provider=provider,
        draft_text=clean_draft,
        city=city,
        date_str=date_str,
        max_chars=3900,
        target_chars=3500,
    )

    out_file = Path("digest_journalistic_2026-09-03.md")
    out_file.write_text(final_text, encoding="utf-8")
    logger.info("Saved final journalistic digest to %s (%d chars)", out_file, len(final_text))

    print("\n" + "═" * 70)
    print("📌 ИТОГОВЫЙ ЖУРНАЛИСТСКИЙ ДАЙДЖЕСТ (Длина: %d знаков)" % len(final_text))
    print("═" * 70)
    print(final_text)
    print("═" * 70)


if __name__ == "__main__":
    asyncio.run(main())
