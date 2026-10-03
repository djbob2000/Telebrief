# ruff: noqa: S101
"""Tests for ArticleEditor and ArticleValidator fixes regarding prompt unit retention and validation blocking."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.config.parsers.publication import _parse_publication_editorial_config
from src.config.schemas.publication import PublicationEditorialConfig
from src.publication.article_claims import extract_concrete_claims
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_editor import ArticleEditor
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import (
    ArticleValidationIssue,
    validate_article_draft,
)


def _make_support(
    sid: str = "story:1:evidence:0:frag:100", text: str = "На Слободке не было света."
) -> ArticleSupport:
    return ArticleSupport(
        support_id=sid,
        text=text,
        source_text=text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("src1",),
        fragment_ids=(100,),
        source_item_ids=(1,),
        observed_at=None,
        temporal_role="CURRENT_WINDOW",
    )


def test_article_editor_max_attempts_config() -> None:
    config = PublicationEditorialConfig()
    assert config.article_editor_max_attempts == 3

    parsed = _parse_publication_editorial_config({"publication_editorial": {}})
    assert parsed.article_editor_max_attempts == 3

    parsed_custom = _parse_publication_editorial_config(
        {"publication_editorial": {"article_editor_max_attempts": 3}}
    )
    assert parsed_custom.article_editor_max_attempts == 3


def test_missing_claim_support_blocking_only_for_concrete_claims() -> None:
    # 1. Epistemic hedge / narrative bridge without concrete facts -> should be WARNING (non-blocking)
    hedge_text = "Эти рассказы не дают оснований описывать город как единое целое: в соседних районах условия различались."
    assert len(extract_concrete_claims(hedge_text)) == 0

    sup = _make_support()
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
    )

    draft_with_hedge = StructuredArticleDraft(
        title="Заголовок статьи",
        title_support_ids=(sup.support_id,),
        lead="Лид статьи.",
        lead_support_ids=(sup.support_id,),
        sections=(
            ArticleSection(
                heading="Обстановка со светом",
                heading_support_ids=(sup.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text="На Слободке не было света. " + hedge_text,
                        cited_support_ids=(sup.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="На Слободке не было света.",
                                cited_support_ids=(sup.support_id,),
                            ),
                            ArticleClaimAtom(
                                text=hedge_text,
                                cited_support_ids=(),  # No cited support for the narrative bridge
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    val = validate_article_draft(draft_with_hedge, context)
    missing_claim_issues = [iss for iss in val.issues if iss.code == "MISSING_CLAIM_SUPPORT"]
    assert len(missing_claim_issues) == 1
    assert missing_claim_issues[0].blocking is False
    assert missing_claim_issues[0].severity == "warning"

    # 2. Fact with concrete details (numbers, prices) without citation -> MUST be ERROR (blocking)
    fact_with_numbers = "Провайдер берет 150 рублей за 10 гигабайт."
    assert len(extract_concrete_claims(fact_with_numbers)) > 0

    draft_with_fact = StructuredArticleDraft(
        title="Заголовок статьи",
        title_support_ids=(sup.support_id,),
        lead="Лид статьи.",
        lead_support_ids=(sup.support_id,),
        sections=(
            ArticleSection(
                heading="Связь",
                heading_support_ids=(sup.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text=fact_with_numbers,
                        cited_support_ids=(sup.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text=fact_with_numbers,
                                cited_support_ids=(),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    val_fact = validate_article_draft(draft_with_fact, context)
    missing_claim_issues_fact = [
        iss for iss in val_fact.issues if iss.code == "MISSING_CLAIM_SUPPORT"
    ]
    assert len(missing_claim_issues_fact) == 1
    assert missing_claim_issues_fact[0].blocking is True
    assert missing_claim_issues_fact[0].severity == "error"


def test_article_editor_retains_paragraphs_with_missing_supports() -> None:
    # A paragraph that lacks cited supports should inherit fallback supports from the section/body
    # and NOT be dropped by _bound_prompt_supports.
    sup = _make_support()
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
    )

    p_with_issue = ArticleParagraph(
        text="В нагорной части перебои тоже затрагивали разные участки.",
        cited_support_ids=(),  # No cited supports
        claims=(),
    )

    sec_p1 = ArticleParagraph(
        text="На Слободке не было света.",
        cited_support_ids=(sup.support_id,),
        claims=(
            ArticleClaimAtom(
                text="На Слободке не было света.", cited_support_ids=(sup.support_id,)
            ),
        ),
    )

    draft = StructuredArticleDraft(
        title="Заголовок статьи",
        title_support_ids=(sup.support_id,),
        lead="Лид статьи.",
        lead_support_ids=(sup.support_id,),
        sections=(
            ArticleSection(
                heading="Электричество",
                heading_support_ids=(sup.support_id,),
                paragraphs=(sec_p1, p_with_issue),
            ),
        ),
    )

    issue = ArticleValidationIssue(
        code="CHAT_KITCHEN_LEAK",
        unit_id="P002",
        message="Contains chat leak",
        severity="error",
        blocking=True,
    )
    issues_by_unit = {"P002": [issue]}

    editor = ArticleEditor(provider=MagicMock(), model="openai/gpt-6-luna")
    unit_data = editor._build_unit_contexts(
        draft,
        issues_by_unit,
        context,
        material_projection=None,
    )
    assert len(unit_data) == 1
    assert unit_data[0]["unit_id"] == "P002"
    # Should have inherited candidate supports from the section
    assert len(unit_data[0]["support_ids"]) > 0
    assert len(unit_data[0]["support_packets"]) > 0

    eligible, omitted = ArticleEditor._bound_prompt_supports(unit_data)
    assert len(omitted) == 0
    assert len(eligible) == 1
    assert eligible[0]["unit_id"] == "P002"
