"""Domain models and data structures for frozen publications and delivery (Plan 4)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EligibilityPolicyVersion:
    """An `eligibility_policy_versions` row."""

    id: int
    edition_id: int
    version: int
    config_hash: str
    prompt_version: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> EligibilityPolicyVersion:
        return cls(
            id=row[0],
            edition_id=row[1],
            version=row[2],
            config_hash=row[3],
            prompt_version=row[4],
            created_at=row[5],
        )


@dataclass(frozen=True)
class EditorialSelectionPolicyVersion:
    """An `editorial_selection_policy_versions` row."""

    id: int
    edition_id: int
    version: int
    config_hash: str
    prompt_version: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> EditorialSelectionPolicyVersion:
        return cls(
            id=row[0],
            edition_id=row[1],
            version=row[2],
            config_hash=row[3],
            prompt_version=row[4],
            created_at=row[5],
        )


@dataclass(frozen=True)
class WriterPolicyVersion:
    """A `writer_policy_versions` row."""

    id: int
    edition_id: int
    version: int
    config_hash: str
    prompt_version: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> WriterPolicyVersion:
        return cls(
            id=row[0],
            edition_id=row[1],
            version=row[2],
            config_hash=row[3],
            prompt_version=row[4],
            created_at=row[5],
        )


@dataclass(frozen=True)
class PublicationPolicySet:
    """Bundle of active policy versions for a publication run."""

    eligibility: EligibilityPolicyVersion
    selection: EditorialSelectionPolicyVersion
    writer: WriterPolicyVersion

    @property
    def eligibility_policy_id(self) -> int:
        return self.eligibility.id

    @property
    def selection_policy_id(self) -> int:
        return self.selection.id

    @property
    def writer_policy_id(self) -> int:
        return self.writer.id


@dataclass(frozen=True)
class PublicationRun:
    """A `publication_runs` row."""

    id: int
    edition_id: int
    publication_type: str
    request_key: str
    snapshot_at: dt.datetime
    eligibility_policy_id: int
    selection_policy_id: int
    writer_policy_id: int
    status: str
    error_kind: str | None
    metadata: dict[str, Any]
    created_at: dt.datetime
    completed_at: dt.datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> PublicationRun:
        return cls(
            id=row[0],
            edition_id=row[1],
            publication_type=row[2],
            request_key=row[3],
            snapshot_at=row[4],
            eligibility_policy_id=row[5],
            selection_policy_id=row[6],
            writer_policy_id=row[7],
            status=row[8],
            error_kind=row[9],
            metadata=row[10] if isinstance(row[10], dict) else {},
            created_at=row[11],
            completed_at=row[12],
        )


@dataclass(frozen=True)
class PublicationCandidate:
    """A `publication_candidates` row."""

    id: int
    publication_run_id: int
    story_id: int
    story_revision_id: int
    deterministic_rank: int
    snapshot_features: dict[str, Any]
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> PublicationCandidate:
        return cls(
            id=row[0],
            publication_run_id=row[1],
            story_id=row[2],
            story_revision_id=row[3],
            deterministic_rank=row[4],
            snapshot_features=row[5] if isinstance(row[5], dict) else {},
            created_at=row[6],
        )


@dataclass(frozen=True)
class PublicationSelectionDecision:
    """A `publication_selection_decisions` row."""

    id: int
    publication_run_id: int
    candidate_id: int
    decision: str  # 'INCLUDE', 'OMIT'
    presentation_intent: (
        str | None
    )  # 'lead', 'normal', 'brief', 'unverified_operational', 'follow_up'
    confidence: float | None
    reason: str | None
    rank: int | None
    metadata: dict[str, Any]
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> PublicationSelectionDecision:
        return cls(
            id=row[0],
            publication_run_id=row[1],
            candidate_id=row[2],
            decision=row[3],
            presentation_intent=row[4],
            confidence=float(row[5]) if row[5] is not None else None,
            reason=row[6],
            rank=row[7],
            metadata=row[8] if isinstance(row[8], dict) else {},
            created_at=row[9],
        )


@dataclass(frozen=True)
class PublicationInput:
    """A `publication_inputs` row."""

    id: int
    publication_run_id: int
    story_id: int
    story_revision_id: int
    selection_decision_id: int
    presentation_intent: str | None
    rank: int
    created_at: dt.datetime
    claim_ids: list[int] = field(default_factory=list)
    evidence_cluster_ids: list[int] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: Any) -> PublicationInput:
        return cls(
            id=row[0],
            publication_run_id=row[1],
            story_id=row[2],
            story_revision_id=row[3],
            selection_decision_id=row[4],
            presentation_intent=row[5],
            rank=row[6],
            created_at=row[7],
        )


@dataclass(frozen=True)
class PublicationGenerationAttempt:
    """A `publication_generation_attempts` row."""

    id: int
    publication_run_id: int
    attempt_no: int
    kind: str  # 'writer', 'repair', 'deterministic_fallback', 'story_renderer_fallback'
    status: str  # 'running', 'succeeded', 'failed'
    error_kind: str | None
    provider: str | None
    model: str | None
    prompt_hash: str | None
    metadata: dict[str, Any]
    started_at: dt.datetime
    completed_at: dt.datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> PublicationGenerationAttempt:
        return cls(
            id=row[0],
            publication_run_id=row[1],
            attempt_no=row[2],
            kind=row[3],
            status=row[4],
            error_kind=row[5],
            provider=row[6],
            model=row[7],
            prompt_hash=row[8],
            metadata=row[9] if isinstance(row[9], dict) else {},
            started_at=row[10],
            completed_at=row[11],
        )


@dataclass(frozen=True)
class Publication:
    """A `publications` row."""

    id: int
    publication_run_id: int
    winning_generation_attempt_id: int
    publication_type: str
    title: str
    lead: str | None
    body: str
    metadata: dict[str, Any]
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> Publication:
        return cls(
            id=row[0],
            publication_run_id=row[1],
            winning_generation_attempt_id=row[2],
            publication_type=row[3],
            title=row[4],
            lead=row[5],
            body=row[6],
            metadata=row[7] if isinstance(row[7], dict) else {},
            created_at=row[8],
        )


@dataclass(frozen=True)
class DeliveryDestination:
    """A `delivery_destinations` row."""

    id: int
    edition_id: int
    platform: str  # 'telegram_channel', 'telegraph', 'facebook_page'
    destination_key: str
    config: dict[str, Any]
    is_active: bool
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> DeliveryDestination:
        return cls(
            id=row[0],
            edition_id=row[1],
            platform=row[2],
            destination_key=row[3],
            config=row[4] if isinstance(row[4], dict) else {},
            is_active=bool(row[5]),
            created_at=row[6],
        )


@dataclass(frozen=True)
class PublicationDeliveryPayload:
    """A `publication_delivery_payloads` row."""

    id: int
    publication_id: int
    destination_id: int
    payload_format: str  # 'telegram_html', 'telegraph_nodes', 'facebook_post'
    rendered_content: dict[str, Any]
    content_hash: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> PublicationDeliveryPayload:
        return cls(
            id=row[0],
            publication_id=row[1],
            destination_id=row[2],
            payload_format=row[3],
            rendered_content=row[4] if isinstance(row[4], dict) else {},
            content_hash=row[5],
            created_at=row[6],
        )


@dataclass(frozen=True)
class PublicationDelivery:
    """A `publication_deliveries` row."""

    id: int
    publication_id: int
    destination_id: int
    payload_id: int
    idempotency_key: str
    status: str  # 'pending', 'in_progress', 'succeeded', 'failed', 'outcome_unknown'
    external_delivery_id: str | None
    metadata: dict[str, Any]
    created_at: dt.datetime
    completed_at: dt.datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> PublicationDelivery:
        return cls(
            id=row[0],
            publication_id=row[1],
            destination_id=row[2],
            payload_id=row[3],
            idempotency_key=row[4],
            status=row[5],
            external_delivery_id=row[6],
            metadata=row[7] if isinstance(row[7], dict) else {},
            created_at=row[8],
            completed_at=row[9],
        )


@dataclass(frozen=True)
class PublicationDeliveryAttempt:
    """A `publication_delivery_attempts` row."""

    id: int
    publication_delivery_id: int
    attempt_no: int
    status: str  # 'running', 'succeeded', 'failed', 'outcome_unknown'
    error_kind: str | None
    error_message: str | None
    response: dict[str, Any]
    started_at: dt.datetime
    completed_at: dt.datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> PublicationDeliveryAttempt:
        return cls(
            id=row[0],
            publication_delivery_id=row[1],
            attempt_no=row[2],
            status=row[3],
            error_kind=row[4],
            error_message=row[5],
            response=row[6] if isinstance(row[6], dict) else {},
            started_at=row[7],
            completed_at=row[8],
        )
