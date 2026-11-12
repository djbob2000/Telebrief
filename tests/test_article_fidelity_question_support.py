# ruff: noqa: S101
"""article-fidelity W2.2: a resident question backs a question, never a fact."""

from __future__ import annotations

from src.publication.article_claim_support import assess_claim_against_supports
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import validate_article_draft


def _support(
    sid: str,
    text: str,
    source: str,
    *,
    kind: str = "community_report",
    use: str = "PUBLISH",
) -> ArticleSupport:
    return ArticleSupport(
        support_id=sid,
        text=text,
        source_text=source,
        support_kind="evidence",
        publication_use=use,  # type: ignore[arg-type]
        source_refs=("src",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        temporal_role="CURRENT_WINDOW",
        evidence_kind=kind,
    )


QUESTION = _support(
    "story:1:evidence:0:frag:612180",
    "Вы звонили в теплосеть? Че говорят?",
    "Вы звонили в теплосеть? Че говорят?",
    kind="resident_question",
    use="CONTEXT",
)
POWER = _support(
    "story:1:evidence:1:frag:639128",
    "По сообщению жителя, свет выключили ещё около 2 часов дня.",
    "Нет. Еще часа в 2 дня выключили",
)
SUPPORTS = (QUESTION, POWER)


def _draft(text: str, cited: tuple[str, ...]) -> StructuredArticleDraft:
    return StructuredArticleDraft(
        title="Заголовок статьи",
        title_support_ids=(POWER.support_id,),
        lead="Лид статьи.",
        lead_support_ids=(POWER.support_id,),
        sections=(
            ArticleSection(
                heading="Коммунальная обстановка",
                heading_support_ids=(POWER.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text=text,
                        cited_support_ids=cited,
                        claims=(ArticleClaimAtom(text=text, cited_support_ids=cited),),
                    ),
                ),
            ),
        ),
    )


def _context() -> ArticleEditorialContext:
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=SUPPORTS,
        support_by_id={s.support_id: s for s in SUPPORTS},
        recurring_topics=(),
    )


def test_heating_term_from_a_question_does_not_back_a_fact() -> None:
    claim = "Линия теплосети занята"
    assessment = assess_claim_against_supports(claim, [QUESTION, POWER])
    assert not assessment.supported
    assert "concept:heating" in assessment.blocking_critical_terms


def test_question_framed_claim_passes_on_the_question_alone() -> None:
    claim = "Жители спрашивали, дозвонился ли кто-нибудь в теплосеть."
    assessment = assess_claim_against_supports(claim, [QUESTION])
    assert assessment.supported
    assert assessment.blocking_critical_terms == ()


def test_question_framing_does_not_license_a_concrete_value() -> None:
    # The inquiry frame is allowed; a time that no fact supports still blocks.
    claim = "Жители спрашивали, дозвонился ли кто-нибудь в теплосеть в 15:00."
    assessment = assess_claim_against_supports(claim, [QUESTION])
    assert not assessment.supported
    assert [c.raw.strip() for c in assessment.unsupported_concrete_claims] == ["15:00"]


def test_validator_rejects_the_heating_fact_cited_only_to_a_question_and_a_power_report() -> None:
    text = "Линия теплосети занята с двух часов дня."
    result = validate_article_draft(
        _draft(text, (QUESTION.support_id, POWER.support_id)), _context()
    )
    critical = [i for i in result.issues if i.code == "UNSUPPORTED_CRITICAL_TERM"]
    assert critical, [i.code for i in result.issues]
    assert all(i.blocking for i in critical)
    # The backstop needs the named sentence, not only the unit.
    assert critical[0].claim_text == text


def test_validator_accepts_the_question_framed_paragraph_citing_the_question() -> None:
    text = "Жители спрашивали, дозвонился ли кто-нибудь в теплосеть."
    result = validate_article_draft(_draft(text, (QUESTION.support_id,)), _context())
    blocking = [i.code for i in result.issues if i.blocking]
    assert "UNSUPPORTED_CRITICAL_TERM" not in blocking
    assert "QUESTION_CONTEXT_OVERCLAIM" not in blocking
    assert "UNSUPPORTED_CLAIM_ATOM" not in blocking
