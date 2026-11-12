# ruff: noqa: S101
"""article-fidelity W6.1: a paragraph that restates the previous paragraph is a repair finding."""

from __future__ import annotations

import datetime as dt

from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    PublicationWindow,
)
from src.publication.article_models import ArticleParagraph, ArticleSection, StructuredArticleDraft
from src.publication.article_quality import ArticleReaderQualityFinding
from src.publication.article_quality_policy import article_quality_policy
from src.publication.article_temporal_checks import REDUNDANT_PARAGRAPH, temporal_quality_findings

UTC = dt.timezone.utc
SENT = dt.datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def _support(support_id: str) -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text=f"Факт {support_id}.",
        source_text=f"Факт {support_id}.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=(),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=SENT,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="community_report",
        story_id=f"story:{support_id}",
        sent_at=SENT,
    )


def _context(*ids: str) -> ArticleEditorialContext:
    supports = tuple(_support(i) for i in ids)
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
        publication_window=PublicationWindow(
            snapshot_at=dt.datetime(2026, 11, 12, 18, 48, tzinfo=UTC),
            lookback_start=dt.datetime(2026, 11, 11, 21, 51, tzinfo=UTC),
        ),
        edition_timezone="UTC",
    )


def _draft(*paragraphs: ArticleParagraph) -> StructuredArticleDraft:
    return StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("s1",),
        lead="Лид.",
        lead_support_ids=("s1",),
        sections=(
            ArticleSection(
                heading="Электроснабжение",
                heading_support_ids=("s1",),
                paragraphs=paragraphs,
            ),
        ),
    )


FIRST = ArticleParagraph(
    text=(
        "На Горе электричество пропало на несколько часов. "
        "Жители сообщают, что ремонт на линии ещё не завершён."
    ),
    cited_support_ids=("s1", "s2"),
)


def test_restated_paragraph_with_the_same_supports_is_a_repair_finding() -> None:
    # Defect (D16): P002 repeats P001 with the same supports.
    context = _context("s1", "s2")
    repeat = ArticleParagraph(
        text=(
            "На Горе электричество пропало на несколько часов, а ремонт на линии ещё не завершён."
        ),
        cited_support_ids=("s1",),
    )

    found = temporal_quality_findings(_draft(FIRST, repeat), context)

    assert [(f.code, f.unit_id) for f in found] == [(REDUNDANT_PARAGRAPH, "P002")]


def test_paragraph_with_a_new_support_is_not_redundant() -> None:
    # Faithful: P002 adds a support that P001 did not cite.
    context = _context("s1", "s2", "s3")
    new_fact = ArticleParagraph(
        text=(
            "На Горе электричество пропало на несколько часов. Жители сообщают о ремонте на линии."
        ),
        cited_support_ids=("s1", "s3"),
    )

    assert temporal_quality_findings(_draft(FIRST, new_fact), context) == ()


def test_paragraph_on_the_same_supports_with_new_content_is_not_redundant() -> None:
    # Faithful: the same support, a different development (new time and place).
    context = _context("s1", "s2")
    new_detail = ArticleParagraph(
        text=(
            "В Лисках воду дали утром, а в районе Центрального рынка напор "
            "оставался слабым до обеда."
        ),
        cited_support_ids=("s1",),
    )

    assert temporal_quality_findings(_draft(FIRST, new_detail), context) == ()


def test_subset_supports_alone_are_not_enough_without_repeated_wording() -> None:
    context = _context("s1", "s2")
    subset = ArticleParagraph(
        text="Вода в нагорной части появилась вечером, по сообщениям соседей.",
        cited_support_ids=("s2",),
    )

    assert temporal_quality_findings(_draft(FIRST, subset), context) == ()


def test_redundant_paragraph_is_a_non_blocking_quality_finding() -> None:
    policy = article_quality_policy(REDUNDANT_PARAGRAPH)

    assert policy.severity == "repair"
    assert policy.publication_effect == "repair_recommended"
    finding = ArticleReaderQualityFinding(
        code=REDUNDANT_PARAGRAPH,
        unit_id="P002",
        message="Повтор",
        support_ids=("s1",),
        severity="repair",
    )
    assert finding.to_metadata()["severity"] == "repair"
