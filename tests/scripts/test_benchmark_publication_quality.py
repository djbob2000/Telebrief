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
