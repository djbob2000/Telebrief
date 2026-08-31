"""Tests for benchmark_publication_quality metrics and validation gates."""

from __future__ import annotations

from scripts.benchmark_publication_quality import (
    BenchmarkAttemptRecord,
    BenchmarkRunRecord,
    calculate_benchmark_metrics,
    validate_benchmark_gates,
)


def test_benchmark_metrics_with_success_and_rejection():
    runs = [
        BenchmarkRunRecord(
            run_id=1,
            publication_type="article",
            status="succeeded",
            error_kind=None,
            publication_id=101,
            attempts=[
                BenchmarkAttemptRecord(
                    kind="writer",
                    status="succeeded",
                    error_kind=None,
                    metadata={"status": "writer_success"},
                )
            ],
        ),
        BenchmarkRunRecord(
            run_id=2,
            publication_type="article",
            status="failed",
            error_kind="article_validation_rejected",
            publication_id=None,
            attempts=[
                BenchmarkAttemptRecord(
                    kind="writer",
                    status="failed",
                    error_kind="article_validation_rejected",
                    metadata={"status": "rejected", "reason": "validation_failed"},
                )
            ],
        ),
    ]

    metrics = calculate_benchmark_metrics(runs)

    assert metrics["article_writer_attempts"] == 2
    assert metrics["article_writer_successes"] == 1
    assert metrics["article_rejections"] == 1
    assert metrics["article_publications"] == 1
    assert metrics["article_fallback_content_attempts"] == 0
    assert metrics["article_writer_success_rate"] == 0.5

    violations = validate_benchmark_gates(metrics)
    assert violations == []


def test_benchmark_gates_fail_on_article_fallback_or_excessive_calls():
    # Run with fallback attempt and no successful writer attempt
    runs = [
        BenchmarkRunRecord(
            run_id=1,
            publication_type="article",
            status="succeeded",
            error_kind=None,
            publication_id=101,
            attempts=[
                BenchmarkAttemptRecord(
                    kind="writer",
                    status="failed",
                    error_kind="ValidationFailed",
                    metadata={},
                ),
                BenchmarkAttemptRecord(
                    kind="story_renderer_fallback",
                    status="succeeded",
                    error_kind=None,
                    metadata={},
                ),
            ],
        ),
    ]

    metrics = calculate_benchmark_metrics(runs)
    assert metrics["article_fallback_content_attempts"] == 1

    violations = validate_benchmark_gates(metrics)
    assert any("fallback" in v.lower() for v in violations)
    assert any("writer" in v.lower() or "publication" in v.lower() for v in violations)


def test_evaluate_digest_short_read_quality_redundancy_diagnostics():
    from scripts.benchmark_publication_quality import evaluate_digest_short_read_quality
    from src.publication.digest_narrative import (
        DigestEditorialItemDraft,
        DigestNarrativeBlockDraft,
        DigestNarrativeDraft,
    )
    from src.publication.digest_presentation import (
        CitySituationPresentationGroup,
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentationHint,
    )

    sit_plan = CitySituationPresentationPlan(
        groups=(
            CitySituationPresentationGroup(
                group_id="situation:water:avail",
                group_kind="subject_status",
                subject_key="water",
                subject_label="Водоснабжение",
                state="UNAVAILABLE",
                source_refs=("ref-w-1",),
                detail_lines=("Центр: нет воды",),
            ),
        ),
        covered_source_refs=("ref-w-1",),
    )

    pres_plan = DigestPresentationPlan(
        city_situation=sit_plan,
        detail_story_ids=("story:elec",),
        story_hints=(
            DigestStoryPresentationHint(
                story_id="story:water",
                detail_support_ids=(),
                merge_group_id="story:water",
                detail_role="SUPPRESS",
            ),
            DigestStoryPresentationHint(
                story_id="story:elec",
                detail_support_ids=("ref-gen",),
                merge_group_id="story:elec",
                detail_role="DRILL_DOWN",
            ),
        ),
    )

    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Домовой генератор",
                        body="Жильцы запустили генератор для насоса.",
                        covered_story_ids=("story:elec",),
                        cited_support_ids=("ref-gen",),
                    ),
                ),
            ),
        )
    )

    diag = evaluate_digest_short_read_quality(
        "Digest body text",
        narrative_draft=draft,
        presentation_plan=pres_plan,
    )
    assert diag["dashboard_group_count"] == 1
    assert diag["dashboard_covered_refs_count"] == 1
    assert diag["thematic_detail_story_count"] == 1
    assert diag["thematic_suppressed_story_count"] == 1
    assert diag["drill_down_story_count"] == 1
    assert diag["redundant_thematic_items_count"] == 0
