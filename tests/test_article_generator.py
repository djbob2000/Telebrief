"""Tests for article generator and news-style editorial article prompt."""

# pylint: disable=import-error

from pathlib import Path

import pytest


@pytest.mark.unit
def test_article_prompt_template_exists_and_contains_rules():
    """System prompt file must exist and contain core editorial rules."""
    prompt_path = Path("src/prompts/article_news_style.txt")
    assert prompt_path.exists()
    content = prompt_path.read_text(encoding="utf-8")
    assert "{language}" in content
    assert "Бердянск" in content
    assert "Напомним" in content
    assert "##" in content
    assert "pro.berdyansk.biz" in content
