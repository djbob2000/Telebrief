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
