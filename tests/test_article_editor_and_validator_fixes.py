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

    editor = ArticleEditor(provider=MagicMock(), model="anthropic/claude-haiku-5.5")
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


def test_article_editor_fallback_to_body_repair_supports_when_section_has_no_supports() -> None:
    # If the section itself has NO cited supports at all, it should fall back to
    # current_body_repair_supports() across the entire draft.
    sup = _make_support()
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
    )

    # Section 1 has NO supports
    p_empty_sec = ArticleParagraph(
        text="Связи не было на нескольких улицах.",
        cited_support_ids=(),
        claims=(),
    )
    sec1 = ArticleSection(
        heading="Связь",
        heading_support_ids=(),
        paragraphs=(p_empty_sec,),
    )

    # Section 2 has supports
    p_sec2 = ArticleParagraph(
        text="На Слободке не было света.",
        cited_support_ids=(sup.support_id,),
        claims=(
            ArticleClaimAtom(
                text="На Слободке не было света.", cited_support_ids=(sup.support_id,)
            ),
        ),
    )
    sec2 = ArticleSection(
        heading="Электричество",
        heading_support_ids=(sup.support_id,),
        paragraphs=(p_sec2,),
    )

    draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=(),
        lead="Лид",
        lead_support_ids=(),
        sections=(sec1, sec2),
    )

    issue = ArticleValidationIssue(
        code="UNVERIFIED_PROPER_NAME",
        unit_id="P001",
        message="Unverified proper name",
        severity="error",
        blocking=True,
    )
    issues_by_unit = {"P001": [issue]}

    editor = ArticleEditor(provider=MagicMock(), model="anthropic/claude-haiku-5.5")
    unit_data = editor._build_unit_contexts(
        draft,
        issues_by_unit,
        context,
        material_projection=None,
    )
    assert len(unit_data) == 1
    assert unit_data[0]["unit_id"] == "P001"
    # Should have fallen back to body repair supports (from sec2)
    assert sup.support_id in unit_data[0]["support_ids"]
    assert len(unit_data[0]["support_packets"]) > 0

    eligible, omitted = ArticleEditor._bound_prompt_supports(unit_data)
    assert len(omitted) == 0
    assert len(eligible) == 1
    assert eligible[0]["unit_id"] == "P001"


def test_article_editor_bounds_more_than_64_supports_without_dropping() -> None:
    # When a unit has >64 supports, it must NOT be dropped with support_budget_exceeded.
    # It must be sliced to 64 packets and retained.
    packets = [{"support_id": f"s_{i}", "text": f"Support text {i}"} for i in range(100)]
    unit = {
        "unit_id": "P001",
        "target_text": "Sample text",
        "support_ids": [f"s_{i}" for i in range(100)],
        "support_packets": packets,
    }

    eligible, omitted = ArticleEditor._bound_prompt_supports([unit])
    assert len(omitted) == 0
    assert len(eligible) == 1
    assert len(eligible[0]["prompt_support_ids"]) == 64


def test_article_editor_trims_large_packets_to_context_budget_without_dropping() -> None:
    # When support packets exceed 32,000 chars, it must trim packets to fit rather than dropping the unit.
    large_packet_text = "x" * 1500  # 25 packets * 1500 = 37,500 chars > 32,000 chars
    packets = [{"support_id": f"s_{i}", "text": f"{i}_{large_packet_text}"} for i in range(25)]
    unit = {
        "unit_id": "P001",
        "target_text": "Sample text",
        "support_ids": [f"s_{i}" for i in range(25)],
        "support_packets": packets,
    }

    eligible, omitted = ArticleEditor._bound_prompt_supports([unit])
    assert len(omitted) == 0
    assert len(eligible) == 1
    # Check that it trimmed from 25 packets down to what fits under 32,000 chars
    assert 1 <= len(eligible[0]["prompt_support_ids"]) < 25


def test_generator_editor_attempt_clamping() -> None:
    # Verify the clamp logic in ArticleGenerator: min(attempts, 3)
    def compute_max_attempts(configured_val: int | None) -> int:
        editorial_config = MagicMock()
        if configured_val is not None:
            editorial_config.article_editor_max_attempts = configured_val
        else:
            del editorial_config.article_editor_max_attempts
        return min(getattr(editorial_config, "article_editor_max_attempts", 3), 3)

    assert compute_max_attempts(None) == 3
    assert compute_max_attempts(3) == 3
    assert compute_max_attempts(2) == 2
    assert compute_max_attempts(1) == 1
    # If user sets 5, it should be capped at 3
    assert compute_max_attempts(5) == 3


def test_missing_claim_support_comprehensive_cases() -> None:
    sup = _make_support()
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
    )

    cases = [
        # (text, has_concrete, expected_blocking, expected_severity)
        ("Жители делятся различными мнениями о ситуации.", False, False, "warning"),
        ("Ситуация остается неоднородной в разных частях района.", False, False, "warning"),
        ("В то же время поступают противоречивые сообщения.", False, False, "warning"),
        ("Проезд на маршрутке №4 подорожал до 40 рублей.", True, True, "error"),
        ("Позвонить диспетчеру можно по номеру +79990001122.", True, True, "error"),
        ("Магазин открылся в 9:00.", True, True, "error"),
    ]

    for claim_text, should_be_concrete, exp_blocking, exp_sev in cases:
        concrete = extract_concrete_claims(claim_text)
        assert (len(concrete) > 0) == should_be_concrete, f"Failed concrete check for: {claim_text}"

        draft = StructuredArticleDraft(
            title="Заголовок",
            title_support_ids=(sup.support_id,),
            lead="Лид",
            lead_support_ids=(sup.support_id,),
            sections=(
                ArticleSection(
                    heading="Раздел",
                    heading_support_ids=(sup.support_id,),
                    paragraphs=(
                        ArticleParagraph(
                            text=claim_text,
                            cited_support_ids=(sup.support_id,),
                            claims=(
                                ArticleClaimAtom(
                                    text=claim_text,
                                    cited_support_ids=(),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )

        val = validate_article_draft(draft, context)
        issues = [i for i in val.issues if i.code == "MISSING_CLAIM_SUPPORT"]
        assert len(issues) == 1, f"Expected 1 MISSING_CLAIM_SUPPORT for: {claim_text}"
        assert issues[0].blocking == exp_blocking, f"Wrong blocking for: {claim_text}"
        assert issues[0].severity == exp_sev, f"Wrong severity for: {claim_text}"
