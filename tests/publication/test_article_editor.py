"""Unit tests for ArticleEditor targeted copy-editing and fact-checking."""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock

import pytest

from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
)
from src.publication.article_editor import ArticleEditor
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import (
    validate_article_draft,
)

_NOW = dt.datetime(2026, 9, 5, 10, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def sample_context() -> ArticleEditorialContext:
    sup1 = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="По информации жителей города, на Восточном проспекте продолжаются работы по замене водовода.",
        source_text="По информации жителей города, на Восточном проспекте продолжаются работы по замене водовода.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("test_ch",),
        fragment_ids=(101,),
        source_item_ids=(101,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        story_id="story:1",
    )
    sup2 = ArticleSupport(
        support_id="story:2:evidence:0:frag:102",
        text="Движение автобусов по маршруту №4 осуществляется с интервалом около одного часа.",
        source_text="Движение автобусов по маршруту №4 осуществляется с интервалом около одного часа.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("test_ch",),
        fragment_ids=(102,),
        source_item_ids=(102,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        story_id="story:2",
    )
    supports = (sup1, sup2)
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
        edition_name="Бердянск",
        edition_anchor_terms=("бердянск", "бердянске", "бердянска"),
        story_cards=(),
    )


@pytest.fixture
def sample_draft() -> StructuredArticleDraft:
    sup1_id = "story:1:evidence:0:frag:101"
    sup2_id = "story:2:evidence:0:frag:102"
    return StructuredArticleDraft(
        title="Городские работы и движение транспорта в Бердянске",
        title_support_ids=(sup1_id,),
        title_claims=(
            ArticleClaimAtom(text="Городские работы в Бердянске", cited_support_ids=(sup1_id,)),
        ),
        lead="В городе продолжаются инфраструктурные работы: «Мы всё починим к вечеру».",
        lead_support_ids=(sup1_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В городе продолжаются инфраструктурные работы", cited_support_ids=(sup1_id,)
            ),
        ),
        sections=(
            ArticleSection(
                heading="Водоснабжение на Восточном",
                heading_support_ids=(sup1_id,),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Водоснабжение на Восточном", cited_support_ids=(sup1_id,)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="По решению Минобразования на Восточном проспекте рабочие меняют трубы.",
                        cited_support_ids=(sup1_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="На Восточном проспекте меняют трубы",
                                cited_support_ids=(sup1_id,),
                            ),
                        ),
                    ),
                ),
            ),
            ArticleSection(
                heading="Городской транспорт",
                heading_support_ids=(sup2_id,),
                heading_claims=(
                    ArticleClaimAtom(text="Городской транспорт", cited_support_ids=(sup2_id,)),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Автобус №4 ходит примерно раз в час.",
                        cited_support_ids=(sup2_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Автобус №4 ходит раз в час", cited_support_ids=(sup2_id,)
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


@pytest.mark.unit
def test_apply_patches_preserves_structure_and_provenance(
    sample_draft: StructuredArticleDraft,
) -> None:
    editor = ArticleEditor(provider=AsyncMock(), model="test-model")
    patches = {
        "LEAD": "В городе продолжаются инфраструктурные работы, специалисты восстанавливают подачу.",
        "P001": "На Восточном проспекте коммунальные службы продолжают замену водовода.",
    }

    patched = editor.apply_patches(sample_draft, patches)

    assert patched.title == sample_draft.title
    assert (
        patched.lead
        == "В городе продолжаются инфраструктурные работы, специалисты восстанавливают подачу."
    )
    assert (
        patched.sections[0].paragraphs[0].text
        == "На Восточном проспекте коммунальные службы продолжают замену водовода."
    )
    # Provenance preserved:
    assert (
        patched.sections[0].paragraphs[0].cited_support_ids
        == sample_draft.sections[0].paragraphs[0].cited_support_ids
    )
    # Unpatched paragraph untouched:
    assert patched.sections[1].paragraphs[0].text == sample_draft.sections[1].paragraphs[0].text


@pytest.mark.unit
def test_parse_editor_response_handles_various_formats() -> None:
    editor = ArticleEditor(provider=AsyncMock(), model="test-model")

    # 1. Plain json object with "units"
    res1 = json.dumps({"units": {"LEAD": "Текст лида", "P002": "Текст второго абзаца"}})
    assert editor._parse_editor_response(res1) == {
        "LEAD": "Текст лида",
        "P002": "Текст второго абзаца",
    }

    # 2. Markdown wrapped
    res2 = f"```json\n{res1}\n```"
    assert editor._parse_editor_response(res2) == {
        "LEAD": "Текст лида",
        "P002": "Текст второго абзаца",
    }

    # 3. Direct mapping without "units" key
    res3 = json.dumps({"P001": "Текст абзаца 1"})
    assert editor._parse_editor_response(res3) == {"P001": "Текст абзаца 1"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_editor_resolves_validation_issues(
    sample_draft: StructuredArticleDraft, sample_context: ArticleEditorialContext
) -> None:
    mock_provider = AsyncMock()
    # Mock LLM editor response that cleans quotes and genericizes names
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "units": {
                "LEAD": "В городе продолжаются инфраструктурные работы, ремонтные бригады занимаются восстановлением сетей.",
                "P001": "По информации жителей, на Восточном проспекте рабочие продолжают замену водовода.",
            }
        }
    )

    editor = ArticleEditor(provider=mock_provider, model="test-model")

    from src.config_loader import PublicationEditorialConfig

    test_config = PublicationEditorialConfig(article_min_sections=1, article_min_words=5)

    # Initial draft has issues in LEAD and P001
    val_res = validate_article_draft(sample_draft, sample_context, config=test_config)

    edited_draft, edited_val = await editor.edit_draft(
        sample_draft,
        val_res,
        sample_context,
        config=test_config,
        max_attempts=1,
    )

    assert mock_provider.chat_completion.call_count == 1
    assert "«" not in edited_draft.lead
    assert "Минобразования" not in edited_draft.sections[0].paragraphs[0].text
    assert edited_val.is_valid
