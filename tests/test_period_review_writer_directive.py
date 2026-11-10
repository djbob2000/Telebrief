# ruff: noqa: S101
"""A period review is written as a synthesis, and only period reviews are."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from src.article_generator import _article_lead_instruction
from src.publication.article_context import ArticleEditorialContext, PublicationWindow
from src.publication.article_writer_context import render_article_writer_context_with_stats
from src.publication.period_review import (
    PERIOD_REVIEW_COMPOSITION_DIRECTIVE,
    render_review_outline,
    review_writer_note,
    review_writer_reminder,
)

UTC = dt.timezone.utc
_OLD_TRAJECTORY_LINE = "Trace trajectory evolution with precise effective-time anchors"


def _render(lookback_days: int, period_review_kind: str = "") -> str:
    snapshot_at = dt.datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(),
        support_by_id={},
        recurring_topics=(),
        publication_window=PublicationWindow(
            snapshot_at=snapshot_at,
            lookback_start=snapshot_at - dt.timedelta(days=lookback_days),
        ),
        period_review_kind=period_review_kind,
    )
    rendered, _stats = render_article_writer_context_with_stats(context)
    return rendered


def test_period_review_gets_synthesis_directive_instead_of_day_by_day_tracing() -> None:
    rendered = _render(30, period_review_kind="month")

    assert PERIOD_REVIEW_COMPOSITION_DIRECTIVE in rendered
    assert _OLD_TRAJECTORY_LINE not in rendered


def test_weekly_article_keeps_the_longitudinal_directive() -> None:
    rendered = _render(7)

    assert _OLD_TRAJECTORY_LINE in rendered
    assert PERIOD_REVIEW_COMPOSITION_DIRECTIVE not in rendered


def test_daily_article_gets_neither_directive() -> None:
    rendered = _render(1)

    assert _OLD_TRAJECTORY_LINE not in rendered
    assert PERIOD_REVIEW_COMPOSITION_DIRECTIVE not in rendered


def test_directive_asks_for_synthesis_without_losing_detail_or_stretching_scope() -> None:
    directive = PERIOD_REVIEW_COMPOSITION_DIRECTIVE

    # Too much of a log: a storyline must not be walked through by date.
    assert "not a diary" in directive
    assert "Never walk through a storyline day by day" in directive
    # Too generic: the pattern still has to be proved by concrete detail.
    assert "a generalization never replaces the detail that shows it" in directive
    # Synthesis must not become an upgrade of a single report's scope.
    assert "Do not stretch one report" in directive


def test_directive_stays_generic_about_the_edition() -> None:
    lowered = PERIOD_REVIEW_COMPOSITION_DIRECTIVE.lower()

    for local_name in ("бердянск", "колони", "слободк", "акз", "гора"):
        assert local_name not in lowered


def test_partial_period_note_requires_the_real_span_in_title_and_lead() -> None:
    note = review_writer_note(
        {
            "kind": "month",
            "label": "сентября 2026 года: 11–30 сентября",
            "partial": True,
            "coverage_start": "2026-09-11",
        }
    )

    assert "«Итоги сентября 2026 года: 11–30 сентября»" in note
    assert "in both the title and the lead" in note
    assert "The start of the material is not the start of events" in note


def test_full_period_note_does_not_claim_partial_coverage() -> None:
    note = review_writer_note({"kind": "month", "label": "октября 2026 года", "partial": False})

    assert "«Итоги октября 2026 года»" in note
    assert "Material was collected only" not in note
    assert "start of the material" not in note


_PARTIAL_PLAN = {
    "kind": "month",
    "label": "сентября 2026 года: 11–30 сентября",
    "partial": True,
    "period_start": "2026-09-01",
    "period_end": "2026-10-01",
    "coverage_start": "2026-09-11",
}
_FULL_PLAN = {
    "kind": "month",
    "label": "октября 2026 года",
    "partial": False,
    "period_start": "2026-10-01",
    "period_end": "2026-11-01",
    "coverage_start": "2026-10-01",
}


def test_partial_review_is_reminded_of_its_span_after_the_dossier() -> None:
    reminder = review_writer_reminder(_PARTIAL_PLAN)

    assert "Обзор охватывает только 11–30 сентября" in reminder
    assert "и в заголовке, и в лиде" in reminder
    assert "Дальше пишите о городе, а не о сообщениях" in reminder
    assert "не начало событий" in reminder


def test_full_review_reminder_does_not_claim_partial_coverage() -> None:
    reminder = review_writer_reminder(_FULL_PLAN)

    assert "Лид подводит итог всего периода" in reminder
    assert "Обзор охватывает только" not in reminder
    assert "Начало материала" not in reminder


def test_reminder_keeps_pattern_proof_and_stays_generic() -> None:
    reminder = review_writer_reminder(_PARTIAL_PLAN).lower()

    assert "разные свидетельства, а не счётчик" in reminder
    assert "одной фразой с контрастом" in reminder
    for local_name in ("бердянск", "колони", "слободк", "акз", "гора"):
        assert local_name not in reminder


def test_outline_marks_the_archive_start_only_for_a_partial_period() -> None:
    coverage = SimpleNamespace(story_ids=())

    assert "where the archive begins" in render_review_outline(_PARTIAL_PLAN, coverage)
    assert "where the archive begins" not in render_review_outline(_FULL_PLAN, coverage)


def test_lead_rule_is_a_period_outcome_only_for_reviews() -> None:
    daily = _article_lead_instruction("")
    review = _article_lead_instruction(review_writer_reminder(_FULL_PLAN))

    assert "панораму суток" in daily
    assert "панораму суток" not in review
    assert "итог всего периода" in review
