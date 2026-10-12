"""A paragraph that only repeats another may be removed, never one carrying unique facts."""

# ruff: noqa: S101
from unittest.mock import MagicMock

from src.publication.article_editor import ArticleEditor
from src.publication.article_models import (
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)


def _draft(second_ids: tuple[str, ...]) -> StructuredArticleDraft:
    return StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("s1",),
        lead="Лид.",
        lead_support_ids=("s1",),
        sections=(
            ArticleSection(
                heading="Свет",
                paragraphs=(
                    ArticleParagraph(
                        text="На Горе света нет десятые сутки.", cited_support_ids=("s1", "s2")
                    ),
                    ArticleParagraph(
                        text="Десятые сутки на Горе без света.", cited_support_ids=second_ids
                    ),
                ),
            ),
        ),
    )


def _apply(draft: StructuredArticleDraft, repetition: frozenset[str]):
    outcomes: dict[str, dict[str, str]] = {}
    editor = ArticleEditor(provider=MagicMock(), model="anthropic/claude-haiku-5.5")
    result = editor.apply_patches(
        draft, {"P002": "[DELETE]"}, patch_outcomes=outcomes, repetition_unit_ids=repetition
    )
    return result, outcomes["P002"]


def test_repeated_paragraph_is_removed_when_its_facts_stay() -> None:
    result, outcome = _apply(_draft(("s2",)), frozenset({"P002"}))
    assert outcome["status"] == "applied"
    assert [p.text for p in result.sections[0].paragraphs] == ["На Горе света нет десятые сутки."]


def test_paragraph_with_unique_support_is_kept() -> None:
    result, outcome = _apply(_draft(("s2", "s3")), frozenset({"P002"}))
    assert outcome["reason"] == "whole_paragraph_deletion_not_allowed"
    assert len(result.sections[0].paragraphs) == 2


def test_deletion_needs_a_repetition_finding() -> None:
    result, outcome = _apply(_draft(("s2",)), frozenset())
    assert outcome["reason"] == "whole_paragraph_deletion_not_allowed"
    assert len(result.sections[0].paragraphs) == 2
