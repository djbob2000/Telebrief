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
        paragraphs=[],
        sections=[
            ArticleSection("Свет", ["Одинаковый текст", "Одинаковый текст"]),
        ],
    )

    units = draft.audit_units()

    assert units["H001"].path == ("sections", "0", "heading")
    assert units["P001"].path == ("sections", "0", "paragraphs", "0")
    assert units["P002"].path == ("sections", "0", "paragraphs", "1")
    assert units["P001"].path != units["P002"].path
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


def test_article_draft_preserves_sections_with_string_headings():
    draft = ArticleDraft.from_dict(
        {
            "headline": "Город за сутки",
            "lead": "Главные темы дня.",
            "paragraphs": ["Лидирующие детали."],
            "sections": [{"heading": "Вода", "paragraphs": ["Подачу воды временно отключали."]}],
        }
    )

    assert draft.sections[0].heading == "Вода"
    assert draft.sections[0].paragraphs == ["Подачу воды временно отключали."]
    assert "## Вода" in draft.to_markdown()


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
    assert "8–12 substantive paragraphs" in combined
    assert "900–1500 words" in combined


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writer_uses_configured_longform_output_budget():
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        return_value=json.dumps(
            {
                "headline": "Вода",
                "lead": "Лид.",
                "paragraphs": ["Абзац."],
                "sections": [],
            }
        )
    )
    writer = EditorialWriter(
        provider,
        "model",
        "SKILL: precise local news",
        MagicMock(),
        max_output_tokens=65536,
    )

    await writer.write(_analysis(), _bundle())

    assert provider.chat_completion.call_args.kwargs["max_tokens"] == 65536


def test_writer_prompt_uses_selected_source_excerpts_only():
    writer = EditorialWriter(MagicMock(), "model", "SKILL: precise local news", MagicMock())
    selected = PreparedBundle(
        records={"S000001": _bundle().records["S000001"]},
        prompt_text="[S000001] Жители сообщили о перебоях с водой.",
        total_messages=2,
        candidate_count=1,
    )

    _, user_prompt = writer.build_prompt(_analysis(), selected)

    assert "Жители сообщили о перебоях с водой" in user_prompt
    assert "S000002" not in user_prompt


def test_editorial_writer_prompt_contains_thematic_chapters_and_synthesis_rules():
    import logging

    writer = EditorialWriter(
        provider=None,
        model="test-model",
        skill_instructions="Newsroom style guidelines",
        logger=logging.getLogger("test"),
    )
    analysis = EditorialAnalysis(
        cards=[
            StoryCard(
                id="SC001",
                topic="Свет",
                importance="high",
                summary="Отключения",
                representative_source_refs=["S000001"],
            )
        ]
    )
    bundle = PreparedBundle(records={}, prompt_text="", total_messages=1, candidate_count=1)
    system, _ = writer.build_prompt(analysis, bundle)
    assert "3–5" in system or "3-5" in system
    assert "lead" in system.lower()
    assert "Story Cards are reporting notes" in system


def test_news_style_skill_file_contains_approved_composition_contract():
    from src.article_generator import _load_skill_instructions

    content = _load_skill_instructions(".agents/skills/news-style/SKILL.md")
    assert "3–5" in content or "3-5" in content
    assert "жители" in content.lower() or "resident" in content.lower()
    assert "causality" in content.lower() or "причинн" in content.lower()


def test_to_markdown_renders_only_sections_when_sections_present():
    draft = ArticleDraft(
        headline="Городской заголовок",
        lead="Городской лид.",
        paragraphs=["Дублирующий абзац 1", "Дублирующий абзац 2"],
        sections=[
            ArticleSection("Раздел 1", ["Абзац раздела 1"]),
            ArticleSection("Раздел 2", ["Абзац раздела 2"]),
        ],
    )

    markdown = draft.to_markdown()

    assert markdown == (
        "# Городской заголовок\n\n"
        "Городской лид.\n\n"
        "## Раздел 1\n\n"
        "Абзац раздела 1\n\n"
        "## Раздел 2\n\n"
        "Абзац раздела 2"
    )
    assert "Дублирующий" not in markdown


def test_to_markdown_renders_paragraphs_when_sections_empty():
    draft = ArticleDraft(
        headline="Короткая заметка",
        lead="Лид заметки.",
        paragraphs=["Единственный абзац статьи."],
        sections=[],
    )

    markdown = draft.to_markdown()

    assert markdown == ("# Короткая заметка\n\n" "Лид заметки.\n\n" "Единственный абзац статьи.")


def test_audit_units_does_not_duplicate_paragraphs_when_sections_present():
    draft = ArticleDraft(
        headline="Заголовок",
        lead="Лид",
        paragraphs=["Параграф"],
        sections=[ArticleSection("Глава", ["Параграф главы"])],
    )

    units = draft.audit_units()

    assert set(units.keys()) == {"TITLE", "LEAD", "H001", "P001"}
    assert units["H001"].text == "Глава"
    assert units["P001"].text == "Параграф главы"
    assert units["P001"].path == ("sections", "0", "paragraphs", "0")


def test_article_draft_from_dict_normalizes_empty_paragraphs_when_sections_present():
    payload = {
        "headline": "Заголовок",
        "lead": "Лид",
        "paragraphs": ["Параграф 1", "Параграф 2"],
        "sections": [{"heading": "Глава 1", "paragraphs": ["Параграф 1", "Параграф 2"]}],
    }

    draft = ArticleDraft.from_dict(payload)

    assert draft.paragraphs == []
    assert len(draft.sections) == 1


@pytest.mark.unit
def test_writer_prompt_contains_unofficial_versions_and_scale_discipline(mock_logger):
    writer = EditorialWriter(MagicMock(), "model", "skill", mock_logger)
    system_prompt, _ = writer.build_prompt(_analysis(), _bundle())

    assert "verified baseline" in system_prompt.lower()
    assert "unofficial estimates" in system_prompt.lower()
    assert "evidence of scale" in system_prompt.lower()
