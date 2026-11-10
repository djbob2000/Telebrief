# ruff: noqa: S101
"""Writer reasoning effort: period reviews may run at their own level (AGENTS.md §0.12)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.article_generator import _article_writer_reasoning_effort
from src.config.parsers.publication import _parse_publication_editorial_config
from src.config.schemas.publication import PublicationEditorialConfig


def test_period_review_effort_defaults_to_none() -> None:
    assert PublicationEditorialConfig().period_review_writer_reasoning_effort is None


def test_period_review_effort_parses_and_validates() -> None:
    parsed = _parse_publication_editorial_config(
        {"publication_editorial": {"period_review_writer_reasoning_effort": "high"}}
    )
    assert parsed.period_review_writer_reasoning_effort == "high"

    with pytest.raises(ValueError, match="period_review_writer_reasoning_effort"):
        PublicationEditorialConfig(period_review_writer_reasoning_effort="extreme")


def test_daily_article_keeps_global_writer_effort() -> None:
    settings = SimpleNamespace(reasoning_effort="low")
    editorial = PublicationEditorialConfig(period_review_writer_reasoning_effort="high")

    assert _article_writer_reasoning_effort(editorial, settings, "") == "low"


def test_period_review_uses_its_own_effort() -> None:
    settings = SimpleNamespace(reasoning_effort="low")
    editorial = PublicationEditorialConfig(period_review_writer_reasoning_effort="high")

    assert _article_writer_reasoning_effort(editorial, settings, "month") == "high"


def test_period_review_without_own_effort_falls_back_to_writer_effort() -> None:
    settings = SimpleNamespace(reasoning_effort="low")
    editorial = PublicationEditorialConfig(article_writer_reasoning_effort="medium")

    assert _article_writer_reasoning_effort(editorial, settings, "month") == "medium"


def test_missing_publication_editorial_falls_back_to_global_effort() -> None:
    settings = SimpleNamespace(reasoning_effort="low")

    assert _article_writer_reasoning_effort(None, settings, "month") == "low"
    assert _article_writer_reasoning_effort(None, SimpleNamespace(), "") == "low"
