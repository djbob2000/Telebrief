# ruff: noqa: S101
"""Relative time in period reviews is detected outside quotations (§0.12 undated-span rule)."""

from __future__ import annotations

import pytest

from src.publication.article_quality import _review_relative_time_words
from src.publication.period_review import PERIOD_REVIEW_COMPOSITION_DIRECTIVE


@pytest.mark.parametrize(
    "text",
    [
        "Кирова дали свет на час назад.",
        "Свет появился сегодня утром.",
        "Позавчера на Коса свет не было.",
        "Свет дали 20 минут назад.",
        "Со 2 дней назад жители ждут.",
    ],
)
def test_relative_time_outside_quotes_is_flagged(text: str) -> None:
    assert _review_relative_time_words(text)


def test_relative_time_inside_quotes_is_left_alone() -> None:
    text = "Житель написал: «Свет дали сегодня утром», и это вся информация."
    assert _review_relative_time_words(text) == ()


def test_dated_paragraph_is_not_flagged() -> None:
    assert _review_relative_time_words("22 сентября на Слободке дали свет в 9:40.") == ()


def test_directive_asks_for_gender_neutral_and_no_joke_measurements() -> None:
    assert "«жители»" in PERIOD_REVIEW_COMPOSITION_DIRECTIVE
    assert "not a voltage reading" in PERIOD_REVIEW_COMPOSITION_DIRECTIVE
    assert "No more than two dates in one paragraph" in PERIOD_REVIEW_COMPOSITION_DIRECTIVE


def test_every_repairable_reader_quality_code_has_an_editor_instruction() -> None:
    # A repair-scope code without an instruction makes ArticleEditor raise and drop the pass.
    from src.publication.article_editor import ArticleEditor
    from src.publication.article_quality_policy import ARTICLE_QUALITY_FINDING_POLICIES

    for code, policy in ARTICLE_QUALITY_FINDING_POLICIES.items():
        if policy.repair_scope in {"unit", "support_units", "story_unit"}:
            # Raises ArticleQualityPolicyError when the instruction is missing.
            assert ArticleEditor._quality_repair_instruction(code), code
