"""Tests for publication benchmark metrics and gate validations."""

from __future__ import annotations

from scripts.benchmark_publication_quality import (
    BenchmarkAttemptRecord,
    BenchmarkRunRecord,
    calculate_benchmark_metrics,
    validate_benchmark_gates,
)


def test_full_fallback_is_quality_signal_not_correctness_failure():
    runs = [
        BenchmarkRunRecord(
            run_id=1,
            publication_type="article",
            status="succeeded",
            publication_id=10,
            attempts=[
                BenchmarkAttemptRecord(
                    kind="writer",
                    status="failed",
                    error_kind="article_writer_rejected",
                    metadata={"writer_status": "failed"},
                ),
                BenchmarkAttemptRecord(
                    kind="deterministic_fallback",
                    status="succeeded",
                    metadata={
                        "winning_kind": "event_article_deterministic_fallback",
                        "final_story_coverage": 1.0,
                        "unsupported_final_claim_count": 0,
                        "leaked_directory_payload_count": 0,
                    },
                ),
            ],
        )
    ]

    metrics = calculate_benchmark_metrics(runs)
    assert metrics["article_publications"] == 1
    assert metrics["article_full_fallbacks"] == 1
    assert metrics["article_full_fallback_rate"] == 1.0
    assert metrics["article_writer_success_rate"] == 0.0
    assert validate_benchmark_gates(metrics) == []


def test_article_hard_gates_enforced():
    # 1. Coverage < 1.0
    runs = [
        BenchmarkRunRecord(
            run_id=1,
            publication_type="article",
            status="succeeded",
            publication_id=10,
            attempts=[
                BenchmarkAttemptRecord(
                    kind="writer",
                    status="succeeded",
                    metadata={
                        "final_story_coverage": 0.95,
                        "unsupported_final_claim_count": 0,
                        "leaked_directory_payload_count": 0,
                    },
                )
            ],
        )
    ]
    metrics = calculate_benchmark_metrics(runs)
    violations = validate_benchmark_gates(metrics)
    assert any("final_story_coverage" in v for v in violations)

    # 2. Unsupported final claim count > 0
    runs[0].attempts[0].metadata["final_story_coverage"] = 1.0
    runs[0].attempts[0].metadata["unsupported_final_claim_count"] = 2
    metrics = calculate_benchmark_metrics(runs)
    violations = validate_benchmark_gates(metrics)
    assert any("unsupported_final_claim_count" in v for v in violations)

    # 3. Leaked directory payload count > 0
    runs[0].attempts[0].metadata["unsupported_final_claim_count"] = 0
    runs[0].attempts[0].metadata["leaked_directory_payload_count"] = 1
    metrics = calculate_benchmark_metrics(runs)
    violations = validate_benchmark_gates(metrics)
    assert any("leaked_directory_payload_count" in v for v in violations)

    # 4. Max writer calls per run > 1
    runs[0].attempts.append(
        BenchmarkAttemptRecord(kind="writer", status="failed", error_kind="err")
    )
    runs[0].attempts[0].metadata["leaked_directory_payload_count"] = 0
    metrics = calculate_benchmark_metrics(runs)
    violations = validate_benchmark_gates(metrics)
    assert any("max_article_writer_calls_per_run" in v for v in violations)


def test_digest_hard_gates_enforced():
    runs = [
        BenchmarkRunRecord(
            run_id=2,
            publication_type="digest_grouped",
            status="succeeded",
            publication_id=20,
            attempts=[
                BenchmarkAttemptRecord(
                    kind="digest_narrative",
                    status="succeeded",
                    metadata={
                        "final_digest_story_coverage": 0.8,
                        "planned_story_count": 5,
                    },
                )
            ],
        )
    ]
    metrics = calculate_benchmark_metrics(runs)
    violations = validate_benchmark_gates(metrics)
    assert any("final_digest_story_coverage" in v for v in violations)
