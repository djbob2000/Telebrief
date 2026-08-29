from pathlib import Path

import pytest

from scripts.evaluate_event_pipeline import EventEvaluationReport, evaluate_fixture


@pytest.mark.unit
def test_event_benchmark_report_has_quality_and_cost_fields():
    fixture = Path("tests/fixtures/event_first_day.json")
    report = evaluate_fixture(fixture, legacy_baseline_cost_usd=4.0)

    assert isinstance(report, EventEvaluationReport)
    assert report.raw_revision_count > 0
    assert report.gold_event_recall >= 0.0
    assert report.source_provenance_rate >= 0.0
    assert report.knowledge_llm_calls_per_1000 >= 0.0
    assert report.legacy_baseline_cost_usd == 4.0
    assert report.event_processing_cost_usd is None
