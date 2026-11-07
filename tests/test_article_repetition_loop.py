# ruff: noqa: S101
"""Regression tests for the REPEATED_CONTENT_LOOP check in the article validator.

Near-duplicate sentences inside one paragraph are a loop. Sentences that differ
only in a number are different facts and must not be flagged.
"""

from __future__ import annotations

from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import validate_article_draft

FIRST = (
    "На улице Карла Маркса 12 домов остались без света, воды и газа до позднего вечера четверга."
)
# Same facts with one extra word: 12 of 13 content stems shared (0.92 >= 0.90).
NEAR_DUPLICATE = "На улице Карла Маркса 12 домов остались полностью без света, воды и газа до позднего вечера четверга."
# Same wording, different number: a different fact.
OTHER_NUMBER = (
    "На улице Карла Маркса 14 домов остались без света, воды и газа до позднего вечера четверга."
)
UNRELATED = "Ремонтная бригада обещала вернуть воду на Слободку к утру четверга."


def _validate_pair(first: str, second: str):  # noqa: ANN202 - test helper
    sentences = (first, second)
    supports = tuple(
        ArticleSupport(
            support_id=f"story:1:evidence:{index}:frag:{index}",
            text=text,
            source_text=text,
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"src{index}",),
            fragment_ids=(index,),
            source_item_ids=(index,),
            observed_at=None,
            temporal_role="CURRENT_WINDOW",
        )
        for index, text in enumerate(sentences)
    )
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
    )
    paragraph = ArticleParagraph(
        text=" ".join(sentences),
        cited_support_ids=tuple(support.support_id for support in supports),
        claims=tuple(
            ArticleClaimAtom(text=text, cited_support_ids=(support.support_id,))
            for text, support in zip(sentences, supports, strict=True)
        ),
    )
    draft = StructuredArticleDraft(
        title="Итоги октября",
        title_support_ids=(supports[0].support_id,),
        lead="Октябрь в городе.",
        lead_support_ids=(supports[0].support_id,),
        sections=(
            ArticleSection(
                heading="Электроснабжение",
                heading_support_ids=(supports[0].support_id,),
                paragraphs=(paragraph,),
                cited_evidence_ids=tuple(support.support_id for support in supports),
            ),
        ),
        cited_evidence_ids=tuple(support.support_id for support in supports),
    )
    return validate_article_draft(draft, context)


def _loop_issues(result) -> list:  # noqa: ANN001 - test helper
    return [issue for issue in result.issues if issue.code == "REPEATED_CONTENT_LOOP"]


def test_near_duplicate_sentences_with_same_numbers_are_a_blocking_loop() -> None:
    issues = _loop_issues(_validate_pair(FIRST, NEAR_DUPLICATE))

    assert len(issues) == 1
    assert issues[0].blocking is True


def test_same_wording_with_different_number_is_not_a_loop() -> None:
    assert _loop_issues(_validate_pair(FIRST, OTHER_NUMBER)) == []


def test_unrelated_sentences_are_not_a_loop() -> None:
    assert _loop_issues(_validate_pair(FIRST, UNRELATED)) == []
