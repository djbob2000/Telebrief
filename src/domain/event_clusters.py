"""Domain models for event clustering and centroid maintenance."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class StoryClusterState:
    """Operational state of a Story vector cluster."""

    story_id: int
    centroid: list[float]
    model: str
    dimensions: int
    fragment_count: int
    unique_source_count: int
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
    latest_assignment_id: int
    last_analyzed_assignment_id: int | None
    last_analyzed_at: dt.datetime | None
    analysis_dirty: bool
    updated_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any, centroid_vec: list[float]) -> StoryClusterState:
        return cls(
            story_id=int(row[0]),
            centroid=centroid_vec,
            model=str(row[2]),
            dimensions=int(row[3]),
            fragment_count=int(row[4]),
            unique_source_count=int(row[5]),
            first_seen_at=row[6],
            last_seen_at=row[7],
            latest_assignment_id=int(row[8]),
            last_analyzed_assignment_id=int(row[9]) if row[9] is not None else None,
            last_analyzed_at=row[10],
            analysis_dirty=bool(row[11]),
            updated_at=row[12],
        )


@dataclass(frozen=True)
class ClusterJoinCandidate:
    """A nearby candidate story for vector join."""

    story_id: int
    similarity: float
    fragment_count: int
    last_seen_at: dt.datetime


@dataclass(frozen=True)
class ClusterAssignmentResult:
    """Result of assigning a fragment to a story cluster."""

    fragment_id: int
    story_id: int
    assignment_kind: Literal["new_story", "vector_join", "manual"]
    similarity: float | None
    assignment_id: int
