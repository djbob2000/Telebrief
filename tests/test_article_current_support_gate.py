# ruff: noqa: S101
"""Pre-writer gate: an article window without current PUBLISH support cannot pass."""

from __future__ import annotations

from src.article_generator import _writer_can_cite_current_publish
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.generation import ARTICLE_RUN_RETRY_REASONS


def _support(
    support_id: str,
    *,
    publication_use: str = "PUBLISH",
    temporal_role: str = "CURRENT_WINDOW",
) -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text=f"Факт {support_id}.",
        source_text=f"Факт {support_id}.",
        support_kind="evidence",
        publication_use=publication_use,
        source_refs=(),
        fragment_ids=(),
        source_item_ids=(),
        observed_at=None,
        temporal_role=temporal_role,
        evidence_kind="community_report",
        story_id=f"story:{support_id}",
    )


def _context(*supports: ArticleSupport) -> ArticleEditorialContext:
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
    )


def test_historical_only_window_cannot_satisfy_title_and_lead_policy():
    context = _context(_support("h1", temporal_role="HISTORICAL_CONTEXT"))

    assert _writer_can_cite_current_publish(context, ["h1"]) is False


def test_one_current_publish_support_in_the_exposed_set_is_enough():
    context = _context(_support("h1", temporal_role="HISTORICAL_CONTEXT"), _support("c1"))

    assert _writer_can_cite_current_publish(context, ["h1", "c1"]) is True


def test_current_support_outside_the_exposed_set_does_not_count():
    context = _context(_support("h1", temporal_role="HISTORICAL_CONTEXT"), _support("c1"))

    assert _writer_can_cite_current_publish(context, ["h1"]) is False


def test_current_context_only_support_does_not_count():
    context = _context(_support("c1", publication_use="CONTEXT"))

    assert _writer_can_cite_current_publish(context, ["c1"]) is False


def test_empty_exposure_cannot_satisfy_the_policy():
    context = _context(_support("c1"))

    assert _writer_can_cite_current_publish(context, []) is False


def test_pre_writer_rejection_is_not_retried_at_run_level():
    assert "global_incompleteness" not in ARTICLE_RUN_RETRY_REASONS
