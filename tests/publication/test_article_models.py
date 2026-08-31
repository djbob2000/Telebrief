"""Tests for StructuredArticleDraft and clean markdown rendering."""

from __future__ import annotations

import pytest

from src.publication.article_models import (
    ArticleParagraph,
    StructuredArticleDraft,
    _strip_internal_handles,
)


@pytest.mark.unit
def test_strip_internal_handles() -> None:
    text = "Ремонт на водоводе [story:1:evidence:0:frag:101] продолжается в штатном режиме."
    cleaned = _strip_internal_handles(text)
    assert cleaned == "Ремонт на водоводе продолжается в штатном режиме."


@pytest.mark.unit
def test_structured_article_draft_unit_level_support_ids() -> None:
    payload = {
        "title": "Бердянск: аварии на сетях и городские события [story:1]",
        "title_support_ids": ["e1"],
        "lead": "В городе сохранялись перебои с водой.",
        "lead_support_ids": ["e1"],
        "sections": [
            {
                "heading": "Водоснабжение",
                "heading_support_ids": ["e1"],
                "paragraphs": [
                    {
                        "text": "К вечеру в Колонии снова не было воды [story:1:evidence:0:frag:101].",
                        "cited_support_ids": ["e1"],
                    }
                ],
            }
        ],
    }

    draft = StructuredArticleDraft.from_dict(payload)
    assert draft.title == "Бердянск: аварии на сетях и городские события"
    assert draft.title_support_ids == ("e1",)
    assert draft.lead == "В городе сохранялись перебои с водой."
    assert draft.lead_support_ids == ("e1",)
    assert len(draft.sections) == 1
    sec = draft.sections[0]
    assert sec.heading == "Водоснабжение"
    assert sec.heading_support_ids == ("e1",)
    assert len(sec.paragraphs) == 1
    assert isinstance(sec.paragraphs[0], ArticleParagraph)
    assert sec.paragraphs[0].text == "К вечеру в Колонии снова не было воды."
    assert sec.paragraphs[0].cited_support_ids == ("e1",)
    assert draft.cited_support_ids == ("e1",)

    rendered = draft.render_markdown()
    assert "В городе сохранялись перебои с водой." in rendered
    assert "## Водоснабжение" in rendered
    assert "К вечеру в Колонии снова не было воды." in rendered
    assert "story:" not in rendered
    assert "e1" not in rendered


@pytest.mark.unit
def test_structured_article_draft_with_claim_atoms() -> None:
    from src.publication.article_models import ArticleClaimAtom

    raw = {
        "title": "Бердянск остается без стабильного электроснабжения",
        "title_support_ids": ["SUP-POWER"],
        "title_claims": [
            {
                "text": "В Бердянске сохраняются перебои с электроснабжением",
                "cited_support_ids": ["SUP-POWER"],
            }
        ],
        "lead": "За последние сутки жители продолжали сообщать о перебоях со светом.",
        "lead_support_ids": ["SUP-POWER"],
        "lead_claims": [
            {
                "text": "За последние сутки жители сообщали о перебоях со светом",
                "cited_support_ids": ["SUP-POWER"],
            }
        ],
        "sections": [
            {
                "heading": "Электроснабжение",
                "heading_support_ids": ["SUP-POWER"],
                "heading_claims": [
                    {
                        "text": "Раздел описывает состояние электроснабжения",
                        "cited_support_ids": ["SUP-POWER"],
                    }
                ],
                "paragraphs": [
                    {
                        "text": "В центре города жители сообщают об отсутствии света.",
                        "cited_support_ids": ["SUP-POWER"],
                        "claims": [
                            {
                                "text": "В центре города жители сообщают об отсутствии света",
                                "cited_support_ids": ["SUP-POWER"],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    draft = StructuredArticleDraft.from_dict(raw)
    assert len(draft.title_claims) == 1
    assert isinstance(draft.title_claims[0], ArticleClaimAtom)
    assert draft.title_claims[0].text == "В Бердянске сохраняются перебои с электроснабжением"
    assert draft.title_claims[0].cited_support_ids == ("SUP-POWER",)

    assert len(draft.lead_claims) == 1
    assert draft.lead_claims[0].text == "За последние сутки жители сообщали о перебоях со светом"

    sec = draft.sections[0]
    assert len(sec.heading_claims) == 1
    assert sec.heading_claims[0].text == "Раздел описывает состояние электроснабжения"

    p = sec.paragraphs[0]
    assert len(p.claims) == 1
    assert p.claims[0].text == "В центре города жители сообщают об отсутствии света"
    assert p.claims[0].cited_support_ids == ("SUP-POWER",)

    # Internal support IDs must remain absent from rendered Markdown
    rendered = draft.render_markdown()
    assert "SUP-POWER" not in rendered


@pytest.mark.unit
def test_structured_article_draft_legacy_string_paragraphs_fail_closed_empty_support() -> None:
    payload = {
        "title": "Заголовок",
        "lead": "Лид",
        "sections": [
            {
                "heading": "Раздел",
                "paragraphs": ["К вечеру воды не было."],
                "cited_evidence_ids": ["e1"],
            }
        ],
    }

    draft = StructuredArticleDraft.from_dict(payload)
    assert len(draft.sections) == 1
    sec = draft.sections[0]
    assert len(sec.paragraphs) == 1
    p = sec.paragraphs[0]
    assert isinstance(p, ArticleParagraph)
    assert p.text == "К вечеру воды не было."
    # Legacy string paragraph must NOT inherit section cited_evidence_ids
    assert p.cited_support_ids == ()


@pytest.mark.unit
def test_structured_article_from_dict_forces_ai_origin() -> None:
    draft = StructuredArticleDraft.from_dict(
        {
            "title": "Заголовок",
            "title_support_ids": ["story:1:evidence:0:frag:1"],
            "title_claims": [
                {
                    "text": "Заголовок",
                    "cited_support_ids": ["story:1:evidence:0:frag:1"],
                }
            ],
            "lead": "Лид",
            "lead_support_ids": ["story:1:evidence:0:frag:1"],
            "lead_claims": [
                {
                    "text": "Лид",
                    "cited_support_ids": ["story:1:evidence:0:frag:1"],
                }
            ],
            "title_generation_origin": "FALLBACK",
            "sections": [
                {
                    "heading": "Раздел",
                    "heading_support_ids": ["story:1:evidence:0:frag:1"],
                    "heading_generation_origin": "FALLBACK",
                    "paragraphs": [
                        {
                            "text": "Абзац",
                            "cited_support_ids": ["story:1:evidence:0:frag:1"],
                            "generation_origin": "FALLBACK",
                            "claims": [
                                {
                                    "text": "Абзац",
                                    "cited_support_ids": ["story:1:evidence:0:frag:1"],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    assert draft.title_generation_origin == "AI"
    assert draft.lead_generation_origin == "AI"
    assert draft.sections[0].heading_generation_origin == "AI"
    assert draft.sections[0].paragraphs[0].generation_origin == "AI"
