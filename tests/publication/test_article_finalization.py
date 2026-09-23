"""Regression test for forced writer failure fallback quality and coverage."""

from __future__ import annotations

import datetime as dt

import pytest

from src.config_loader import PublicationEditorialConfig
from src.editorial_models import StoryCard
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleStoryAssignment,
    ArticleStoryCoverage,
    ArticleThematicSection,
)
from src.publication.article_finalization import ArticleFinalizer, _materialize_fallback_projection
from src.publication.article_material import ArticleMaterialProjection
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_quality import ArticleReaderQualityFinding, ArticleReaderQualityReport
from src.publication.errors import ArticlePublicationRejected
from tests.publication.test_article_recovery import RecordingAttemptObserver

_NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def quality_gate_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests focused on finalizer behavior unrelated to style diagnostics."""
    monkeypatch.setattr(
        "src.publication.article_finalization.diagnose_article_quality",
        lambda *args, **kwargs: ArticleReaderQualityReport(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_revalidates_writer_draft_after_structural_finalization(
    monkeypatch: pytest.MonkeyPatch,
    quality_gate_clear: None,
) -> None:
    """The finalizer validates the exact draft that it will render."""
    from src.publication.article_models import ArticleClaimAtom
    from src.publication.article_validator import validate_article_draft

    support = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="В Бердянске восстановили подачу электроэнергии.",
        source_text="В Бердянске восстановили подачу электроэнергии.",
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
        headline_candidates=("Электроснабжение",),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
        edition_anchor_terms=("Бердянск",),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(support.support_id,),
                detail_support_ids=(support.support_id,),
            ),
        )
    )
    draft = StructuredArticleDraft(
        title="Электроснабжение в Бердянске",
        title_support_ids=(support.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Электроснабжение в Бердянске",
                cited_support_ids=(support.support_id,),
            ),
        ),
        lead="В Бердянске восстановили подачу электроэнергии.",
        lead_support_ids=(support.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В Бердянске восстановили подачу электроэнергии",
                cited_support_ids=(support.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Электроснабжение",
                heading_support_ids=(support.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text="В Бердянске восстановили подачу электроэнергии.",
                        cited_support_ids=(support.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="В Бердянске восстановили подачу электроэнергии",
                                cited_support_ids=(support.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    config = PublicationEditorialConfig(
        article_min_sections=1,
        article_min_words=1,
        article_max_sections=3,
        article_allow_deterministic_fallback=False,
    )
    validation = validate_article_draft(draft, context, config)
    assert validation.is_valid

    validation_calls: list[tuple[object, ...]] = []
    real_validate = validate_article_draft

    def record_revalidation(*args: object, **kwargs: object):
        validation_calls.append(args)
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(
        "src.publication.article_finalization.validate_article_draft", record_revalidation
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")
    result = await ArticleFinalizer().finalize(
        writer_draft=draft,
        writer_error=None,
        writer_attempt_id=writer_id,
        context=context,
        coverage_plan=plan,
        editorial_config=config,
        length_profile=None,
        attempt_observer=observer,
        writer_validation=validation,
    )

    assert result.writer_status == "passed"
    assert len(validation_calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_warning_only_quality_finding_passes_without_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = ArticleSupport(
        support_id="story:warning:evidence:0:frag:1",
        text="На улице Садовой восстановили свет.",
        source_text="На улице Садовой восстановили свет.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:warning",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        evidence_kind="established_fact",
        story_id="story:warning",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Свет",),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id=support.story_id,
                topic="Свет",
                rank=1,
                prominence="BRIEF",
                support_ids=(support.support_id,),
            ),
        )
    )
    draft = StructuredArticleDraft(
        title="Свет на Садовой",
        title_support_ids=(support.support_id,),
        title_claims=(
            ArticleClaimAtom(text="Свет на Садовой", cited_support_ids=(support.support_id,)),
        ),
        lead=support.text,
        lead_support_ids=(support.support_id,),
        lead_claims=(ArticleClaimAtom(text=support.text, cited_support_ids=(support.support_id,)),),
        sections=(
            ArticleSection(
                heading="Городские службы",
                heading_support_ids=(support.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text=support.text,
                        cited_support_ids=(support.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text=support.text, cited_support_ids=(support.support_id,)
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    warning = ArticleReaderQualityReport(
        findings=(
            ArticleReaderQualityFinding(
                code="MISSING_DETAIL_SUPPORT",
                unit_id="P001",
                message="Можно сохранить полезную деталь.",
                severity="repair",
            ),
        )
    )
    monkeypatch.setattr(
        "src.publication.article_finalization.diagnose_article_quality",
        lambda *args, **kwargs: warning,
    )
    config = PublicationEditorialConfig(
        article_min_sections=1,
        article_min_words=1,
        article_allow_deterministic_fallback=False,
    )
    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    result = await ArticleFinalizer().finalize(
        writer_draft=draft,
        writer_error=None,
        writer_attempt_id=writer_id,
        context=context,
        coverage_plan=plan,
        editorial_config=config,
        attempt_observer=observer,
    )

    assert result.writer_status == "passed"
    assert result.metadata["reader_quality"]["counts_by_severity"]["repair"] == 1
    assert observer.finished_attempts[writer_id]["status"] == "succeeded"
    assert all(item["status"] != "failed" for item in observer.finished_attempts.values())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_uses_only_surviving_material_projection(
    quality_gate_clear: None,
) -> None:
    retained = ArticleSupport(
        support_id="story:retained:evidence:0:frag:1",
        text="На улице Садовой восстановили свет.",
        source_text="На улице Садовой восстановили свет.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:retained",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        evidence_kind="established_fact",
        story_id="story:retained",
    )
    suppressed = ArticleSupport(
        support_id="story:ad:evidence:0:frag:2",
        text="Продам товар, звоните по телефону.",
        source_text="Продам товар, звоните по телефону.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:ad",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=_NOW,
        evidence_kind="commercial_offer",
        story_id="story:ad",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Свет", "Объявление"),
        support_index=(retained, suppressed),
        support_by_id={s.support_id: s for s in (retained, suppressed)},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:retained",
                topic="Свет",
                rank=1,
                prominence="DEVELOP",
                support_ids=(retained.support_id,),
            ),
            ArticleStoryCoverage(
                story_id="story:ad",
                topic="Объявление",
                rank=2,
                prominence="BRIEF",
                support_ids=(suppressed.support_id,),
            ),
        )
    )
    projection = ArticleMaterialProjection(
        text_by_support_id={
            retained.support_id: retained.text,
            suppressed.support_id: suppressed.text,
        },
        actions_by_support_id={
            retained.support_id: "KEEP",
            # The explicit suppressed-story projection is authoritative even
            # if a stale action map still says KEEP.
            suppressed.support_id: "KEEP",
        },
        reasons_by_support_id={
            retained.support_id: "supported_material_retained",
            suppressed.support_id: "high_confidence_promotion_only",
        },
        suppressed_story_ids=("story:ad",),
    )
    config = PublicationEditorialConfig(
        article_min_sections=1,
        article_min_words=1,
        article_allow_deterministic_fallback=True,
    )
    writer_metadata = {
        "composition": {"bundle_count": 2},
        "as_of": "2026-08-30T15:00:00+03:00",
        "as_of_utc": "2026-08-30T12:00:00+00:00",
        "context_hash": "context-digest",
        "prompt_hash": "prompt-digest",
        "editor_retry_count": 1,
        "editor_patched_unit_ids": ["P001"],
    }

    result = await ArticleFinalizer().finalize(
        writer_draft=None,
        writer_error=RuntimeError("writer unavailable"),
        writer_attempt_id=0,
        context=context,
        coverage_plan=plan,
        editorial_config=config,
        writer_metadata=writer_metadata,
        material_projection=projection,
    )

    rendered = result.draft.render_markdown()
    assert result.recovery_mode == "full_fallback"
    assert "Продам товар" not in rendered
    assert "восстановили свет" in rendered
    assert set(result.final_covered_story_ids) == {"story:retained"}
    assert result.metadata["writer_attempt"] == writer_metadata
    assert result.metadata["composition"] == {"bundle_count": 2}
    assert result.metadata["as_of"] == "2026-08-30T15:00:00+03:00"
    assert result.metadata["as_of_utc"] == "2026-08-30T12:00:00+00:00"
    assert result.metadata["context_hash"] == "context-digest"
    assert result.metadata["prompt_hash"] == "prompt-digest"
    assert result.metadata["editor_retry_count"] == 1
    assert result.metadata["editor_patched_unit_ids"] == ["P001"]


@pytest.mark.unit
def test_fallback_filters_pooled_evidence_to_story_owner_and_retargets_lead() -> None:
    support_a = ArticleSupport(
        support_id="story:a:evidence:0:frag:11",
        text="Деталь истории А.",
        source_text="Деталь истории А.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:a",),
        fragment_ids=(11,),
        source_item_ids=(11,),
        observed_at=_NOW,
        story_id="story:a",
    )
    support_b = ArticleSupport(
        support_id="story:b:evidence:0:frag:12",
        text="Деталь истории Б.",
        source_text="Деталь истории Б.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:b",),
        fragment_ids=(12,),
        source_item_ids=(12,),
        observed_at=_NOW,
        story_id="story:b",
    )
    support_gone = ArticleSupport(
        support_id="story:gone:evidence:0:frag:13",
        text="Промо деталь.",
        source_text="Промо деталь.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:gone",),
        fragment_ids=(13,),
        source_item_ids=(13,),
        observed_at=_NOW,
        story_id="story:gone",
    )
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(support_a, support_b, support_gone),
        support_by_id={s.support_id: s for s in (support_a, support_b, support_gone)},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:a",
                topic="История А",
                rank=1,
                prominence="DEVELOP",
                support_ids=(support_a.support_id, support_b.support_id),
                detail_support_ids=(support_b.support_id,),
            ),
            ArticleStoryCoverage(
                story_id="story:b",
                topic="История Б",
                rank=2,
                prominence="WEAVE",
                support_ids=(support_b.support_id, support_a.support_id),
                detail_support_ids=(support_a.support_id,),
            ),
            ArticleStoryCoverage(
                story_id="story:gone",
                topic="Удалённая история",
                rank=3,
                prominence="BRIEF",
                support_ids=(support_gone.support_id,),
            ),
        ),
        sections=(
            ArticleThematicSection(
                section_id="section:stories",
                title="Истории",
                lead_story_id="story:gone",
                story_assignments=(
                    ArticleStoryAssignment(
                        story_id="story:a",
                        section_id="section:stories",
                        depth="DEVELOP",
                        rank=1,
                        primary_evidence_ids=(support_a.support_id, support_b.support_id),
                        concrete_details=(support_b.support_id,),
                    ),
                    ArticleStoryAssignment(
                        story_id="story:b",
                        section_id="section:stories",
                        depth="WEAVE",
                        rank=2,
                        primary_evidence_ids=(support_b.support_id, support_a.support_id),
                        concrete_details=(support_a.support_id,),
                    ),
                    ArticleStoryAssignment(
                        story_id="story:gone",
                        section_id="section:stories",
                        depth="BRIEF",
                        rank=3,
                        primary_evidence_ids=(support_gone.support_id,),
                    ),
                ),
                narrative_intent="Сопоставьте истории.",
            ),
        ),
    )
    projection = ArticleMaterialProjection(
        text_by_support_id={
            support_a.support_id: support_a.text,
            support_b.support_id: support_b.text,
        },
        actions_by_support_id={
            support_a.support_id: "KEEP",
            support_b.support_id: "KEEP",
            support_gone.support_id: "TRIM_DIRECTORY",
        },
        reasons_by_support_id={},
    )

    projected_context, projected_plan = _materialize_fallback_projection(context, plan, projection)

    assert set(projected_context.support_by_id) == {support_a.support_id, support_b.support_id}
    assert projected_plan.story_ids == ("story:a", "story:b")
    assignment_a, assignment_b = projected_plan.sections[0].story_assignments
    assert assignment_a.primary_evidence_ids == (support_a.support_id,)
    assert assignment_a.concrete_details == ()
    assert assignment_b.primary_evidence_ids == (support_b.support_id,)
    assert assignment_b.concrete_details == ()
    assert projected_plan.sections[0].lead_story_id == "story:a"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_rejects_contradiction_after_orphan_merge_with_quality_metadata() -> None:
    start = _NOW - dt.timedelta(hours=1)
    end = _NOW + dt.timedelta(hours=1)
    sup_available = ArticleSupport(
        support_id="story:water-a:evidence:0:frag:701",
        text="На улице Садовой подачу воды восстановили.",
        source_text="На улице Садовой подачу воды восстановили.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:701",),
        fragment_ids=(701,),
        source_item_ids=(701,),
        observed_at=_NOW,
        effective_from=start,
        effective_until=end,
        evidence_kind="established_fact",
        story_id="story:water-a",
    )
    sup_restricted = ArticleSupport(
        support_id="story:water-b:evidence:0:frag:702",
        text="На улице Садовой подача воды остаётся ограниченной.",
        source_text="На улице Садовой подача воды остаётся ограниченной.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:702",),
        fragment_ids=(702,),
        source_item_ids=(702,),
        observed_at=_NOW,
        effective_from=start,
        effective_until=end,
        evidence_kind="established_fact",
        story_id="story:water-b",
    )
    supports = (sup_available, sup_restricted)
    context = ArticleEditorialContext(
        headline_candidates=("Вода на Садовой",),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=tuple(
            ArticleStoryCoverage(
                story_id=support.story_id,
                topic="Подача воды",
                rank=index,
                prominence="BRIEF",
                support_ids=(support.support_id,),
                detail_support_ids=(),
            )
            for index, support in enumerate(supports, start=1)
        )
    )
    draft = StructuredArticleDraft(
        title="Вода на Садовой",
        title_support_ids=(sup_available.support_id,),
        title_claims=(
            ArticleClaimAtom(text="Вода на Садовой", cited_support_ids=(sup_available.support_id,)),
        ),
        lead=sup_available.text,
        lead_support_ids=(sup_available.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text=sup_available.text, cited_support_ids=(sup_available.support_id,)
            ),
        ),
        sections=(
            ArticleSection(
                heading="Коммунальная обстановка",
                heading_support_ids=(sup_available.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text=sup_available.text,
                        cited_support_ids=(sup_available.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text=sup_available.text,
                                cited_support_ids=(sup_available.support_id,),
                            ),
                        ),
                    ),
                    ArticleParagraph(
                        text=sup_restricted.text,
                        cited_support_ids=(sup_restricted.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text=sup_restricted.text,
                                cited_support_ids=(sup_restricted.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    config = PublicationEditorialConfig(
        article_min_sections=1,
        article_min_words=1,
        article_allow_deterministic_fallback=True,
    )
    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")
    before_edit = ArticleReaderQualityReport(
        findings=(
            ArticleReaderQualityFinding(
                code="MISSING_DETAIL_SUPPORT",
                unit_id="P001",
                message="Canary detail text must not be persisted.",
                severity="repair",
            ),
        )
    )
    after_edit = ArticleReaderQualityReport(
        findings=(
            ArticleReaderQualityFinding(
                code="OVERLOADED_ROSTER_PARAGRAPH",
                unit_id="P002",
                message="Another private diagnostic detail.",
                severity="blocking",
            ),
        )
    )
    writer_metadata = {
        "editor_retry_count": 2,
        "editor_patched_unit_ids": ["P001", "P004"],
        "context_hash": "context-hash",
        "prompt_hash": "prompt-hash",
        "prompt_chars": 1234,
        "raw_prompt": "RAW PROMPT CANARY",
        "raw_response": "RAW RESPONSE CANARY",
        "provider_secret": "RAW SECRET CANARY",
    }

    with pytest.raises(ArticlePublicationRejected) as exc_info:
        await ArticleFinalizer().finalize(
            writer_draft=draft,
            writer_error=None,
            writer_attempt_id=writer_id,
            context=context,
            coverage_plan=plan,
            editorial_config=config,
            attempt_observer=observer,
            writer_metadata=writer_metadata,
            quality_report=before_edit,
            quality_report_after_edit=after_edit,
            writer_validation=None,
        )

    error = exc_info.value
    assert error.reason == "quality_failed"
    assert error.error_kind == "article_quality_rejected"
    assert error.metadata["stage"] == "post_finalization_quality"
    assert error.metadata["quality_version"] == "article-reader-quality-v3"
    assert error.metadata["unresolved_quality_findings"] == [
        {
            "code": "CONTRADICTORY_SERVICE_STATE",
            "unit_id": "LEAD",
            "severity": "blocking",
        }
    ]
    assert error.metadata["editor_attempt_count"] == 2
    assert error.metadata["patched_unit_ids"] == ["P001", "P004"]
    assert error.metadata["factual_validation"]["is_valid"] is True
    assert error.metadata["evidence_boundary_passed"] is True
    assert error.metadata["quality_gate_passed"] is False
    assert error.metadata["quality_before_edit"]["version"] == "article-reader-quality-v3"
    assert error.metadata["quality_after_edit"]["findings"][0]["code"] == (
        "OVERLOADED_ROSTER_PARAGRAPH"
    )
    assert error.metadata["quality_after_edit"]["findings"][0].get("message") is None
    observed_metadata = observer.finished_attempts[writer_id]["kwargs"]["metadata"]
    assert observed_metadata["writer_attempt"]["prompt_hash"] == "prompt-hash"
    assert "raw_prompt" not in observed_metadata["writer_attempt"]
    for private_payload in ("RAW PROMPT CANARY", "RAW RESPONSE CANARY", "RAW SECRET CANARY"):
        assert private_payload not in repr(error.metadata)
        assert private_payload not in repr(observed_metadata)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forced_writer_failure_fallback_regression(quality_gate_clear: None) -> None:
    """Test 10A: When writer fails/produces invalid draft, fallback draft passes validation with 100% coverage."""
    sup1 = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="В Бердянске восстановили подачу электроэнергии.",
        source_text="В Бердянске восстановили подачу электроэнергии.",
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
    sup2 = ArticleSupport(
        support_id="story:2:evidence:0:frag:202",
        text="Автобус №4 курсирует с интервалом в 30 минут.",
        source_text="Автобус №4 курсирует с интервалом в 30 минут.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="community_report",
        story_id="story:2",
    )
    sup3 = ArticleSupport(
        support_id="story:3:evidence:0:frag:303",
        text="В спорткомплексе открылся набор в секцию плавания.",
        source_text="В спорткомплексе открылся набор в секцию плавания.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:3",),
        fragment_ids=(3,),
        source_item_ids=(3,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="established_fact",
        story_id="story:3",
    )
    supports = (sup1, sup2, sup3)
    cards = (
        StoryCard(
            id="story:1",
            topic="Электроснабжение",
            importance="high",
            category="utilities",
            summary="Электроснабжение",
        ),
        StoryCard(
            id="story:2",
            topic="Транспорт",
            importance="medium",
            category="transport",
            summary="Транспорт",
        ),
        StoryCard(
            id="story:3",
            topic="Спорт",
            importance="low",
            category="social",
            summary="Спорт",
        ),
    )
    context = ArticleEditorialContext(
        headline_candidates=("Электроснабжение", "Городской транспорт", "Спорт"),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
        edition_anchor_terms=("Бердянск",),
        story_cards=cards,
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup1.support_id,),
                detail_support_ids=(sup1.support_id,),
            ),
            ArticleStoryCoverage(
                story_id="story:2",
                topic="Городской транспорт",
                rank=2,
                prominence="WEAVE",
                support_ids=(sup2.support_id,),
                detail_support_ids=(sup2.support_id,),
            ),
            ArticleStoryCoverage(
                story_id="story:3",
                topic="Спорт",
                rank=3,
                prominence="BRIEF",
                support_ids=(sup3.support_id,),
                detail_support_ids=(sup3.support_id,),
            ),
        )
    )

    # Invalid draft with unsupported proper names and leaked brackets
    invalid_draft = StructuredArticleDraft(
        title="Заголовок из ниоткуда",
        title_support_ids=("story:1:evidence:0:frag:101",),
        lead="Лид с выдуманным городом Париж.",
        lead_support_ids=("story:1:evidence:0:frag:101",),
        sections=(
            ArticleSection(
                heading="Секция",
                heading_support_ids=("story:1:evidence:0:frag:101",),
                paragraphs=(
                    ArticleParagraph(
                        text="[power] AVAILABLE везде.",
                        cited_support_ids=("story:1:evidence:0:frag:101",),
                    ),
                ),
            ),
        ),
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_min_sections=1,
        article_min_words=5,
        article_max_sections=6,
        article_allow_deterministic_fallback=True,
    )

    result = await finalizer.finalize(
        writer_draft=invalid_draft,
        writer_error=None,
        writer_attempt_id=writer_id,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
        length_profile=None,
        attempt_observer=observer,
    )

    assert result.writer_status == "rejected"
    assert result.recovery_mode == "full_fallback"
    from src.publication.article_validator import validate_article_draft

    val_res = validate_article_draft(result.draft, context, editorial_config)
    assert val_res.is_valid is True, f"Validation issues: {val_res.issues}"

    # Assert section count <= article_max_sections
    assert len(result.draft.sections) <= editorial_config.article_max_sections

    # Assert 0 leaks of `[...] AVAILABLE` or raw enums
    markdown = result.draft.render_markdown()
    assert "AVAILABLE" not in markdown
    assert "UNAVAILABLE" not in markdown
    assert "[" not in markdown and "]" not in markdown


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_repairs_unsupported_quote_without_full_fallback(
    quality_gate_clear: None,
) -> None:
    """Test: When writer draft has an unverified quote, finalizer converts to indirect speech without fallback."""
    from src.publication.article_models import ArticleClaimAtom

    sup1 = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="В Бердянске восстановили подачу электроэнергии.",
        source_text="В Бердянске восстановили подачу электроэнергии.",
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
        headline_candidates=("Электроснабжение",),
        support_index=(sup1,),
        support_by_id={sup1.support_id: sup1},
        recurring_topics=(),
        edition_anchor_terms=("Бердянск",),
        story_cards=(
            StoryCard(
                id="story:1",
                topic="Электроснабжение",
                importance="high",
                category="utilities",
                summary="Электроснабжение",
            ),
        ),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup1.support_id,),
                detail_support_ids=(sup1.support_id,),
            ),
        ),
    )

    # Draft with a quote not present in source: «Свет вернулся быстро»
    draft_with_unsupported_quote = StructuredArticleDraft(
        title="Электроснабжение в Бердянске",
        title_support_ids=(sup1.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Электроснабжение в Бердянске", cited_support_ids=(sup1.support_id,)
            ),
        ),
        lead="В Бердянске восстановили подачу электроэнергии.",
        lead_support_ids=(sup1.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В Бердянске восстановили подачу электроэнергии",
                cited_support_ids=(sup1.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Восстановление сетей",
                heading_support_ids=(sup1.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="По словам горожан, «свет вернулся быстро», и в Бердянске восстановили подачу электроэнергии.",
                        cited_support_ids=(sup1.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="В Бердянске восстановили подачу электроэнергии",
                                cited_support_ids=(sup1.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=40,
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_min_sections=1,
        article_min_words=5,
        article_max_sections=6,
        article_allow_deterministic_fallback=True,
    )

    result = await finalizer.finalize(
        writer_draft=draft_with_unsupported_quote,
        writer_error=None,
        writer_attempt_id=writer_id,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
        length_profile=None,
        attempt_observer=observer,
    )

    # Draft should be saved (not discarded to full_fallback)
    assert result.recovery_mode != "full_fallback"
    assert result.writer_status == "passed"
    # The quotation marks should be stripped, converting to indirect speech
    rendered = result.draft.render_markdown()
    assert "«свет вернулся быстро»" not in rendered
    assert "свет вернулся быстро" in rendered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writer_draft_repaired_by_sentence_pruning() -> None:
    """AGENTS.md 0.7: Paragraph with speculative unsupported sentence is repaired by pruning that sentence."""
    from src.publication.article_models import ArticleClaimAtom

    sup1 = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="В Бердянске восстановили подачу электроэнергии.",
        source_text="В Бердянске восстановили подачу электроэнергии.",
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
        headline_candidates=("Электроснабжение",),
        support_index=(sup1,),
        support_by_id={sup1.support_id: sup1},
        recurring_topics=(),
        edition_anchor_terms=("Бердянск",),
        story_cards=(
            StoryCard(
                id="story:1",
                topic="Электроснабжение",
                importance="high",
                category="utilities",
                summary="Электроснабжение",
            ),
        ),
        publication_window=None,
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup1.support_id,),
                detail_support_ids=(sup1.support_id,),
            ),
        )
    )

    draft_with_extra_sentence = StructuredArticleDraft(
        title="Электроснабжение в городе",
        title_support_ids=(sup1.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Электроснабжение в городе", cited_support_ids=(sup1.support_id,)
            ),
        ),
        lead="В городе продолжаются восстановительные работы.",
        lead_support_ids=(sup1.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В городе продолжаются восстановительные работы",
                cited_support_ids=(sup1.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Коммунальные службы",
                heading_support_ids=(sup1.support_id,),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="В Бердянске восстановили подачу электроэнергии. По слухам в неизвестных районах открылся космический космодром.",
                        cited_support_ids=(sup1.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="В Бердянске восстановили подачу электроэнергии",
                                cited_support_ids=(sup1.support_id,),
                            ),
                            ArticleClaimAtom(
                                text="По слухам в неизвестных районах открылся космический космодром",
                                cited_support_ids=(sup1.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=40,
    )

    observer = RecordingAttemptObserver()
    writer_id = await observer.attempt_started("writer")

    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_min_sections=1,
        article_min_words=5,
        article_max_sections=6,
        article_allow_deterministic_fallback=False,  # Strict: fail-closed if not repaired
    )

    result = await finalizer.finalize(
        writer_draft=draft_with_extra_sentence,
        writer_error=None,
        writer_attempt_id=writer_id,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
        length_profile=None,
        attempt_observer=observer,
    )

    assert result.writer_status == "passed"
    rendered = result.draft.render_markdown()
    assert "космодром" not in rendered
    assert "В Бердянске восстановили подачу электроэнергии" in rendered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_deduplicates_repeated_sentences_in_paragraph(
    quality_gate_clear: None,
) -> None:
    from src.publication.article_models import ArticleClaimAtom

    sup = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="В Бердянске восстановили подачу электроэнергии.",
        source_text="В Бердянске восстановили подачу электроэнергии.",
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
        headline_candidates=("Электроснабжение",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=(),
        edition_anchor_terms=("Бердянск",),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup.support_id,),
                detail_support_ids=(sup.support_id,),
            ),
        )
    )

    draft_with_loop = StructuredArticleDraft(
        title="Электроснабжение в Бердянске",
        title_support_ids=(sup.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Электроснабжение в Бердянске", cited_support_ids=(sup.support_id,)
            ),
        ),
        lead="В Бердянске восстановили подачу электроэнергии.",
        lead_support_ids=(sup.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В Бердянске восстановили подачу электроэнергии",
                cited_support_ids=(sup.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Электроснабжение",
                heading_support_ids=(sup.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text="В Бердянске восстановили подачу электроэнергии. В Бердянске восстановили подачу электроэнергии. В Бердянске восстановили подачу электроэнергии.",
                        cited_support_ids=(sup.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="В Бердянске восстановили подачу электроэнергии",
                                cited_support_ids=(sup.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=30,
    )

    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_min_sections=1,
        article_min_words=5,
        article_allow_deterministic_fallback=False,
    )

    result = await finalizer.finalize(
        writer_draft=draft_with_loop,
        writer_error=None,
        writer_attempt_id=1,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
    )

    assert result.writer_status == "passed"
    para_text = result.draft.sections[0].paragraphs[0].text
    # Must appear only once!
    assert para_text.count("В Бердянске восстановили подачу электроэнергии.") == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_deduplicates_cross_section_paragraphs(
    quality_gate_clear: None,
) -> None:
    from src.publication.article_models import ArticleClaimAtom

    sup1 = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="В Бердянске восстановили подачу электроэнергии.",
        source_text="В Бердянске восстановили подачу электроэнергии.",
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
    sup2 = ArticleSupport(
        support_id="story:2:evidence:0:frag:201",
        text="На проспекте Труда в общежитии газа нет совсем.",
        source_text="На проспекте Труда в общежитии газа нет совсем.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="established_fact",
        story_id="story:2",
    )
    sup3 = ArticleSupport(
        support_id="story:1:evidence:1:frag:102",
        text="Специалисты проверили состояние городских сетей.",
        source_text="Специалисты проверили состояние городских сетей.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:3",),
        fragment_ids=(3,),
        source_item_ids=(3,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="established_fact",
        story_id="story:1",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Электроснабжение", "Газоснабжение"),
        support_index=(sup1, sup2, sup3),
        support_by_id={sup1.support_id: sup1, sup2.support_id: sup2, sup3.support_id: sup3},
        recurring_topics=(),
        edition_anchor_terms=("Бердянск",),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup1.support_id, sup3.support_id),
                detail_support_ids=(sup1.support_id, sup3.support_id),
            ),
            ArticleStoryCoverage(
                story_id="story:2",
                topic="Газоснабжение",
                rank=2,
                prominence="WEAVE",
                support_ids=(sup2.support_id,),
                detail_support_ids=(sup2.support_id,),
            ),
        )
    )

    draft_with_dup_para = StructuredArticleDraft(
        title="Городские события в Бердянске",
        title_support_ids=(sup1.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Городские события в Бердянске", cited_support_ids=(sup1.support_id,)
            ),
        ),
        lead="В Бердянске восстановили подачу электроэнергии.",
        lead_support_ids=(sup1.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В Бердянске восстановили подачу электроэнергии",
                cited_support_ids=(sup1.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Электроснабжение и газ",
                heading_support_ids=(sup1.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text="В Бердянске восстановили подачу электроэнергии.",
                        cited_support_ids=(sup1.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="В Бердянске восстановили подачу электроэнергии",
                                cited_support_ids=(sup1.support_id,),
                            ),
                        ),
                    ),
                    ArticleParagraph(
                        text="На проспекте Труда в общежитии газа нет совсем.",
                        cited_support_ids=(sup2.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="На проспекте Труда в общежитии газа нет совсем",
                                cited_support_ids=(sup2.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
            ArticleSection(
                heading="Бытовые вопросы",
                heading_support_ids=(sup2.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text="На проспекте Труда в общежитии газа нет совсем.",
                        cited_support_ids=(sup2.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="На проспекте Труда в общежитии газа нет совсем",
                                cited_support_ids=(sup2.support_id,),
                            ),
                        ),
                    ),
                    ArticleParagraph(
                        text="Специалисты проверили состояние городских сетей.",
                        cited_support_ids=(sup3.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Специалисты проверили состояние городских сетей",
                                cited_support_ids=(sup3.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=50,
    )

    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_min_sections=2,
        article_min_words=5,
        article_allow_deterministic_fallback=False,
    )

    result = await finalizer.finalize(
        writer_draft=draft_with_dup_para,
        writer_error=None,
        writer_attempt_id=1,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
    )

    assert result.writer_status == "passed"
    rendered = result.draft.render_markdown()
    # "газа нет совсем" was present in both sections; now it must appear only once!
    assert rendered.count("На проспекте Труда в общежитии газа нет совсем.") == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_finalizer_repairs_phantom_heading(quality_gate_clear: None) -> None:
    from src.publication.article_models import ArticleClaimAtom

    sup1 = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="В Бердянске восстановили подачу электроэнергии.",
        source_text="В Бердянске восстановили подачу электроэнергии.",
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
    sup2 = ArticleSupport(
        support_id="story:1:evidence:1:frag:102",
        text="Ремонтные бригады завершили наладку оборудования.",
        source_text="Ремонтные бригады завершили наладку оборудования.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="established_fact",
        story_id="story:1",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Электроснабжение",),
        support_index=(sup1, sup2),
        support_by_id={sup1.support_id: sup1, sup2.support_id: sup2},
        recurring_topics=(),
        edition_anchor_terms=("Бердянск",),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:1",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(sup1.support_id, sup2.support_id),
                detail_support_ids=(sup1.support_id, sup2.support_id),
            ),
        )
    )

    # Heading promises phantom topics after colon: "операторы, мобильный интернет и поиск кошки"
    draft_phantom_heading = StructuredArticleDraft(
        title="Электроснабжение в Бердянске",
        title_support_ids=(sup1.support_id,),
        title_claims=(
            ArticleClaimAtom(
                text="Электроснабжение в Бердянске", cited_support_ids=(sup1.support_id,)
            ),
        ),
        lead="В Бердянске восстановили подачу электроэнергии.",
        lead_support_ids=(sup1.support_id,),
        lead_claims=(
            ArticleClaimAtom(
                text="В Бердянске восстановили подачу электроэнергии",
                cited_support_ids=(sup1.support_id,),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Связь и сервисы: операторы, мобильный интернет и поиск кошки",
                heading_support_ids=(sup1.support_id,),
                paragraphs=(
                    ArticleParagraph(
                        text="В Бердянске восстановили подачу электроэнергии.",
                        cited_support_ids=(sup1.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="В Бердянске восстановили подачу электроэнергии",
                                cited_support_ids=(sup1.support_id,),
                            ),
                        ),
                    ),
                    ArticleParagraph(
                        text="Ремонтные бригады завершили наладку оборудования.",
                        cited_support_ids=(sup2.support_id,),
                        claims=(
                            ArticleClaimAtom(
                                text="Ремонтные бригады завершили наладку оборудования",
                                cited_support_ids=(sup2.support_id,),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=40,
    )

    finalizer = ArticleFinalizer()
    editorial_config = PublicationEditorialConfig(
        article_min_sections=1,
        article_min_words=5,
        article_allow_deterministic_fallback=False,
    )

    result = await finalizer.finalize(
        writer_draft=draft_phantom_heading,
        writer_error=None,
        writer_attempt_id=1,
        context=context,
        coverage_plan=plan,
        editorial_config=editorial_config,
    )

    assert result.writer_status == "passed"
    # Phantom subtopics trimmed from heading
    assert result.draft.sections[0].heading == "Связь и сервисы"
    assert "поиск кошки" not in result.draft.sections[0].heading
