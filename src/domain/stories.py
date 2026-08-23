"""Story aggregate domain models: persistent stories, immutable revisions,
state events, relations, and claim-membership results (migrations/0006).

Immutable snapshots of Plan 3 schema rows. Every persisted dataclass exposes
a ``from_row`` mapper whose positional order matches the explicit SELECT
column lists used by ``src/repositories/stories.py``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Story:
    """A `stories` row: edition-scoped narrative whose current-revision
    pointer is guaranteed same-story by the composite DEFERRABLE FK."""

    id: int
    edition_id: int
    current_revision_id: int | None
    lifecycle_state: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> Story:
        return cls(
            id=row[0],
            edition_id=row[1],
            current_revision_id=row[2],
            lifecycle_state=row[3],
            created_at=row[4],
        )


@dataclass(frozen=True)
class NewStoryRevision:
    """Input for one new immutable revision; the application service passes
    this payload ONLY when it decided a meaningful semantic change exists —
    no AI judgment lives in the repository."""

    current_state: str
    semantic_text: str
    content_hash: str
    created_at: dt.datetime
    title: str | None = None
    summary: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class StoryRevision:
    """An immutable `story_revisions` row (append-only history; never updated)."""

    id: int
    story_id: int
    revision_no: int
    title: str | None
    summary: str | None
    current_state: str
    semantic_text: str
    content_hash: str
    reason: str | None
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> StoryRevision:
        return cls(
            id=row[0],
            story_id=row[1],
            revision_no=row[2],
            title=row[3],
            summary=row[4],
            current_state=row[5],
            semantic_text=row[6],
            content_hash=row[7],
            reason=row[8],
            created_at=row[9],
        )


@dataclass(frozen=True)
class StoryWithRevision:
    """Result of atomically creating a story shell plus its first revision."""

    story_id: int
    revision: StoryRevision


@dataclass(frozen=True)
class StoryStateEvent:
    """An append-only `story_state_events` row; the lifecycle history stays
    reconstructable from these events alone."""

    id: int
    story_id: int
    type: str
    observed_at: dt.datetime
    reason: str | None
    evidence: dict[str, Any]
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> StoryStateEvent:
        return cls(
            id=row[0],
            story_id=row[1],
            type=row[2],
            observed_at=row[3],
            reason=row[4],
            evidence=row[5],
            created_at=row[6],
        )


@dataclass(frozen=True)
class StoryRelation:
    """An immutable `story_relations` row (append-only cross-story graph;
    invalidation is represented by a later state event, never a delete)."""

    id: int
    from_story_id: int
    to_story_id: int
    relation_type: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> StoryRelation:
        return cls(
            id=row[0],
            from_story_id=row[1],
            to_story_id=row[2],
            relation_type=row[3],
            created_at=row[4],
        )


@dataclass(frozen=True)
class StoryMatchingRun:
    """A `story_matching_runs` row; at most one per (claim, policy) ever
    reaches status 'succeeded' (partial unique index uq_story_match_success).
    'stale' marks a run whose frozen target moved before apply — a fresh
    matching task was re-deferred for it on the same connection."""

    id: int
    claim_id: int
    edition_id: int
    policy_id: int
    claim_embedding_id: int | None
    started_at: dt.datetime
    completed_at: dt.datetime | None
    status: str
    error_kind: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> StoryMatchingRun:
        return cls(
            id=row[0],
            claim_id=row[1],
            edition_id=row[2],
            policy_id=row[3],
            claim_embedding_id=row[4],
            started_at=row[5],
            completed_at=row[6],
            status=row[7],
            error_kind=row[8],
            metadata=row[9],
        )


@dataclass(frozen=True)
class FrozenStoryCandidate:
    """An immutable `story_matching_candidates` row: the exact story revision
    identity the matcher read plus its retrieval provenance signals. None of
    the numeric fields is an admission threshold."""

    id: int
    run_id: int
    story_id: int
    story_revision_id: int
    story_revision_embedding_id: int | None
    retrieved_by_vector: bool
    retrieved_by_lexical: bool
    retrieved_by_state: bool
    vector_distance: float | None
    lexical_score: float | None
    location_overlap: float | None
    entity_overlap: float | None
    time_score: float | None
    status_score: float | None
    rank: int

    @classmethod
    def from_row(cls, row: Any) -> FrozenStoryCandidate:
        return cls(
            id=row[0],
            run_id=row[1],
            story_id=row[2],
            story_revision_id=row[3],
            story_revision_embedding_id=row[4],
            retrieved_by_vector=bool(row[5]),
            retrieved_by_lexical=bool(row[6]),
            retrieved_by_state=bool(row[7]),
            vector_distance=row[8],
            lexical_score=row[9],
            location_overlap=row[10],
            entity_overlap=row[11],
            time_score=row[12],
            status_score=row[13],
            rank=row[14],
        )


@dataclass(frozen=True)
class StoryMatchDecisionRecord:
    """The single persisted `story_match_decisions` verdict for one run
    (UNIQUE run_id). ``story_update`` keeps the raw proposed payload."""

    id: int
    run_id: int
    assignment: str
    target_story_id: int | None
    story_update: dict[str, Any] | None
    confidence: float | None
    reason: str | None
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> StoryMatchDecisionRecord:
        return cls(
            id=row[0],
            run_id=row[1],
            assignment=row[2],
            target_story_id=row[3],
            story_update=row[4],
            confidence=None if row[5] is None else float(row[5]),
            reason=row[6],
            created_at=row[7],
        )


@dataclass(frozen=True)
class StoryMatchingPolicyVersion:
    """A `story_matching_policy_versions` row: per-edition retrieval/matcher
    identity (embedding space, per-signal recall limits, total candidate cap,
    broad resolved-story lookback, prompt/config hash)."""

    id: int
    edition_id: int
    version: int
    config_hash: str
    prompt_version: str
    vector_limit: int
    lexical_limit: int
    state_fallback_limit: int
    total_candidate_limit: int
    resolved_lookback_days: int
    embedding_model: str
    embedding_dimensions: int
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> StoryMatchingPolicyVersion:
        return cls(
            id=row[0],
            edition_id=row[1],
            version=row[2],
            config_hash=row[3],
            prompt_version=row[4],
            vector_limit=row[5],
            lexical_limit=row[6],
            state_fallback_limit=row[7],
            total_candidate_limit=row[8],
            resolved_lookback_days=row[9],
            embedding_model=row[10],
            embedding_dimensions=row[11],
            created_at=row[12],
        )
