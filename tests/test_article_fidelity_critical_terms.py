# ruff: noqa: S101
"""article-fidelity W2.3: building types and schedules are checkable critical terms."""

from __future__ import annotations

from src.publication.article_claim_support import assess_claim_against_supports
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_semantic_lexicon import canonical_semantic_concepts
from src.publication.article_validator import validate_article_draft


def _support(sid: str, text: str, source: str | None = None) -> ArticleSupport:
    return ArticleSupport(
        support_id=sid,
        text=text,
        source_text=source if source is not None else text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("src",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="community_report",
    )


PRIVATE_SECTOR_641253 = _support(
    "story:5:evidence:0:frag:641253",
    "По сообщению жителя, в частном секторе нет света 4 недели.",
    "Нет, частный сектор отдельно. Тут нет света 4 неделю",
)
HIGHRISE_SCHEDULE = _support(
    "story:6:evidence:0:frag:700100",
    "В девятиэтажках подачу дают по графику.",
    "В девятиэтажки подачу дают по графику",
)
SCHEDULE_ONLY = _support(
    "story:7:evidence:0:frag:700101",
    "Подача воды идёт по графику.",
    "Подача идёт по графику",
)
PLAIN_POWER = _support(
    "story:8:evidence:0:frag:700102",
    "Свет на Горе не включали.",
    "На Горе света нет",
)


def _context(*supports: ArticleSupport) -> ArticleEditorialContext:
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
    )


def _draft(text: str, cited: str) -> StructuredArticleDraft:
    return StructuredArticleDraft(
        title="Заголовок статьи",
        title_support_ids=(cited,),
        lead="Лид статьи.",
        lead_support_ids=(cited,),
        sections=(
            ArticleSection(
                heading="Свет",
                heading_support_ids=(cited,),
                paragraphs=(
                    ArticleParagraph(
                        text=text,
                        cited_support_ids=(cited,),
                        claims=(ArticleClaimAtom(text=text, cited_support_ids=(cited,)),),
                    ),
                ),
            ),
        ),
    )


CAUGHT_HIGHRISE = (
    "«Тут нет света 4 неделю», — написал он, уточнив, что речь идёт именно о частном секторе, "
    "а не о соседних многоэтажках."
)
PASSING_PRIVATE = (
    "«Тут нет света 4 неделю», — написал он, уточнив, что речь идёт именно о частном секторе."
)


def test_building_type_absent_from_the_source_is_a_critical_term() -> None:
    assessment = assess_claim_against_supports(CAUGHT_HIGHRISE, [PRIVATE_SECTOR_641253])
    assert not assessment.supported
    assert "concept:apartment_block" in assessment.blocking_critical_terms


def test_validator_blocks_the_highrise_contrast_unsupported_by_the_source() -> None:
    result = validate_article_draft(
        _draft(CAUGHT_HIGHRISE, PRIVATE_SECTOR_641253.support_id),
        _context(PRIVATE_SECTOR_641253),
    )
    critical = [i for i in result.issues if i.code == "UNSUPPORTED_CRITICAL_TERM"]
    assert critical
    assert all(i.blocking for i in critical)


def test_the_same_claim_without_the_highrise_clause_passes() -> None:
    assessment = assess_claim_against_supports(PASSING_PRIVATE, [PRIVATE_SECTOR_641253])
    assert assessment.supported
    assert assessment.blocking_critical_terms == ()


def test_synonym_building_types_share_one_concept() -> None:
    assert "concept:apartment_block" in canonical_semantic_concepts("в девятиэтажках")
    assert "concept:apartment_block" in canonical_semantic_concepts("пятиэтажки на Морозова")
    assert "concept:apartment_block" in canonical_semantic_concepts("в многоэтажке")
    # «частный сектор» and «частные дома» are one housing type, not apartment blocks.
    assert "concept:apartment_block" not in canonical_semantic_concepts("частный сектор")
    assert "concept:private_house" in canonical_semantic_concepts("в частном секторе")
    assert "concept:private_house" in canonical_semantic_concepts("в частных домах")


def test_dormitory_is_a_separate_building_type() -> None:
    concepts = canonical_semantic_concepts("в общежитии на Морозова")
    assert "concept:dormitory" in concepts
    assert "concept:apartment_block" not in concepts


def test_private_houses_paraphrase_of_private_sector_passes() -> None:
    claim = "По сообщению жителя, в частных домах нет света четвёртую неделю."
    assessment = assess_claim_against_supports(claim, [PRIVATE_SECTOR_641253])
    assert assessment.blocking_critical_terms == ()


def test_schedule_term_is_backed_by_a_schedule_source() -> None:
    claim = "Свет остаётся предметом обещаний и графика."
    assessment = assess_claim_against_supports(claim, [PLAIN_POWER])
    assert not assessment.supported
    assert "concept:schedule" in assessment.blocking_critical_terms

    supported = assess_claim_against_supports("Подача идёт по графику.", [SCHEDULE_ONLY])
    assert supported.supported


def test_schedule_word_without_schedule_source_is_a_blocking_critical_term_in_validation() -> None:
    claim = "Свет остаётся предметом обещаний и графика."
    result = validate_article_draft(_draft(claim, PLAIN_POWER.support_id), _context(PLAIN_POWER))
    critical = [i for i in result.issues if i.code == "UNSUPPORTED_CRITICAL_TERM"]
    assert critical
    assert all(i.blocking for i in critical)


def test_building_synonym_in_source_supports_the_claim() -> None:
    claim = "В девятиэтажках подачу дают по графику."
    assessment = assess_claim_against_supports(claim, [HIGHRISE_SCHEDULE])
    assert assessment.supported
