# ruff: noqa: S101
"""Tests for ArticleEditor reasoning_effort configuration and usage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.config.parsers.publication import _parse_publication_editorial_config
from src.config.schemas.publication import PublicationEditorialConfig
from src.publication.article_editor import ArticleEditor


def test_publication_editorial_config_reasoning_effort_default() -> None:
    config = PublicationEditorialConfig()
    assert config.article_editor_reasoning_effort == "none"


def test_publication_editorial_config_reasoning_effort_validation() -> None:
    # Valid values
    cfg_high = PublicationEditorialConfig(article_editor_reasoning_effort="high")
    assert cfg_high.article_editor_reasoning_effort == "high"

    cfg_low = PublicationEditorialConfig(article_editor_reasoning_effort="low")
    assert cfg_low.article_editor_reasoning_effort == "low"

    cfg_none = PublicationEditorialConfig(article_editor_reasoning_effort=None)
    assert cfg_none.article_editor_reasoning_effort is None

    # Invalid value
    with pytest.raises(ValueError, match="article_editor_reasoning_effort"):
        PublicationEditorialConfig(article_editor_reasoning_effort="extreme")


def test_parse_publication_editorial_config_reasoning_effort() -> None:
    # If not specified in config dict, defaults to "none"
    parsed_default = _parse_publication_editorial_config({"publication_editorial": {}})
    assert parsed_default.article_editor_reasoning_effort == "none"

    # If specified as "high"
    parsed_high = _parse_publication_editorial_config(
        {"publication_editorial": {"article_editor_reasoning_effort": "high"}}
    )
    assert parsed_high.article_editor_reasoning_effort == "high"


def test_article_editor_init_default_reasoning_effort() -> None:
    mock_provider = MagicMock()
    # Default is "none"
    editor = ArticleEditor(provider=mock_provider, model="openai/gpt-6-luna")
    assert editor.reasoning_effort == "none"

    # Explicit value passes through
    editor_custom = ArticleEditor(
        provider=mock_provider, model="openai/gpt-6-luna", reasoning_effort="high"
    )
    assert editor_custom.reasoning_effort == "high"
