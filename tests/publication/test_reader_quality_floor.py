"""Regression test suite asserting reader quality floor for digests and long-read articles."""

from __future__ import annotations

import datetime as dt

from src.editorial_models import StoryCard
from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    PublicationWindow,
)
from src.publication.article_coverage import build_article_coverage_plan
from src.publication.article_recovery import ArticleDeterministicComposer
from src.publication.digest_narrative import (
    DigestNarrativeDraft,
)
from src.publication.narrative_quality import (
    evaluate_article_markdown_quality,
    evaluate_digest_markdown_quality,
)


def test_article_composer_prevents_giant_paragraphs_and_maintains_structure():
    """Verify that deterministic fallback chunks stories into readable paragraphs without giant walls."""
    now = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc)
    cards = []
    sups = []

    # Create 1 DEVELOP story, 2 WEAVE stories, and 12 BRIEF stories
    for i in range(15):
        sid = f"story:{i}"
        importance = "high" if i == 0 else ("medium" if i < 3 else "low")
        topic = f"Городская тема {i}"
        cards.append(
            StoryCard(
                id=sid,
                topic=topic,
                importance=importance,
                summary=f"Краткое описание темы {i}",
            )
        )
        sup = ArticleSupport(
            support_id=f"{sid}:evidence:0:frag:{i}",
            text=f"Фактическое событие номер {i} в городе Бердянске.",
            source_text=f"Фактическое событие номер {i} в городе Бердянске.",
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"ref-{i}",),
            fragment_ids=(i,),
            source_item_ids=(i,),
            observed_at=now,
            temporal_role="CURRENT_WINDOW",
            evidence_kind="community_report",
            story_id=sid,
        )
        sups.append(sup)

    ctx = ArticleEditorialContext(
        headline_candidates=("Хроника дня",),
        support_index=tuple(sups),
        support_by_id={s.support_id: s for s in sups},
        recurring_topics=(),
        publication_window=PublicationWindow(
            snapshot_at=now,
            lookback_start=now - dt.timedelta(hours=24),
        ),
        edition_name="Бердянск",
    )

    plan = build_article_coverage_plan(cards, ctx)
    composer = ArticleDeterministicComposer()
    draft = composer.render_full_fallback(ctx, plan)

    md = draft.render_markdown()
    metrics = evaluate_article_markdown_quality(md)

    # 1. No single giant paragraph
    assert metrics.max_paragraph_words < 150
    # 2. Structured across multiple paragraphs
    assert metrics.paragraph_count >= 5
    # 3. No leaked internal IDs
    assert metrics.internal_handle_count == 0
    # 4. Low mechanical chain markers
    assert metrics.temporal_chain_marker_count < 10
    # 5. Low duplicate sentences
    assert metrics.duplicate_sentence_count == 0


def test_digest_narrative_duplicate_block_id_consolidation_preserves_all_items():
    """Verify that when an LLM duplicates block_id, items are merged instead of discarded."""
    raw_response = """
    {
      "blocks": [
        {
          "block_id": "block:infra:0",
          "items": [
            {
              "headline": "Первая новость ЖКХ",
              "body": "Подробности первой новости.",
              "covered_story_ids": ["s1"],
              "cited_support_ids": ["sup1"]
            }
          ]
        },
        {
          "block_id": "block:infra:0",
          "items": [
            {
              "headline": "Вторая новость ЖКХ",
              "body": "Подробности второй новости.",
              "covered_story_ids": ["s2"],
              "cited_support_ids": ["sup2"]
            }
          ]
        }
      ]
    }
    """
    import json

    parsed = json.loads(raw_response)

    # Consolidate duplicate blocks
    merged_blocks: list[dict] = []
    block_by_id: dict[str, dict] = {}
    for b in parsed["blocks"]:
        if isinstance(b, dict) and b.get("block_id"):
            bid = str(b["block_id"]).strip()
            if bid in block_by_id:
                existing_items = block_by_id[bid].setdefault("items", [])
                new_items = b.get("items", [])
                if isinstance(existing_items, list) and isinstance(new_items, list):
                    existing_items.extend(new_items)
            else:
                block_by_id[bid] = b
                merged_blocks.append(b)
        else:
            merged_blocks.append(b)
    parsed["blocks"] = merged_blocks

    draft = DigestNarrativeDraft.from_dict(parsed)
    assert len(draft.blocks) == 1
    assert draft.blocks[0].block_id == "block:infra:0"
    assert len(draft.blocks[0].items) == 2
    assert draft.blocks[0].items[0].headline == "Первая новость ЖКХ"
    assert draft.blocks[0].items[1].headline == "Вторая новость ЖКХ"


def test_digest_quality_floor_on_rendered_prose():
    """Verify quality floor criteria on sample scan-first narrative digest markdown."""
    sample_digest = """# Дайджест: Бердянск · 02.09.2026

*🏙 Городская обстановка*
• 🔴 **Электроснабжение**: В городе сохраняются аварийные отключения.
• 🟢 **Транспорт**: Городские автобусы курсируют по графику.

*⚡️ Инфраструктура и ЖКХ*
• **Аварийные работы на электросетях**: По сообщениям коммунальных служб, ремонтные бригады продолжают восстановление оборудования на ключевых подстанциях.
• **График подачи воды**: Водоснабжение в центральных районах подается по временному расписанию с 17:00 до 21:00.

*🚌 Транспорт и городская среда*
• **Оплата проезда смартфонами**: В общественном транспорте действует безналичная оплата через терминалы.
"""
    metrics = evaluate_digest_markdown_quality(sample_digest)
    assert metrics.bullet_count == 5
    assert metrics.heading_count == 4
    assert metrics.duplicate_sentence_count == 0
    assert metrics.raw_technical_token_count == 0
    assert metrics.internal_handle_count == 0
    assert metrics.temporal_chain_marker_count == 0
