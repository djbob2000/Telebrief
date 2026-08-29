"""Tests for StructuredArticleDraft and clean markdown rendering."""

from __future__ import annotations

import pytest

from src.publication.article_models import (
    StructuredArticleDraft,
    _strip_internal_handles,
)


@pytest.mark.unit
def test_strip_internal_handles() -> None:
    text = "Ремонт на водоводе [story:1:evidence:0:frag:101] продолжается в штатном режиме."
    cleaned = _strip_internal_handles(text)
    assert cleaned == "Ремонт на водоводе продолжается в штатном режиме."


@pytest.mark.unit
def test_structured_article_draft_from_dict_and_render_markdown() -> None:
    data = {
        "title": "Хроника дня: коммунальные работы и культура [story:1]",
        "lead": "В Бердянске завершились утренние ремонты на сетях [story:1:evidence:0:frag:101].",
        "sections": [
            {
                "heading": "Водоснабжение и коммунальные службы",
                "paragraphs": [
                    "Специалисты водоканала завершили сварку трубы [story:1:evidence:0:frag:101]. Вода подана во все дома.",
                    "Давление в сети стабилизировалось к полудню.",
                ],
                "cited_evidence_ids": ["story:1:evidence:0:frag:101"],
            },
            {
                "heading": "Культурные события",
                "paragraphs": [
                    "В городском музее открылась выставка картин [story:2:evidence:0:frag:201].",
                ],
                "cited_evidence_ids": ["story:2:evidence:0:frag:201"],
            },
        ],
        "cited_evidence_ids": ["story:1:evidence:0:frag:101"],
    }

    draft = StructuredArticleDraft.from_dict(data)
    assert draft.title == "Хроника дня: коммунальные работы и культура"
    assert draft.lead == "В Бердянске завершились утренние ремонты на сетях."
    assert len(draft.sections) == 2
    assert draft.sections[0].cited_evidence_ids == ("story:1:evidence:0:frag:101",)
    assert "story:2:evidence:0:frag:201" in draft.cited_evidence_ids
    assert draft.word_count > 0

    rendered = draft.render_markdown()
    assert "В Бердянске завершились утренние ремонты на сетях." in rendered
    assert "## Водоснабжение и коммунальные службы" in rendered
    assert "## Культурные события" in rendered
    assert "Специалисты водоканала завершили сварку трубы. Вода подана во все дома." in rendered
    # Crucial invariant: internal IDs must NEVER appear in rendered markdown!
    assert "story:" not in rendered
    assert "frag:" not in rendered
    assert "evidence:" not in rendered
