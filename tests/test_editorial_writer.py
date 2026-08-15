"""Tests for the free-form Story Card writer and structural audit locators."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collector import Message
from src.editorial_models import EditorialAnalysis, PreparedBundle, SourceRecord, StoryCard
from src.editorial_writer import ArticleDraft, ArticleSection, EditorialWriter


def _analysis() -> EditorialAnalysis:
    return EditorialAnalysis(
        cards=[
            StoryCard(
                id="SC001",
                topic="Вода",
                importance="high",
                summary="В нескольких районах обсуждали перебои с водой.",
            )
        ]
    )


def _bundle() -> PreparedBundle:
    message = Message(
        text="Жители сообщили о перебоях с водой.",
        sender="Житель",
        timestamp=datetime(2026, 8, 14, tzinfo=timezone.utc),
        link="https://t.me/source/1",
        channel_name="Source",
        has_media=False,
        media_type="",
        message_id=1,
    )
    return PreparedBundle(
        records={"S000001": SourceRecord("S000001", message, "community")},
        prompt_text="[S000001] source_type=community\ntext: Жители сообщили о перебоях с водой.",
        total_messages=1,
        candidate_count=1,
    )


def test_article_draft_supports_thin_story_without_h2_sections():
    draft = ArticleDraft(
        headline="Жители сообщили о перебоях с водой",
        lead="В нескольких районах жители рассказали о перебоях с водой.",
        paragraphs=["Коммунальное предприятие пока не сообщило подробности."],
        sections=[],
    )

    markdown = draft.to_markdown()
    units = draft.audit_units()

    assert markdown.startswith("# Жители сообщили")
    assert "\n\nКоммунальное предприятие" in markdown
    assert set(units) == {"TITLE", "LEAD", "P001"}
    assert units["P001"].path == ("paragraphs", "0")


def test_article_draft_assigns_structural_units_to_duplicate_text_and_h2():
    draft = ArticleDraft(
        headline="Заголовок",
        lead="Лид",
        paragraphs=["Одинаковый текст", "Одинаковый текст"],
        sections=[ArticleSection("Свет", ["Одинаковый текст"])],
    )

    units = draft.audit_units()

    assert units["P001"].path != units["P002"].path
    assert units["H001"].path == ("sections", "0", "heading")
    assert units["P003"].path == ("sections", "0", "paragraphs", "0")
    assert len({locator.path for locator in units.values()}) == len(units)


def test_article_draft_apply_replacements_uses_unit_paths_not_text_search():
    draft = ArticleDraft(
        headline="Заголовок",
        lead="Лид",
        paragraphs=["Одинаковый текст", "Одинаковый текст"],
        sections=[],
    )

    repaired = draft.apply_replacements({"P002": "Исправленный второй абзац"})

    assert repaired.paragraphs == ["Одинаковый текст", "Исправленный второй абзац"]


def test_article_draft_can_remove_unresolved_body_unit():
    draft = ArticleDraft(
        headline="Заголовок",
        lead="Лид",
        paragraphs=["Неподдержанный фрагмент"],
        sections=[],
    )

    repaired = draft.apply_replacements({"P001": ""})

    assert repaired.to_markdown() == "# Заголовок\n\nЛид"


def test_article_draft_rejects_malformed_writer_shape():
    with pytest.raises(ValueError, match="headline"):
        ArticleDraft.from_dict({"lead": "Лид", "paragraphs": [], "sections": []})


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writer_prompt_allows_synthesis_without_claim_ids():
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        return_value=json.dumps(
            {
                "headline": "Жители сообщили о перебоях с водой",
                "lead": "В нескольких районах жители рассказали о перебоях с водой.",
                "paragraphs": ["Подробности уточняются."],
                "sections": [],
            }
        )
    )
    writer = EditorialWriter(provider, "model", "SKILL: precise local news", MagicMock())

    draft = await writer.write(_analysis(), _bundle())

    assert draft.headline.startswith("Жители")
    prompt = provider.chat_completion.call_args.kwargs["messages"]
    combined = "\n".join(item["content"] for item in prompt)
    assert "reporting notes" in combined.lower()
    assert "new independently verifiable fact" in combined.lower()
    assert "claim id" not in combined.lower()
    assert "S000001" in combined
