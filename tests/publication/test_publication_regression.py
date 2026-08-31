"""Pure tests for offline legacy regression and loss attribution engine."""

from __future__ import annotations

import pytest

from scripts.publication_regression import (
    AllowedLegacyLossReason,
    CaseStageState,
    LegacyCoverageCase,
    LegacyCoverageUnit,
    LegacyLoss,
    LegacySourceIdentity,
    attribute_loss,
    evaluate_case,
    source_fingerprint,
)


def test_loss_attribution_stops_at_first_missing_stage():
    unit = LegacyCoverageUnit(id="free_charging_gagarina_1", description="Free charging")

    assert attribute_loss(unit, CaseStageState(source=False)) == LegacyLoss.SOURCE_CORPUS_LOSS
    assert (
        attribute_loss(unit, CaseStageState(source=True, evidence=False))
        == LegacyLoss.KNOWLEDGE_LAYER_LOSS
    )
    assert (
        attribute_loss(unit, CaseStageState(source=True, evidence=True, candidate=False))
        == LegacyLoss.ELIGIBILITY_LOSS
    )
    assert (
        attribute_loss(
            unit,
            CaseStageState(source=True, evidence=True, candidate=True, sealed=False),
        )
        == LegacyLoss.SELECTION_LOSS
    )
    assert (
        attribute_loss(
            unit,
            CaseStageState(
                source=True,
                evidence=True,
                candidate=True,
                sealed=True,
                plan=False,
            ),
        )
        == LegacyLoss.COVERAGE_PLAN_LOSS
    )
    assert (
        attribute_loss(
            unit,
            CaseStageState(
                source=True,
                evidence=True,
                candidate=True,
                sealed=True,
                plan=True,
                final_trace=False,
            ),
        )
        == LegacyLoss.PUBLICATION_COVERAGE_LOSS
    )
    assert attribute_loss(unit, CaseStageState.all_present()) == LegacyLoss.COVERED


def test_allowed_loss_reason_enum_validation():
    # Valid reasons
    for reason in (
        "HARD_EXCLUSION",
        "TRUE_DUPLICATE",
        "UNSUPPORTED_LEGACY_INFERENCE",
    ):
        unit = LegacyCoverageUnit.from_dict(
            {"id": "u1", "description": "d", "allowed_loss_reason": reason}
        )
        assert unit.allowed_loss_reason == AllowedLegacyLossReason(reason)

    # Invalid reasons rejected
    for invalid in ("WRITER_OMITTED", "LOW_PRIORITY", "TOO_MINOR", "UNKNOWN"):
        with pytest.raises(ValueError, match="Invalid allowed_loss_reason"):
            LegacyCoverageUnit.from_dict(
                {"id": "u1", "description": "d", "allowed_loss_reason": invalid}
            )


def test_source_fingerprint_deterministic():
    text1 = "На ул. Гагарина, 1 открыли пункт подзарядки"
    text2 = "  на ул.  Гагарина, 1   открыли пункт подзарядки  \n"
    assert source_fingerprint(text1) == source_fingerprint(text2)


def test_trace_matching_and_offline_gate_metrics():
    fp1 = source_fingerprint("Пункт подзарядки на Гагарина 1")
    fp2 = source_fingerprint("Движение автобуса 4 восстановлено")
    fp3 = source_fingerprint("Реклама такси круглосуточно")

    case = LegacyCoverageCase(
        id="berdyansk_case_1",
        legacy_commit="abc1234",
        window_start="2026-08-30T00:00:00Z",
        window_end="2026-08-31T00:00:00Z",
        coverage_units=(
            LegacyCoverageUnit(
                id="unit_charging",
                description="Charging station",
                acceptable_sources=(
                    LegacySourceIdentity(
                        fixture_fragment_id="frag_1",
                        source_ref="ref_1",
                        source_fingerprint=fp1,
                    ),
                ),
                required_microdetails=("ул. Гагарина, 1", "бесплатно"),
            ),
            LegacyCoverageUnit(
                id="missing_binding_unit",
                description="Bus line 4",
                acceptable_sources=(
                    LegacySourceIdentity(
                        fixture_fragment_id="frag_2",
                        source_ref="ref_2",
                        source_fingerprint=fp2,
                    ),
                ),
                required_microdetails=("маршрут №4",),
            ),
            LegacyCoverageUnit(
                id="excluded_ad_unit",
                description="Taxi ad",
                acceptable_sources=(
                    LegacySourceIdentity(
                        fixture_fragment_id="frag_3",
                        source_ref="ref_3",
                        source_fingerprint=fp3,
                    ),
                ),
                allowed_loss_reason=AllowedLegacyLossReason.HARD_EXCLUSION,
            ),
        ),
    )

    # 1. First scenario: missing_binding_unit drops at PUBLICATION_COVERAGE_LOSS
    exported_case_partial = {
        "source_fingerprints": [fp1, fp2, fp3],
        "source_refs": ["ref_1", "ref_2", "ref_3"],
        "source_fragment_ids": ["frag_1", "frag_2", "frag_3"],
        "evidence_fingerprints": [fp1, fp2],
        "evidence_refs": ["ref_1", "ref_2"],
        "evidence_fragment_ids": ["frag_1", "frag_2"],
        "candidate_refs": ["ref_1", "ref_2"],
        "candidate_fragment_ids": ["frag_1", "frag_2"],
        "sealed_refs": ["ref_1", "ref_2"],
        "sealed_fragment_ids": ["frag_1", "frag_2"],
        "plan_refs": ["ref_1", "ref_2"],
        "plan_fragment_ids": ["frag_1", "frag_2"],
        # only unit_charging in final trace
        "final_trace_supports": ["ref_1"],
        "final_trace_refs": ["ref_1"],
        "final_trace_fragment_ids": ["frag_1"],
        "retained_microdetails_by_unit": {
            "unit_charging": ["ул. Гагарина, 1", "бесплатно"],
        },
    }

    report_partial = evaluate_case(case, exported_case_partial)
    assert report_partial.legacy_floor_coverage == 0.5
    assert report_partial.legacy_microdetail_retention < 1.0
    assert report_partial.regression_unit_ids == ("missing_binding_unit",)

    # 2. Second scenario: both binding units covered with all microdetails
    exported_case_full = {
        **exported_case_partial,
        "final_trace_supports": ["ref_1", "ref_2"],
        "final_trace_refs": ["ref_1", "ref_2"],
        "final_trace_fragment_ids": ["frag_1", "frag_2"],
        "retained_microdetails_by_unit": {
            "unit_charging": ["ул. Гагарина, 1", "бесплатно"],
            "missing_binding_unit": ["маршрут №4"],
        },
    }

    report_full = evaluate_case(case, exported_case_full)
    assert report_full.legacy_floor_coverage == 1.0
    assert report_full.legacy_microdetail_retention == 1.0
    assert report_full.regression_unit_ids == ()
