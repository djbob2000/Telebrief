"""Knowledge-processing domain models: policies, decisions, runs, claims.

Immutable snapshots of Plan 3 schema rows (migrations/0005). Every persisted
dataclass exposes a ``from_row`` mapper whose positional order matches the
explicit SELECT column lists used by ``src/repositories/relevance.py`` and
``src/repositories/claims.py``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RelevancePolicyVersion:
    """A `relevance_policy_versions` row: per-edition prompt/config identity."""

    id: int
    edition_id: int
    version: int
    config_hash: str
    prompt_version: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> RelevancePolicyVersion:
        return cls(
            id=row[0],
            edition_id=row[1],
            version=row[2],
            config_hash=row[3],
            prompt_version=row[4],
            created_at=row[5],
        )


@dataclass(frozen=True)
class EditionRelevanceDecision:
    """An immutable `edition_relevance_decisions` row (root or post-vision child)."""

    id: int
    source_item_revision_id: int
    edition_id: int
    relevance_policy_id: int
    status: str
    confidence: float | None
    reason: str
    provider: str | None
    model: str | None
    parent_decision_id: int | None
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> EditionRelevanceDecision:
        return cls(
            id=row[0],
            source_item_revision_id=row[1],
            edition_id=row[2],
            relevance_policy_id=row[3],
            status=row[4],
            confidence=None if row[5] is None else float(row[5]),
            reason=row[6],
            provider=row[7],
            model=row[8],
            parent_decision_id=row[9],
            created_at=row[10],
        )


@dataclass(frozen=True)
class VisionPolicyVersion:
    """A `vision_policy_versions` row: off / relevance_only / full mode."""

    id: int
    edition_id: int
    version: int
    mode: str
    config_hash: str
    prompt_version: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> VisionPolicyVersion:
        return cls(
            id=row[0],
            edition_id=row[1],
            version=row[2],
            mode=row[3],
            config_hash=row[4],
            prompt_version=row[5],
            created_at=row[6],
        )


@dataclass(frozen=True)
class VisionAnalysisRun:
    """A `vision_analysis_runs` row; status transitions are the documented
    mutable exception (running -> succeeded|failed|unavailable)."""

    id: int
    source_item_revision_id: int
    edition_id: int
    relevance_decision_id: int | None
    policy_id: int
    started_at: dt.datetime
    completed_at: dt.datetime | None
    status: str
    error_kind: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> VisionAnalysisRun:
        return cls(
            id=row[0],
            source_item_revision_id=row[1],
            edition_id=row[2],
            relevance_decision_id=row[3],
            policy_id=row[4],
            started_at=row[5],
            completed_at=row[6],
            status=row[7],
            error_kind=row[8],
            metadata=row[9],
        )


@dataclass(frozen=True)
class VisionObservation:
    """A derived `vision_observations` provenance artifact (append-only)."""

    id: int
    vision_run_id: int
    source_asset_id: int | None
    source_item_revision_id: int
    kind: str
    text: str | None
    metadata: dict[str, Any]
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> VisionObservation:
        return cls(
            id=row[0],
            vision_run_id=row[1],
            source_asset_id=row[2],
            source_item_revision_id=row[3],
            kind=row[4],
            text=row[5],
            metadata=row[6],
            created_at=row[7],
        )


@dataclass(frozen=True)
class ClaimExtractionPolicyVersion:
    """A `claim_extraction_policy_versions` row."""

    id: int
    edition_id: int
    version: int
    config_hash: str
    prompt_version: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> ClaimExtractionPolicyVersion:
        return cls(
            id=row[0],
            edition_id=row[1],
            version=row[2],
            config_hash=row[3],
            prompt_version=row[4],
            created_at=row[5],
        )


@dataclass(frozen=True)
class ClaimExtractionRun:
    """A `claim_extraction_runs` row; at most one per semantic key ever
    reaches status 'succeeded' (partial unique index uq_claim_extraction_success)."""

    id: int
    source_item_revision_id: int
    edition_id: int
    extraction_policy_id: int
    relevance_decision_id: int
    started_at: dt.datetime
    completed_at: dt.datetime | None
    status: str
    error_kind: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> ClaimExtractionRun:
        return cls(
            id=row[0],
            source_item_revision_id=row[1],
            edition_id=row[2],
            extraction_policy_id=row[3],
            relevance_decision_id=row[4],
            started_at=row[5],
            completed_at=row[6],
            status=row[7],
            error_kind=row[8],
            metadata=row[9],
        )


@dataclass(frozen=True)
class ProcessingAttempt:
    """A `processing_attempts` audit row keyed by
    (stage, semantic_run_id, attempt_no); never a queue."""

    stage: str
    semantic_run_id: int
    attempt_no: int
    provider: str | None
    model: str | None
    started_at: dt.datetime
    completed_at: dt.datetime | None
    status: str
    error_kind: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> ProcessingAttempt:
        return cls(
            stage=row[0],
            semantic_run_id=row[1],
            attempt_no=row[2],
            provider=row[3],
            model=row[4],
            started_at=row[5],
            completed_at=row[6],
            status=row[7],
            error_kind=row[8],
            metadata=row[9],
        )


@dataclass(frozen=True)
class NewClaim:
    """Input for creating a claim; run provenance is attached by the repository."""

    assertion_text: str
    normalized_assertion: str
    event_time_start: dt.datetime | None = None
    event_time_end: dt.datetime | None = None
    event_time_precision: str | None = None
    event_time_confidence: float | None = None
    event_time_original_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Claim:
    """An immutable `claims` row: source-bound assertion with spec §16 temporal model."""

    id: int
    claim_extraction_run_id: int
    source_item_revision_id: int
    edition_id: int
    assertion_text: str
    normalized_assertion: str
    event_time_start: dt.datetime | None
    event_time_end: dt.datetime | None
    event_time_precision: str | None
    event_time_confidence: float | None
    event_time_original_text: str | None
    metadata: dict[str, Any]
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> Claim:
        return cls(
            id=row[0],
            claim_extraction_run_id=row[1],
            source_item_revision_id=row[2],
            edition_id=row[3],
            assertion_text=row[4],
            normalized_assertion=row[5],
            event_time_start=row[6],
            event_time_end=row[7],
            event_time_precision=row[8],
            event_time_confidence=None if row[9] is None else float(row[9]),
            event_time_original_text=row[10],
            metadata=row[11],
            created_at=row[12],
        )


@dataclass(frozen=True)
class ClaimRelation:
    """An immutable `claim_relations` row: CORRECTS | SUPERSEDES | RETRACTS."""

    id: int
    from_claim_id: int
    to_claim_id: int
    relation_type: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> ClaimRelation:
        return cls(
            id=row[0],
            from_claim_id=row[1],
            to_claim_id=row[2],
            relation_type=row[3],
            created_at=row[4],
        )


@dataclass(frozen=True)
class ClaimStateEvent:
    """An append-only `claim_state_events` row; operational state must stay
    reconstructable from these plus immutable relations."""

    id: int
    claim_id: int
    type: str
    observed_at: dt.datetime
    reason: str | None
    evidence: dict[str, Any]
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> ClaimStateEvent:
        return cls(
            id=row[0],
            claim_id=row[1],
            type=row[2],
            observed_at=row[3],
            reason=row[4],
            evidence=row[5],
            created_at=row[6],
        )
