"""Regression test for forced writer failure fallback quality and coverage."""

from __future__ import annotations

import datetime as dt

import pytest

from src.config_loader import PublicationEditorialConfig
from src.editorial_models import StoryCard
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import ArticleCoveragePlan, ArticleStoryCoverage
from src.publication.article_finalization import ArticleFinalizer
from src.publication.article_models import (
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from tests.publication.test_article_recovery import RecordingAttemptObserver

_NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forced_writer_failure_fallback_regression() -> None:
    """Test 10A: When writer fails/produces invalid draft, fallback draft passes validation with 100% coverage."""
    sup1 = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="В Бердянске восстановили подачу электроэнергии.",
        source_text="В Бердянске восстановили подачу электроэнергии.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="established_fact",
        story_id="story:1",
    )
    sup2 = ArticleSupport(
        support_id="story:2:evidence:0:frag:202",
        text="Автобус №4 курсирует с интервалом в 30 минут.",
        source_text="Автобус №4 курсирует с интервалом в 30 минут.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="community_report",
        story_id="story:2",
    )
    sup3 = ArticleSupport(
        support_id="story:3:evidence:0:frag:303",
        text="В спорткомплексе открылся набор в секцию плавания.",
        source_text="В спорткомплексе открылся набор в секцию плавания.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:3",),
        fragment_ids=(3,),
        source_item_ids=(3,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="established_fact",
        story_id="story:3",
    )
    supports = (sup1, sup2, sup3)
    cards = (
        StoryCard(
            id="story:1",
            topic="Электроснабжение",
            importance="high",
            category="utilities",
            summary="Электроснабжение",
        ),
        StoryCard(
            id="story:2",
            topic="Транспорт",
            importance="medium",
            category="transport",
            summary="Транспорт",
        ),
        StoryCard(
            id="story:3",
            topic="Спорт",
            importance="low",
            category="social",
            summary="Спорт",
        ),
    )
    context = ArticleEditorialContext(
        headline_candidates=("Электроснабжение", "Городской транспорт", "Спорт"),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
        edition_anchor_terms=("Бердянск",),
        story_cards=cards,
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup1.support_id,),
                detail_support_ids=(sup1.support_id,),
            ),
            ArticleStoryCoverage(
                story_id="story:2",
                topic="Городской транспорт",
                rank=2,
                prominence="WEAVE",
                support_ids=(sup2.support_id,),
                detail_support_ids=(sup2.support_id,),
            ),
            ArticleStoryCoverage(
                story_id="story:3",
                topic="Спорт",
                rank=3,
                prominence="BRIEF",
                support_ids=(sup3.support_id,),
                detail_support_ids=(sup3.support_id,),
            ),
        )
    )

    # Invalid draft with unsupported proper names and leaked brackets
    invalid_draft = StructuredArticleDraft(
        title="Заголовок из ниоткуда",
        title_support_ids=("story:1:evidence:0:frag:101",),
        lead="Лид с выдуманным городом Париж.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                paragraphs=(
                    ArticleParagraph(
                        text="[power] AVAILABLE везде.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                    ),
                ),
            ),
        ),
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_min_sections=1, article_min_words=5, article_max_sections=6
    )

    result = await finalizer.finalize(
        writer_draft=invalid_draft,
        writer_error=None,
        writer_attempt_id=writer_id,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
        length_profile=None,
        attempt_observer=observer,
    )

    assert result.writer_status == "rejected"
    assert result.recovery_mode == "full_fallback"
    from src.publication.article_validator import validate_article_draft

    val_res = validate_article_draft(result.draft, context, editorial_config)
    assert val_res.is_valid is True, f"Validation issues: {val_res.issues}"

    # Assert section count <= article_max_sections
    assert len(result.draft.sections) <= editorial_config.article_max_sections

    # Assert 0 leaks of `[...] AVAILABLE` or raw enums
    markdown = result.draft.render_markdown()
    assert "AVAILABLE" not in markdown
    assert "UNAVAILABLE" not in markdown
    assert "[" not in markdown and "]" not in markdown
