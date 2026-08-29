"""Tests for deterministic ArticleValidator."""

from __future__ import annotations

import datetime as dt

import pytest

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_models import ArticleSection, StructuredArticleDraft
from src.publication.article_validator import validate_article_draft
from src.publication.evidence import PublicationEvidence

_NOW = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)


def _make_sample_context() -> ArticleEditorialContext:
    evi1 = PublicationEvidence(
        evidence_id="story:1:evidence:0:frag:101",
        story_id=1,
        text="Факт 1",
        source_text="Факт 1",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="ref-1",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=_NOW,
    )
    evi2 = PublicationEvidence(
        evidence_id="story:2:evidence:0:frag:201",
        story_id=2,
        text="Факт 2",
        source_text="Факт 2",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=201,
        source_ref="ref-2",
        source_id=2,
        source_item_id=2,
        source_role="community",
        observed_at=_NOW,
    )
    return ArticleEditorialContext(
        headline_candidates=("Заголовок",),
        operational_timeline=(),
        evidence_index=(evi1, evi2),
        evidence_by_id={evi1.evidence_id: evi1, evi2.evidence_id: evi2},
        recurring_topics=("utilities",),
        general_facts=(evi1, evi2),
        resident_observations=(),
    )


@pytest.mark.unit
def test_valid_draft_passes_validation() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(
        article_min_words=10,
        article_max_words=200,
        article_min_sections=2,
        article_max_sections=4,
    )

    draft = StructuredArticleDraft(
        title="Заголовок статьи",
        lead="Вводный лид статьи с описанием основных событий дня.",
        sections=(
            ArticleSection(
                heading="Первая секция",
                paragraphs=("Параграф текста с описанием фактов и деталей.",),
                cited_evidence_ids=("story:1:evidence:0:frag:101",),
            ),
            ArticleSection(
                heading="Вторая секция",
                paragraphs=("Второй параграф текста с подробностями.",),
                cited_evidence_ids=("story:2:evidence:0:frag:201",),
            ),
        ),
        cited_evidence_ids=("story:1:evidence:0:frag:101", "story:2:evidence:0:frag:201"),
        word_count=30,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is True
    assert result.violations == ()
    assert result.unknown_evidence_ids == ()


@pytest.mark.unit
def test_draft_with_unknown_evidence_id_fails() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    draft = StructuredArticleDraft(
        title="Заголовок",
        lead="Лид статьи.",
        sections=(
            ArticleSection(
                heading="Секция",
                paragraphs=("Текст параграфа.",),
                cited_evidence_ids=("story:999:evidence:0:frag:999",),
            ),
        ),
        cited_evidence_ids=("story:999:evidence:0:frag:999",),
        word_count=10,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert "story:999:evidence:0:frag:999" in result.unknown_evidence_ids


@pytest.mark.unit
def test_draft_with_too_few_sections_fails() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_sections=3)

    draft = StructuredArticleDraft(
        title="Заголовок",
        lead="Лид статьи.",
        sections=(ArticleSection(heading="Секция 1", paragraphs=("Текст 1.",)),),
        cited_evidence_ids=(),
        word_count=50,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert any("sections" in v for v in result.violations)


@pytest.mark.unit
def test_draft_with_too_few_words_fails() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=800, article_min_sections=1)

    draft = StructuredArticleDraft(
        title="Заголовок",
        lead="Лид статьи.",
        sections=(ArticleSection(heading="Секция 1", paragraphs=("Текст 1.",)),),
        cited_evidence_ids=(),
        word_count=50,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert any("words" in v for v in result.violations)
