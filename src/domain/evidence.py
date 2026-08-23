"""Evidence and verification domain models (Plan 3 Task 9).

Immutable snapshots of Plan 3 evidence assessment runs, clusters, members,
and lightweight verification records.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


def hash_sorted_ids(claim_ids: Sequence[int]) -> str:
    """Compute a deterministic SHA-256 hash over an order-independent set of claim ids."""
    sorted_unique = sorted(set(claim_ids))
    payload = ",".join(str(cid) for cid in sorted_unique).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class EvidenceAssessmentPolicyVersion:
    """A `evidence_assessment_policy_versions` row."""

    id: int
    edition_id: int
    version: int
    config_hash: str
    prompt_version: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> EvidenceAssessmentPolicyVersion:
        return cls(
            id=row[0],
            edition_id=row[1],
            version=row[2],
            config_hash=row[3],
            prompt_version=row[4],
            created_at=row[5],
        )


@dataclass(frozen=True)
class EvidenceAssessmentRun:
    """An `evidence_assessment_runs` row."""

    id: int
    story_id: int
    story_revision_id: int
    edition_id: int
    policy_id: int
    input_hash: str
    started_at: dt.datetime
    completed_at: dt.datetime | None
    status: str
    error_kind: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> EvidenceAssessmentRun:
        return cls(
            id=row[0],
            story_id=row[1],
            story_revision_id=row[2],
            edition_id=row[3],
            policy_id=row[4],
            input_hash=row[5],
            started_at=row[6],
            completed_at=row[7],
            status=row[8],
            error_kind=row[9],
            metadata=row[10] if isinstance(row[10], dict) else {},
        )


@dataclass(frozen=True)
class ClusterMemberProposal:
    """Proposal for a claim's stance in a cluster."""

    claim_id: int
    stance: str  # 'SUPPORTS', 'CONTRADICTS', 'UNCERTAIN', 'CONTEXTUAL'


@dataclass(frozen=True)
class EvidenceClusterProposal:
    """Proposal for inserting an evidence cluster with its members."""

    label: str | None = None
    summary: str | None = None
    supporting_claims: int = 0
    contradicting_claims: int = 0
    unique_sources: int = 0
    estimated_independent_source_groups: int = 0
    supersedes_cluster_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    members: list[ClusterMemberProposal] = field(default_factory=list)


@dataclass(frozen=True)
class EvidenceCluster:
    """An `evidence_clusters` row."""

    id: int
    run_id: int
    supersedes_cluster_id: int | None
    label: str | None
    summary: str | None
    supporting_claims: int
    contradicting_claims: int
    unique_sources: int
    estimated_independent_source_groups: int
    metadata: dict[str, Any]
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> EvidenceCluster:
        return cls(
            id=row[0],
            run_id=row[1],
            supersedes_cluster_id=row[2],
            label=row[3],
            summary=row[4],
            supporting_claims=row[5],
            contradicting_claims=row[6],
            unique_sources=row[7],
            estimated_independent_source_groups=row[8],
            metadata=row[9] if isinstance(row[9], dict) else {},
            created_at=row[10],
        )


@dataclass(frozen=True)
class EvidenceClusterMember:
    """An `evidence_cluster_members` row."""

    cluster_id: int
    claim_id: int
    stance: str

    @classmethod
    def from_row(cls, row: Any) -> EvidenceClusterMember:
        return cls(
            cluster_id=row[0],
            claim_id=row[1],
            stance=row[2],
        )


@dataclass(frozen=True)
class VerificationPolicyVersion:
    """A `verification_policy_versions` row."""

    id: int
    edition_id: int
    version: int
    config_hash: str
    prompt_version: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> VerificationPolicyVersion:
        return cls(
            id=row[0],
            edition_id=row[1],
            version=row[2],
            config_hash=row[3],
            prompt_version=row[4],
            created_at=row[5],
        )


@dataclass(frozen=True)
class VerificationAssessment:
    """A `verification_assessments` row.

    Soft descriptive evidence metadata only (spec §22).
    MUST NOT contain publication gates (publication_blocking, eligible, allowed, publishable).
    """

    id: int
    evidence_assessment_run_id: int
    verification_policy_id: int
    cluster_id: int | None
    state: str  # 'reported', 'corroborated', 'officially_supported', 'disputed', 'retracted'
    risk_level: str | None  # 'low', 'medium', 'high'
    reason: str | None
    provider: str | None
    model: str | None
    metadata: dict[str, Any]
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> VerificationAssessment:
        return cls(
            id=row[0],
            evidence_assessment_run_id=row[1],
            verification_policy_id=row[2],
            cluster_id=row[3],
            state=row[4],
            risk_level=row[5],
            reason=row[6],
            provider=row[7],
            model=row[8],
            metadata=row[9] if isinstance(row[9], dict) else {},
            created_at=row[10],
        )
