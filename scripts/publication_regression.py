"""Offline legacy semantic inventory, loss attribution, and regression testing suite."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class LegacyLoss(str, Enum):
    COVERED = "COVERED"
    SOURCE_CORPUS_LOSS = "SOURCE_CORPUS_LOSS"
    KNOWLEDGE_LAYER_LOSS = "KNOWLEDGE_LAYER_LOSS"
    ELIGIBILITY_LOSS = "ELIGIBILITY_LOSS"
    SELECTION_LOSS = "SELECTION_LOSS"
    COVERAGE_PLAN_LOSS = "COVERAGE_PLAN_LOSS"
    PUBLICATION_COVERAGE_LOSS = "PUBLICATION_COVERAGE_LOSS"


class AllowedLegacyLossReason(str, Enum):
    HARD_EXCLUSION = "HARD_EXCLUSION"
    TRUE_DUPLICATE = "TRUE_DUPLICATE"
    UNSUPPORTED_LEGACY_INFERENCE = "UNSUPPORTED_LEGACY_INFERENCE"


def source_fingerprint(text: str) -> str:
    """Deterministic SHA-256 fingerprint of normalized source text."""
    normalized = " ".join(text.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LegacySourceIdentity:
    fixture_fragment_id: str | None = None
    source_ref: str | None = None
    source_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {}
        if self.fixture_fragment_id:
            res["fixture_fragment_id"] = self.fixture_fragment_id
        if self.source_ref:
            res["source_ref"] = self.source_ref
        if self.source_fingerprint:
            res["source_fingerprint"] = self.source_fingerprint
        return res

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LegacySourceIdentity:
        return cls(
            fixture_fragment_id=data.get("fixture_fragment_id"),
            source_ref=data.get("source_ref"),
            source_fingerprint=data.get("source_fingerprint"),
        )


@dataclass(frozen=True)
class LegacyCoverageUnit:
    id: str
    description: str
    acceptable_sources: tuple[LegacySourceIdentity, ...] = ()
    required_microdetails: tuple[str, ...] = ()
    allowed_loss_reason: AllowedLegacyLossReason | None = None

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "id": self.id,
            "description": self.description,
            "acceptable_sources": [s.to_dict() for s in self.acceptable_sources],
            "required_microdetails": list(self.required_microdetails),
        }
        if self.allowed_loss_reason is not None:
            res["allowed_loss_reason"] = self.allowed_loss_reason.value
        return res

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LegacyCoverageUnit:
        unit_id = data.get("id")
        if not unit_id or not isinstance(unit_id, str):
            raise ValueError("LegacyCoverageUnit requires non-empty 'id'")
        desc = data.get("description", "")
        sources = tuple(
            LegacySourceIdentity.from_dict(s) for s in data.get("acceptable_sources", ())
        )
        microdetails = tuple(data.get("required_microdetails", ()))
        loss_reason = data.get("allowed_loss_reason")
        allowed_reason: AllowedLegacyLossReason | None = None
        if loss_reason is not None:
            try:
                allowed_reason = AllowedLegacyLossReason(loss_reason)
            except ValueError as err:
                raise ValueError(
                    f"Invalid allowed_loss_reason '{loss_reason}'. Allowed values: {[e.value for e in AllowedLegacyLossReason]}"
                ) from err
        return cls(
            id=unit_id,
            description=desc,
            acceptable_sources=sources,
            required_microdetails=microdetails,
            allowed_loss_reason=allowed_reason,
        )


@dataclass(frozen=True)
class LegacyCoverageCase:
    id: str
    legacy_commit: str
    window_start: str
    window_end: str
    coverage_units: tuple[LegacyCoverageUnit, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "legacy_commit": self.legacy_commit,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "coverage_units": [u.to_dict() for u in self.coverage_units],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LegacyCoverageCase:
        case_id = data.get("id")
        if not case_id or not isinstance(case_id, str):
            raise ValueError("LegacyCoverageCase requires non-empty 'id'")
        return cls(
            id=case_id,
            legacy_commit=data.get("legacy_commit", ""),
            window_start=data.get("window_start", ""),
            window_end=data.get("window_end", ""),
            coverage_units=tuple(
                LegacyCoverageUnit.from_dict(u) for u in data.get("coverage_units", ())
            ),
        )

    @classmethod
    def load_json(cls, path: str | Path) -> LegacyCoverageCase:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


@dataclass(frozen=True)
class CaseStageState:
    source: bool = False
    evidence: bool = False
    candidate: bool = False
    sealed: bool = False
    plan: bool = False
    final_trace: bool = False

    @classmethod
    def all_present(cls) -> CaseStageState:
        return cls(
            source=True,
            evidence=True,
            candidate=True,
            sealed=True,
            plan=True,
            final_trace=True,
        )


def attribute_loss(unit: LegacyCoverageUnit, stage_state: CaseStageState) -> LegacyLoss:
    """Attribute coverage loss to the exact first missing pipeline stage."""
    if not stage_state.source:
        return LegacyLoss.SOURCE_CORPUS_LOSS
    if not stage_state.evidence:
        return LegacyLoss.KNOWLEDGE_LAYER_LOSS
    if not stage_state.candidate:
        return LegacyLoss.ELIGIBILITY_LOSS
    if not stage_state.sealed:
        return LegacyLoss.SELECTION_LOSS
    if not stage_state.plan:
        return LegacyLoss.COVERAGE_PLAN_LOSS
    if not stage_state.final_trace:
        return LegacyLoss.PUBLICATION_COVERAGE_LOSS
    return LegacyLoss.COVERED


@dataclass(frozen=True)
class LegacyRegressionUnitResult:
    unit_id: str
    loss: LegacyLoss
    allowed_loss_reason: AllowedLegacyLossReason | None
    retained_microdetails: tuple[str, ...]
    missing_microdetails: tuple[str, ...]


@dataclass(frozen=True)
class LegacyRegressionReport:
    units: tuple[LegacyRegressionUnitResult, ...]
    legacy_floor_coverage: float
    legacy_microdetail_retention: float
    regression_unit_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "legacy_floor_coverage": self.legacy_floor_coverage,
            "legacy_microdetail_retention": self.legacy_microdetail_retention,
            "regression_unit_ids": list(self.regression_unit_ids),
            "units": [
                {
                    "unit_id": u.unit_id,
                    "loss": u.loss.value,
                    "allowed_loss_reason": u.allowed_loss_reason.value
                    if u.allowed_loss_reason
                    else None,
                    "retained_microdetails": list(u.retained_microdetails),
                    "missing_microdetails": list(u.missing_microdetails),
                }
                for u in self.units
            ],
        }


def evaluate_case(
    case: LegacyCoverageCase,
    exported_case: dict[str, Any],
) -> LegacyRegressionReport:
    """Evaluate an offline legacy floor case against an exported case snapshot and traces."""
    # Exported case index structures:
    # sources: set of fingerprints and refs present in the source snapshot
    source_fingerprints: set[str] = set(exported_case.get("source_fingerprints", ()))
    source_refs: set[str] = set(exported_case.get("source_refs", ()))
    source_frag_ids: set[str] = set(exported_case.get("source_fragment_ids", ()))

    # evidence: set of refs / fragment_ids / fingerprints present in active knowledge
    evidence_fingerprints: set[str] = set(exported_case.get("evidence_fingerprints", ()))
    evidence_refs: set[str] = set(exported_case.get("evidence_refs", ()))
    evidence_frag_ids: set[str] = set(exported_case.get("evidence_fragment_ids", ()))

    # candidate: set of refs / fragment_ids in candidate stories
    candidate_refs: set[str] = set(exported_case.get("candidate_refs", ()))
    candidate_frag_ids: set[str] = set(exported_case.get("candidate_fragment_ids", ()))

    # sealed: set of refs / fragment_ids in sealed selection
    sealed_refs: set[str] = set(exported_case.get("sealed_refs", ()))
    sealed_frag_ids: set[str] = set(exported_case.get("sealed_fragment_ids", ()))

    # plan: set of refs / fragment_ids in coverage plan
    plan_refs: set[str] = set(exported_case.get("plan_refs", ()))
    plan_frag_ids: set[str] = set(exported_case.get("plan_fragment_ids", ()))

    # final_trace: set of cited supports and covered refs from final trace
    final_trace_supports: set[str] = set(exported_case.get("final_trace_supports", ()))
    final_trace_refs: set[str] = set(exported_case.get("final_trace_refs", ()))
    final_trace_frag_ids: set[str] = set(exported_case.get("final_trace_fragment_ids", ()))

    # retained microdetails by unit ID (or globally verified from canonical trace metadata)
    retained_microdetails_map: dict[str, list[str]] = exported_case.get(
        "retained_microdetails_by_unit", {}
    )

    unit_results: list[LegacyRegressionUnitResult] = []

    for unit in case.coverage_units:
        # Check presence at each stage based on acceptable sources
        has_source = False
        has_evidence = False
        has_candidate = False
        has_sealed = False
        has_plan = False
        has_final = False

        for src in unit.acceptable_sources:
            # 1. Source corpus
            if (
                (src.source_fingerprint and src.source_fingerprint in source_fingerprints)
                or (src.source_ref and src.source_ref in source_refs)
                or (src.fixture_fragment_id and src.fixture_fragment_id in source_frag_ids)
            ):
                has_source = True

            # 2. Evidence knowledge
            if (
                (src.source_fingerprint and src.source_fingerprint in evidence_fingerprints)
                or (src.source_ref and src.source_ref in evidence_refs)
                or (src.fixture_fragment_id and src.fixture_fragment_id in evidence_frag_ids)
            ):
                has_evidence = True

            # 3. Candidate
            if (src.source_ref and src.source_ref in candidate_refs) or (
                src.fixture_fragment_id and src.fixture_fragment_id in candidate_frag_ids
            ):
                has_candidate = True

            # 4. Sealed
            if (src.source_ref and src.source_ref in sealed_refs) or (
                src.fixture_fragment_id and src.fixture_fragment_id in sealed_frag_ids
            ):
                has_sealed = True

            # 5. Coverage plan
            if (src.source_ref and src.source_ref in plan_refs) or (
                src.fixture_fragment_id and src.fixture_fragment_id in plan_frag_ids
            ):
                has_plan = True

            # 6. Final trace
            if (
                (src.source_ref and src.source_ref in final_trace_supports)
                or (src.source_ref and src.source_ref in final_trace_refs)
                or (src.fixture_fragment_id and src.fixture_fragment_id in final_trace_frag_ids)
            ):
                has_final = True

        stage_state = CaseStageState(
            source=has_source,
            evidence=has_evidence,
            candidate=has_candidate,
            sealed=has_sealed,
            plan=has_plan,
            final_trace=has_final,
        )
        loss = attribute_loss(unit, stage_state)

        retained_list = retained_microdetails_map.get(unit.id, [])
        retained = tuple(m for m in unit.required_microdetails if m in retained_list or has_final)
        missing = tuple(m for m in unit.required_microdetails if m not in retained)

        unit_results.append(
            LegacyRegressionUnitResult(
                unit_id=unit.id,
                loss=loss,
                allowed_loss_reason=unit.allowed_loss_reason,
                retained_microdetails=retained,
                missing_microdetails=missing,
            )
        )

    binding_results = [r for r in unit_results if r.allowed_loss_reason is None]
    covered_binding = [r for r in binding_results if r.loss == LegacyLoss.COVERED]
    legacy_floor_coverage = (
        len(covered_binding) / len(binding_results) if binding_results else 1.0
    )

    total_required_microdetails = sum(
        len(unit.required_microdetails)
        for unit in case.coverage_units
        if unit.allowed_loss_reason is None
    )
    retained_required_microdetails = sum(
        len(result.retained_microdetails)
        for result in unit_results
        if result.allowed_loss_reason is None
    )
    legacy_microdetail_retention = (
        retained_required_microdetails / total_required_microdetails
        if total_required_microdetails
        else 1.0
    )

    regression_unit_ids = tuple(
        r.unit_id
        for r in binding_results
        if r.loss != LegacyLoss.COVERED or len(r.missing_microdetails) > 0
    )

    return LegacyRegressionReport(
        units=tuple(unit_results),
        legacy_floor_coverage=legacy_floor_coverage,
        legacy_microdetail_retention=legacy_microdetail_retention,
        regression_unit_ids=regression_unit_ids,
    )
