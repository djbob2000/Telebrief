"""Tests for fail-closed article generation policy and claim boundary fixes."""

from __future__ import annotations

import datetime as dt

import pytest

from src.config_loader import PublicationEditorialConfig
from src.publication.article_claims import (
    extract_concrete_claims,
    find_unsupported_claims,
    normalize_support_text,
)
from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
)
from src.publication.article_coverage import ArticleCoveragePlan, ArticleStoryCoverage
from src.publication.article_finalization import ArticleFinalizer
from src.publication.article_models import (
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_semantic_support import assess_semantic_support
from src.publication.errors import ArticlePublicationRejected
from tests.publication.test_article_recovery import RecordingAttemptObserver

_NOW = dt.datetime(2026, 9, 3, 19, 30, tzinfo=dt.timezone.utc)


@pytest.mark.unit
def test_proper_name_extraction_after_ellipsis_quote() -> None:
    """Words following quote ending with ellipsis (…) must not be misclassified as proper names."""
    text = (
        "На этом фоне звучит сдержанный оптимизм: "
        "«Пока впереди День города, есть повод для надежды. Наверное…» "
        "Ситуацию дополняют бытовые сложности водоканала."
    )
    supports = [
        "Пока впереди День города, есть повод для надежды. Наверное",
        "водоканал проводит ремонтные работы",
    ]
    signals = assess_semantic_support(text, supports, allowed_context_terms=("День города",))
    assert "ситуацию" not in signals.blocking_proper_names
    assert "ситуацию" not in signals.unmatched_proper_names


@pytest.mark.unit
def test_number_k_shorthand_normalization() -> None:
    """Sources with 3к / 3k / 3 тыс. correctly support written 3000 рублей."""
    support_text = "в больнице платите 3к± И в ПНД было 1400 по-моему"
    norm = normalize_support_text(support_text)
    assert "3000" in norm
    assert "1400" in norm

    text = "Стоимость медкомиссии жители оценивают примерно в 3000 рублей в больнице и около 1400 в ПНД."
    unsupported = find_unsupported_claims(text, [support_text])
    assert len(unsupported) == 0


@pytest.mark.unit
def test_date_does_not_extract_spurious_standalone_number() -> None:
    """Dates like '2 сентября' must not extract standalone number '2'."""
    text = "Вечер 2 сентября и ночь на 3 сентября жители встретили под звуки взрывов."
    claims = extract_concrete_claims(text)
    number_claims = [c for c in claims if c.kind == "number"]
    date_claims = [c for c in claims if c.kind == "date"]

    assert len(date_claims) == 2
    assert {c.raw for c in date_claims} == {"2 сентября", "3 сентября"}
    # The digits 2 and 3 inside the dates must NOT be emitted as naked quantity numbers
    assert len(number_claims) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_writer_failure_fails_closed_when_fallback_disabled() -> None:
    """When article_allow_deterministic_fallback=False, writer failure raises ArticlePublicationRejected."""
    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_allow_deterministic_fallback=False,
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    sup = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="Факт.",
        source_text="Факт.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="established_fact",
        story_id="story:1",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Тема",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
        edition_name="Бердянск",
        edition_anchor_terms=("Бердянск",),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Тема",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup.support_id,),
                detail_support_ids=(),
            ),
        )
    )

    with pytest.raises(ArticlePublicationRejected) as exc_info:
        await finalizer.finalize(
            writer_draft=None,
            writer_error=RuntimeError("AI model timeout"),
            writer_attempt_id=writer_id,
            context=context,
            coverage_plan=plan,
            editorial_config=editorial_config,
            attempt_observer=observer,
        )

    assert exc_info.value.reason == "writer_failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_invalid_draft_fails_closed_when_fallback_disabled() -> None:
    """When article_allow_deterministic_fallback=False, invalid validation raises ArticlePublicationRejected."""
    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_allow_deterministic_fallback=False,
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    sup = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="В городе ведутся работы.",
        source_text="В городе ведутся работы.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="established_fact",
        story_id="story:1",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Тема",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
        edition_name="Бердянск",
        edition_anchor_terms=("Бердянск",),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Тема",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup.support_id,),
                detail_support_ids=(),
            ),
        )
    )

    # Draft with completely unsupported invented fact
    invalid_draft = StructuredArticleDraft(
        title="Заголовок [story:1]",
        title_support_ids=(sup.support_id,),
        lead="Лид с выдуманным городом Париж и номером +79991234567.",
        lead_support_ids=(sup.support_id,),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=(sup.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text="В Париже зафиксированы аномалии.",
                        cited_support_ids=(sup.support_id,),
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(ArticlePublicationRejected) as exc_info:
        await finalizer.finalize(
            writer_draft=invalid_draft,
            writer_error=None,
            writer_attempt_id=writer_id,
            context=context,
            coverage_plan=plan,
            editorial_config=editorial_config,
            attempt_observer=observer,
        )

    assert exc_info.value.reason == "validation_failed"
