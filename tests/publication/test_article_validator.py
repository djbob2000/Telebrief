"""Tests for deterministic ArticleValidator."""

from __future__ import annotations

import datetime as dt

import pytest

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_models import (
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import validate_article_draft

_NOW = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)


def _make_sample_context() -> ArticleEditorialContext:
    sup1 = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="Авария на подстанции: временно обесточена центральная и нагорная часть Бердянска. Бригада РЭС ведет восстановительные работы.",
        source_text="Авария на подстанции: временно обесточена центральная и нагорная часть Бердянска. Бригада РЭС ведет восстановительные работы.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(101,),
        source_item_ids=(1,),
        observed_at=_NOW,
    )
    sup2 = ArticleSupport(
        support_id="story:2:evidence:0:frag:201",
        text="В художественном музее открылась выставка картин местных авторов.",
        source_text="В художественном музее открылась выставка картин местных авторов.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(201,),
        source_item_ids=(2,),
        observed_at=_NOW,
    )
    sup_ctx = ArticleSupport(
        support_id="story:3:evidence:0:frag:301",
        text="Историческая справка о музее.",
        source_text="Историческая справка о музее.",
        support_kind="evidence",
        publication_use="CONTEXT",
        source_refs=("ref-3",),
        fragment_ids=(301,),
        source_item_ids=(3,),
        observed_at=_NOW,
    )
    support_index = (sup1, sup2, sup_ctx)
    return ArticleEditorialContext(
        headline_candidates=("Заголовок",),
        support_index=support_index,
        support_by_id={s.support_id: s for s in support_index},
        recurring_topics=("utilities", "culture"),
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
        title="Заголовок статьи о событиях",
        title_support_ids=("story:1:evidence:0:frag:101",),
        lead="Вводный лид статьи с описанием основных событий дня в городе.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        sections=(
            ArticleSection(
                heading="Первая секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                paragraphs=(
                    ArticleParagraph(
                        text="В центре и на Нагорной части произошло отключение из-за аварии на подстанции.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                    ),
                ),
            ),
            ArticleSection(
                heading="Вторая секция",
                heading_support_ids=("story:2:evidence:0:frag:201",),
                paragraphs=(
                    ArticleParagraph(
                        text="В городском художественном музее открылась выставка картин.",
                        cited_support_ids=("story:2:evidence:0:frag:201",),
                    ),
                ),
            ),
        ),
        word_count=35,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is True
    assert result.issues == ()
    assert result.violations == ()


@pytest.mark.unit
def test_draft_missing_support_fails() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=(),  # Missing title support
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                paragraphs=(
                    ArticleParagraph(
                        text="Параграф без поддержки.",
                        cited_support_ids=(),  # Missing paragraph support
                    ),
                ),
            ),
        ),
        word_count=10,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    codes = [iss.code for iss in result.issues]
    assert "MISSING_SUPPORT:title" in codes
    assert "MISSING_SUPPORT:paragraph" in codes


@pytest.mark.unit
def test_draft_with_unknown_support_id_fails() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                paragraphs=(
                    ArticleParagraph(
                        text="Текст параграфа.",
                        cited_support_ids=("story:999:evidence:0:frag:999",),
                    ),
                ),
            ),
        ),
        word_count=10,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert any(iss.code == "UNKNOWN_SUPPORT_ID" for iss in result.issues)
    assert "story:999:evidence:0:frag:999" in result.unknown_evidence_ids


@pytest.mark.unit
def test_draft_publication_policy_requires_publish_support() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    # Title citing only CONTEXT support must fail
    draft = StructuredArticleDraft(
        title="Заголовок из контекста",
        title_support_ids=("story:3:evidence:0:frag:301",),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                paragraphs=(
                    ArticleParagraph(
                        text="Текст параграфа.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                    ),
                ),
            ),
        ),
        word_count=10,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert any(
        iss.code == "INVALID_SUPPORT_POLICY" and iss.unit_id == "TITLE" for iss in result.issues
    )


@pytest.mark.unit
def test_draft_unsupported_specifics_fail() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    # 1. Invented duration: "в течение полутора часов"
    draft_time = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                paragraphs=(
                    ArticleParagraph(
                        text="Бригады восстановили питание в течение полутора часов.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                    ),
                ),
            ),
        ),
        word_count=10,
    )
    res_time = validate_article_draft(draft_time, ctx, config)
    assert res_time.is_valid is False
    assert any(iss.code == "UNSUPPORTED_CONCRETE_CLAIM" for iss in res_time.issues)

    # 2. Invented mechanism: "по резервной схеме"
    draft_mech = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                paragraphs=(
                    ArticleParagraph(
                        text="Энергоснабжение восстановили по резервной схеме.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                    ),
                ),
            ),
        ),
        word_count=10,
    )
    res_mech = validate_article_draft(draft_mech, ctx, config)
    assert res_mech.is_valid is False
    assert any(iss.code == "UNSUPPORTED_MECHANISM" for iss in res_mech.issues)

    # 3. Invented cause: "из-за гидроудара"
    draft_cause = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                paragraphs=(
                    ArticleParagraph(
                        text="Давление упало из-за гидроудара.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                    ),
                ),
            ),
        ),
        word_count=10,
    )
    res_cause = validate_article_draft(draft_cause, ctx, config)
    assert res_cause.is_valid is False
    assert any(iss.code == "UNSUPPORTED_CAUSAL_RELATION" for iss in res_cause.issues)


@pytest.mark.unit
def test_draft_internal_handle_leak_fails() -> None:
    ctx = _make_sample_context()
    config = PublicationEditorialConfig(article_min_words=5, article_min_sections=1)

    draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:101",),
        lead="Лид статьи.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                paragraphs=(
                    ArticleParagraph(
                        text="Факт с утечкой ручки [story:1:evidence:0:frag:101] в тексте.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                    ),
                ),
            ),
        ),
        word_count=10,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert any(iss.code == "INTERNAL_HANDLE_LEAK" for iss in result.issues)
