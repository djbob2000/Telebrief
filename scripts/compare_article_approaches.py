import asyncio
import json
import logging
import os
import psycopg

from src.config_loader import load_config
from src.editorial_models import StoryCard, StoryElement, EditorialAnalysis, PreparedBundle
from src.publication.editorial_adapter import FrozenEditorialInput
from src.publication.evidence import PublicationEvidence
from src.publication.article_context import build_article_editorial_context
from src.article_generator import ArticleGenerator

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("compare_articles")

async def main():
    config = load_config()
    db_url = os.environ.get("DATABASE_URL", "postgresql://air@localhost:5432/telebrief")

    # 1. Fetch real stories from DB
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        cur = await conn.execute("""
            SELECT DISTINCT ON (r.title) s.id, r.title, r.summary, r.event_payload, s.created_at
            FROM stories s
            JOIN story_revisions r ON r.story_id = s.id
            WHERE r.event_payload IS NOT NULL AND length(r.title) > 0
            ORDER BY r.title, s.id DESC
            LIMIT 5
        """)
        rows = await cur.fetchall()

    cards = []
    evidence_items = []

    for idx, r in enumerate(rows):
        sid, title, summary, payload, created_at = r[0], r[1], r[2], r[3], r[4]
        sup_id = f"story:{sid}:evidence:0:frag:10{idx}"

        facts = []
        if isinstance(payload, dict):
            for hf in payload.get("hard_facts", []):
                facts.append(StoryElement(text=hf, source_refs=[sup_id]))
        if not facts:
            facts.append(StoryElement(text=f"{title}. {summary}", source_refs=[sup_id]))

        cards.append(StoryCard(
            id=f"story:{sid}",
            topic=title,
            importance="high",
            summary=summary,
            rubric_id="utilities" if "свет" in title.lower() or "вод" in title.lower() else "society",
            hard_facts=facts,
        ))

        evi = PublicationEvidence(
            evidence_id=sup_id,
            story_id=sid,
            text=f"{title}. {summary}",
            source_text=f"{title}. {summary}",
            kind="established_fact",
            publication_use="PUBLISH",
            fragment_id=100 + idx,
            source_ref=f"ref-{sid}",
            source_id=sid,
            source_item_id=sid,
            source_role="official" if idx % 2 == 0 else "community",
            observed_at=created_at,
        )
        evidence_items.append(evi)

    # -------------------------------------------------------------
    # 1. NEW APPROACH: Event-First Evidence-Bound Narrative Generator
    # -------------------------------------------------------------
    art_ctx = build_article_editorial_context(
        cards=cards,
        evidence_items=evidence_items,
        operational_observations=[],
    )
    analysis = EditorialAnalysis(
        cards=cards,
        article_context=art_ctx,
    )
    bundle = PreparedBundle(records={}, prompt_text="", total_messages=len(cards), candidate_count=len(cards))
    frozen_input = FrozenEditorialInput(
        analysis=analysis,
        writer_bundle=bundle,
        run_id=1001,
    )

    gen = ArticleGenerator(config=config, logger=logger)
    logger.info("Generating article with NEW Event-First Narrative approach (Single Call)...")
    new_title, new_lead, new_body = await gen.generate_from_frozen_input(frozen_input)

    # -------------------------------------------------------------
    # 2. OLD APPROACH: Custom Free-Form Prompt (Simulated with same model)
    # -------------------------------------------------------------
    logger.info("Generating article with OLD Custom Free-Form approach...")
    cards_text = "\n\n".join([f"Story Card {c.id}:\n- Topic: {c.topic}\n- Summary: {c.summary}\n- Facts: " + "; ".join(f.text for f in c.hard_facts) for c in cards])

    old_system_prompt = """You are the article writer for a regional news outlet in Berdyansk.
Write a free-form editorial article in Russian based on the Story Cards below.
Combine and connect the material naturally into 2-3 thematic chapters.
Return JSON matching:
{
  "headline": "...",
  "lead": "...",
  "sections": [
    {
      "heading": "...",
      "paragraphs": ["..."]
    }
  ]
}"""
    old_user_prompt = f"Here are the Story Cards:\n\n{cards_text}"

    raw_old = await gen.provider.chat_completion(
        messages=[
            {"role": "system", "content": old_system_prompt},
            {"role": "user", "content": old_user_prompt},
        ],
        model=config.settings.ai_model,
        response_format={"type": "json_object"},
    )

    cleaned_old = raw_old.strip()
    if cleaned_old.startswith("```"):
        lines = cleaned_old.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned_old = "\n".join(lines).strip()

    old_json = json.loads(cleaned_old)
    old_title = old_json.get("headline") or old_json.get("title")
    old_lead = old_json.get("lead")
    old_body_parts = []
    for s in old_json.get("sections", []):
        old_body_parts.append(f"## {s.get('heading', '')}\n")
        for p in s.get('paragraphs', []):
            old_body_parts.append(p + "\n")
    old_body = "\n".join(old_body_parts)

    print("\n" + "="*80)
    print("1. СТАРЫЙ ПОДХОД (Custom / Free-form Multi-pass Prompt)")
    print("="*80)
    print(f"# {old_title}\n\n{old_lead}\n\n{old_body}")

    print("\n" + "="*80)
    print("2. НОВЫЙ ПОДХОД (Event-First Narrative Evidence-Bound Single-Call)")
    print("="*80)
    print(f"# {new_title}\n\n{new_lead}\n\n{new_body}")

if __name__ == "__main__":
    asyncio.run(main())
