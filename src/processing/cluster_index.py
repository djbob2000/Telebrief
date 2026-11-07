"""In-memory index of active story centroids for one batch's clustering pass.

The database candidate query reads every active centroid (about 6.6k rows,
39 MB on production) for each fragment. Within one batch the active set
changes only through the assignments that batch commits, so the index is
loaded once per edition and kept in step with those commits.

The index only ranks join candidates. The authoritative centroid update still
locks and reads the story row in the database, so a centroid that another
writer changed mid-batch is never written from stale data.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

import numpy as np
import psycopg

from src.domain.event_clusters import ClusterJoinCandidate, ClusterStateChange
from src.repositories.embeddings import _vec_to_list
from src.repositories.event_clusters import EventClusterRepository

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def _micros(ts: dt.datetime) -> int:
    """Exact integer microseconds since the epoch, comparable without datetime objects."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    return (ts - _EPOCH) // dt.timedelta(microseconds=1)


def _unit_vector(value: Any) -> np.ndarray:
    """Return a float32 vector scaled to unit length (zero vectors are returned unchanged)."""
    if hasattr(value, "to_numpy"):
        raw: Any = value.to_numpy()
    elif isinstance(value, np.ndarray | list | tuple):
        raw = value
    else:
        raw = _vec_to_list(value)
    vec = np.asarray(raw, dtype=np.float32)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0.0 else vec


class ClusterIndex:
    """Ranks join candidates for one edition, model and dimension set, in memory."""

    def __init__(self, *, dimensions: int, capacity: int) -> None:
        self._dimensions = dimensions
        rows = max(capacity, 1)
        self._matrix = np.zeros((rows, dimensions), dtype=np.float32)
        self._last_seen = np.zeros(rows, dtype=np.int64)
        self._story_ids: list[int] = []
        self._fragment_counts: list[int] = []
        self._last_seen_at: list[dt.datetime] = []
        self._row_by_story: dict[int, int] = {}

    def __len__(self) -> int:
        return len(self._story_ids)

    def set_row(
        self,
        story_id: int,
        centroid: Any,
        last_seen_at: dt.datetime,
        fragment_count: int,
    ) -> None:
        """Insert or replace one story's centroid and activity."""
        vec = _unit_vector(centroid)
        if vec.shape != (self._dimensions,):
            raise ValueError(f"centroid has shape {vec.shape}, index expects ({self._dimensions},)")
        row = self._row_by_story.get(story_id)
        if row is None:
            row = len(self._story_ids)
            if row >= self._matrix.shape[0]:
                self._grow()
            self._row_by_story[story_id] = row
            self._story_ids.append(story_id)
            self._fragment_counts.append(fragment_count)
            self._last_seen_at.append(last_seen_at)
        else:
            self._fragment_counts[row] = fragment_count
            self._last_seen_at[row] = last_seen_at
        self._matrix[row] = vec
        self._last_seen[row] = _micros(last_seen_at)

    def apply(self, change: ClusterStateChange) -> None:
        """Mirror a cluster state that a committed assignment just wrote."""
        self.set_row(change.story_id, change.centroid, change.last_seen_at, change.fragment_count)

    def nearest(
        self,
        query: Sequence[float],
        *,
        active_since: dt.datetime,
        limit: int,
    ) -> list[ClusterJoinCandidate]:
        """Rank stories by cosine similarity, most similar first, among rows active since the cutoff."""
        count = len(self._story_ids)
        if count == 0 or limit <= 0:
            return []
        eligible = self._last_seen[:count] >= _micros(active_since)
        eligible_count = int(np.count_nonzero(eligible))
        if eligible_count == 0:
            return []
        sims = self._matrix[:count] @ _unit_vector(query)
        scores = np.where(eligible, sims, np.float32(-np.inf))
        order = np.argsort(-scores, kind="stable")[: min(limit, eligible_count)]
        return [
            ClusterJoinCandidate(
                story_id=self._story_ids[row],
                similarity=float(sims[row]),
                fragment_count=self._fragment_counts[row],
                last_seen_at=self._last_seen_at[row],
            )
            for row in order.tolist()
        ]

    def _grow(self) -> None:
        capacity = max(1, self._matrix.shape[0] * 2)
        matrix = np.zeros((capacity, self._dimensions), dtype=np.float32)
        matrix[: self._matrix.shape[0]] = self._matrix
        last_seen = np.zeros(capacity, dtype=np.int64)
        last_seen[: self._last_seen.shape[0]] = self._last_seen
        self._matrix = matrix
        self._last_seen = last_seen


async def load_cluster_index(
    conn: psycopg.AsyncConnection,
    cluster_repo: EventClusterRepository,
    edition_id: int,
    *,
    model: str,
    dimensions: int,
    active_since: dt.datetime,
    extra_capacity: int,
) -> ClusterIndex:
    """Load one edition's candidate rows; extra_capacity reserves room for stories this batch creates."""
    count = await cluster_repo.count_active_centroids(
        conn, edition_id, model=model, dimensions=dimensions, active_since=active_since
    )
    index = ClusterIndex(dimensions=dimensions, capacity=count + extra_capacity)
    async for row in cluster_repo.stream_active_centroids(
        conn, edition_id, model=model, dimensions=dimensions, active_since=active_since
    ):
        index.set_row(row.story_id, row.centroid, row.last_seen_at, row.fragment_count)
    return index
