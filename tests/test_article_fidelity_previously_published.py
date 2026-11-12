# ruff: noqa: S101
"""article-fidelity W1.2: material already published by the previous issue is not cited as new."""

from __future__ import annotations

import datetime as dt

from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    PublicationWindow,
)
from src.publication.article_material import project_article_material

UTC = dt.timezone.utc
SENT = dt.datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def _support(
    support_id: str,
    story_id: str,
    text: str,
    *,
    previously_published: bool = False,
) -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text=text,
        source_text=text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=(),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=SENT,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="community_report",
        story_id=story_id,
        sent_at=SENT,
        previously_published=previously_published,
    )


def _context(*supports: ArticleSupport) -> ArticleEditorialContext:
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


def test_story_with_a_new_and_a_yesterday_message_cites_only_the_new_one() -> None:
    old = _support(
        "story:1:old",
        "story:1",
        "Напор воды на Димитрова упал до 120 В.",
        previously_published=True,
    )
    new = _support(
        "story:1:new",
        "story:1",
        "Сегодня на Димитрова напор воды вернулся.",
    )

    projection = project_article_material(_context(old, new))

    assert projection.text_by_support_id["story:1:old"] == ""
    assert projection.text_by_support_id["story:1:new"].strip() != ""
    assert projection.actions_by_support_id["story:1:old"] == "SUPPRESS_PROMOTION_ONLY"
    assert projection.reasons_by_support_id["story:1:old"] == "previously_published"
    # The Story stays: it still has citable new material.
    assert "story:1" not in projection.suppressed_story_ids


def test_story_with_only_yesterday_messages_is_not_in_the_material() -> None:
    old = _support(
        "story:2:old",
        "story:2",
        "Вода на Горе появилась вечером.",
        previously_published=True,
    )
    other = _support("story:3:new", "story:3", "Лифт в доме на Лисках работает.")

    projection = project_article_material(_context(old, other))

    assert "story:2" in projection.suppressed_story_ids
    assert "story:2" in projection.previously_published_story_ids
    assert "story:3" not in projection.suppressed_story_ids
    assert projection.text_by_support_id["story:3:new"].strip() != ""


def test_first_publication_excludes_nothing() -> None:
    # No previous issue: no support is marked previously published.
    first = _support("story:4:a", "story:4", "Свет дали в 9 часов утра.")
    second = _support("story:4:b", "story:4", "Свет дали в 12 часов дня.")

    projection = project_article_material(_context(first, second))

    assert projection.suppressed_story_ids == ()
    assert projection.previously_published_story_ids == ()
    assert projection.reasons_by_support_id["story:4:a"] != "previously_published"
    assert projection.text_by_support_id["story:4:a"].strip() != ""
    assert projection.text_by_support_id["story:4:b"].strip() != ""


def test_published_material_stays_in_the_provenance_of_the_support() -> None:
    old = _support("story:5:old", "story:5", "Пекарня открыла двери.", previously_published=True)

    context = _context(old)

    assert context.support_by_id["story:5:old"].previously_published is True
    assert context.support_by_id["story:5:old"].text == "Пекарня открыла двери."
