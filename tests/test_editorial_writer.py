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

    assert markdown == ("# Короткая заметка\n\nЛид заметки.\n\nЕдинственный абзац статьи.")


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


@pytest.mark.unit
def test_writer_prompt_contains_3_tier_scale_hierarchy_and_corpus_boundary(mock_logger):
    writer = EditorialWriter(MagicMock(), "model", "skill", mock_logger)
    system_prompt, _ = writer.build_prompt(_analysis(), _bundle())

    assert "geographic spread" in system_prompt.lower()
    assert (
        "broad prevalence" in system_prompt.lower()
        or "broad multi-district" in system_prompt.lower()
    )
    assert "majority" in system_prompt.lower()
    assert "corpus absence" in system_prompt.lower()
    assert "доступных" in system_prompt.lower()


@pytest.mark.unit
def test_article_draft_normalizes_leading_hashes_in_headings():
    draft = ArticleDraft.from_dict(
        {
            "headline": "# Заголовок статьи",
            "lead": "Лид статьи.",
            "paragraphs": [],
            "sections": [
                {
                    "heading": "## Глава 1: События",
                    "paragraphs": ["Текст главы."],
                }
            ],
        }
    )
    assert draft.headline == "Заголовок статьи"
    assert draft.sections[0].heading == "Глава 1: События"
    markdown = draft.to_markdown()
    assert markdown.startswith("# Заголовок статьи\n\nЛид статьи.\n\n## Глава 1: События")
    assert "## ##" not in markdown
    assert "# #" not in markdown


@pytest.mark.unit
def test_writer_prompt_and_skill_contain_chat_slang_normalization(mock_logger):
    from src.article_generator import _load_skill_instructions

    skill_content = _load_skill_instructions(".agents/skills/news-style/SKILL.md")
    writer = EditorialWriter(MagicMock(), "model", skill_content, mock_logger)
    system_prompt, _ = writer.build_prompt(_analysis(), _bundle())

    assert "дистант" in system_prompt.lower() or "сленг" in system_prompt.lower()
    assert "дистанционное" in system_prompt.lower() or "дистанционный" in system_prompt.lower()


@pytest.mark.unit
def test_writer_prompt_contains_local_story_context(mock_logger):
    from src.city_context_models import AreaEvidence, ScaleEvidence, StoryContext

    writer = EditorialWriter(MagicMock(), "model", "skill", mock_logger)
    bundle = _bundle()
    scale = ScaleEvidence(
        observed_area_ids=("center", "liski"),
        observed_count=2,
        geographic_spread=True,
    )
    area1 = AreaEvidence(
        area_set="municipal_neighborhood_committees_2021",
        area_id="center",
        source_refs=("S000001",),
    )
    bundle.story_contexts = {
        "SC001": StoryContext(
            card_id="SC001",
            municipal_areas=(area1,),
            colloquial_area_ids=("center",),
            scale=scale,
        )
    }

    system_prompt, user_prompt = writer.build_prompt(_analysis(), bundle)
    assert "[LOCAL STORY CONTEXT SC001]" in user_prompt
    assert "observed_municipal_areas: center (1 refs)" in user_prompt
    assert "geographic_spread=true" in user_prompt
    assert "LOCAL STORY CONTEXT" in system_prompt


@pytest.mark.unit
def test_writer_prompt_enforces_output_language(mock_logger):
    writer = EditorialWriter(MagicMock(), "model", "skill", mock_logger, output_language="Russian")
    system_prompt, _ = writer.build_prompt(_analysis(), _bundle())
    assert (
        "Write the article exclusively in the configured output language: Russian" in system_prompt
    )
    assert (
        "All headlines, leads, section headings, and body paragraphs must be strictly written in Russian"
        in system_prompt
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writer_rejects_wrong_output_language(mock_logger):
    english_draft = json.dumps(
        {
            "headline": "Citywide power outage enters third week",
            "lead": "Berdyansk residents report a prolonged blackout across multiple districts with no restoration date.",
            "sections": [
                {
                    "heading": "Power outage details",
                    "paragraphs": [
                        "Residents in multiple districts reported being without electricity for about two weeks.",
                        "No official restoration schedule was announced by the local authorities.",
                    ],
                }
            ],
        }
    )
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=english_draft)
    writer = EditorialWriter(provider, "model", "skill", mock_logger, output_language="Russian")

    with pytest.raises(ValueError, match="writer output language mismatch"):
        await writer.write(_analysis(), _bundle())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writer_rejects_single_english_paragraph_in_russian_draft(mock_logger):
    mixed_draft = json.dumps(
        {
            "headline": "В Бердянске обсуждают перебои со светом",
            "lead": "Жители нескольких районов сообщают о длительном отключении электроэнергии.",
            "sections": [
                {
                    "heading": "Ситуация со светом",
                    "paragraphs": [
                        "Электроснабжение отсутствует в ряде микрорайонов города.",
                        "Residents in multiple districts reported being without electricity for two weeks.",
                    ],
                }
            ],
        }
    )
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=mixed_draft)
    writer = EditorialWriter(provider, "model", "skill", mock_logger, output_language="Russian")

    with pytest.raises(ValueError, match="writer output language mismatch"):
        await writer.write(_analysis(), _bundle())


@pytest.mark.unit
def test_writer_prompt_includes_revision_feedback_filtering_fix_only(mock_logger):
    from src.editorial_audit import AuditIssue, FactCheckResult

    writer = EditorialWriter(MagicMock(), "model", "skill", mock_logger, output_language="Russian")
    feedback = FactCheckResult(
        status="FIX",
        systemic_problem=True,
        issues=[
            AuditIssue(
                unit_id="TITLE",
                code="unsupported_scale",
                original_excerpt="Бердянск без света",
                reason="Scale unsupported by evidence",
                suggested_direction="Narrow to confirmed areas",
                source_refs=["S000001"],
                severity="fix",
            ),
            AuditIssue(
                unit_id="LEAD",
                code="soft_overstatement",
                original_excerpt="В городе обсуждают",
                reason="Minor wording style",
                suggested_direction="Soften tone",
                source_refs=[],
                severity="warn",
            ),
        ],
    )

    _, user_prompt = writer.build_prompt(_analysis(), _bundle(), revision_feedback=feedback)
    assert "AUDIT REVISION FEEDBACK:" in user_prompt
    assert "- TITLE / unsupported_scale" in user_prompt
    assert "Reason: Scale unsupported by evidence" in user_prompt
    assert "Direction: Narrow to confirmed areas" in user_prompt
    assert "LEAD / soft_overstatement" not in user_prompt
    assert "These are failure modes to correct, not replacement sentences." in user_prompt
    assert "Do not mechanically copy suggested wording." in user_prompt


@pytest.mark.unit
def test_no_runtime_circular_import():
    import importlib
    import sys

    # Reload modules to verify clean import chain without circular dependencies
    sys.modules.pop("src.editorial_writer", None)
    sys.modules.pop("src.editorial_audit", None)
    writer_mod = importlib.import_module("src.editorial_writer")
    audit_mod = importlib.import_module("src.editorial_audit")
    assert hasattr(writer_mod, "EditorialWriter")
    assert hasattr(audit_mod, "LightFactChecker")
