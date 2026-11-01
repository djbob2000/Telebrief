"""Deterministic streaming event clustering and centroid maintenance service."""

from __future__ import annotations

import datetime as dt
import logging
import math
import operator
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

import psycopg

from src.domain.event_clusters import ClusterAssignmentResult, ClusterJoinCandidate
from src.domain.event_pipeline import SourceFragment
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.stories import StoryRepository

logger = logging.getLogger(__name__)


def normalize_vector(v: Sequence[float]) -> list[float]:
    """L2 normalize vector."""
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0.0:
        return [float(x) for x in v]
    return [float(x) / norm for x in v]


def combine_centroids(
    old_centroid: Sequence[float], old_count: int, new_vector: Sequence[float]
) -> list[float]:
    """Compute updated normalized centroid after adding a new observation."""
    combined = [
        float(old_c) * old_count + float(new_v)
        for old_c, new_v in zip(old_centroid, new_vector, strict=True)
    ]
    return normalize_vector(combined)


def _vector_norm(v: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


@dataclass(frozen=True)
class _CachedCentroid:
    updated_at: dt.datetime
    vector: array[float]
    norm: float


class CandidateCentroidCache:
    """In-memory centroids for the candidate search of one clustering service.

    Every search still reads the candidate universe from the database, so the
    filters, the active window and the visibility of concurrent joins match a
    plain query. Only centroid vectors are reused, and only while the stored
    ``updated_at`` still matches the committed row, so a stale vector can never
    rank a candidate. Entries for stories that left the universe are dropped.
    """

    def __init__(self, cluster_repo: EventClusterRepository) -> None:
        self._cluster_repo = cluster_repo
        self._centroids: dict[int, _CachedCentroid] = {}

    async def find_candidates(
        self,
        conn: psycopg.AsyncConnection,
        edition_id: int,
        query_vector: Sequence[float],
        *,
        model: str,
        dimensions: int,
        active_since: dt.datetime,
        limit: int,
    ) -> list[ClusterJoinCandidate]:
        rows = await self._cluster_repo.list_candidate_metadata(
            conn,
            edition_id,
            model=model,
            dimensions=dimensions,
            active_since=active_since,
        )
        universe = {row.story_id for row in rows}
        for story_id in [sid for sid in self._centroids if sid not in universe]:
            del self._centroids[story_id]

        # Snapshot what is valid now. Another job sharing this cache may prune
        # or refresh entries while this search awaits the database, so scoring
        # uses only this snapshot plus the vectors loaded for this search.
        current: dict[int, _CachedCentroid] = {}
        stale: list[int] = []
        for row in rows:
            cached = self._centroids.get(row.story_id)
            if cached is None or cached.updated_at != row.updated_at:
                stale.append(row.story_id)
            else:
                current[row.story_id] = cached
        if stale:
            loaded = await self._cluster_repo.load_centroids(conn, stale)
            for story_id, (updated_at, vector) in loaded.items():
                entry = _CachedCentroid(
                    updated_at=updated_at,
                    vector=vector,
                    norm=_vector_norm(vector),
                )
                self._centroids[story_id] = entry
                current[story_id] = entry

        query = [float(x) for x in query_vector]
        query_norm = _vector_norm(query)
        scored: list[ClusterJoinCandidate] = []
        for row in rows:
            cached = current.get(row.story_id)
            if cached is None:
                continue
            denominator = query_norm * cached.norm
            similarity = (
                sum(map(operator.mul, query, cached.vector)) / denominator if denominator else 0.0
            )
            scored.append(
                ClusterJoinCandidate(
                    story_id=row.story_id,
                    similarity=similarity,
                    fragment_count=row.fragment_count,
                    last_seen_at=row.last_seen_at,
                )
            )
        scored.sort(key=lambda candidate: (-candidate.similarity, candidate.story_id))
        return scored[:limit]


class EventClusteringService:
    """Assigns candidate fragments to active stories or seeds new story shells."""

    def __init__(
        self,
        cluster_repo: EventClusterRepository | None = None,
        story_repo: StoryRepository | None = None,
        logger_instance: logging.Logger | None = None,
        candidate_cache: CandidateCentroidCache | None = None,
    ) -> None:
        self.cluster_repo = cluster_repo or EventClusterRepository()
        self.story_repo = story_repo or StoryRepository()
        self.logger = logger_instance or logger
        self._candidate_cache = candidate_cache or CandidateCentroidCache(self.cluster_repo)

    async def process_fragment(
        self,
        conn: psycopg.AsyncConnection,
        fragment: SourceFragment,
        *,
        edition_id: int,
        fragment_embedding_id: int,
        vector: Sequence[float],
        model: str,
        dimensions: int,
        item_timestamp: dt.datetime,
        join_similarity: float = 0.84,
        active_window_hours: int = 72,
        max_cluster_candidates: int = 20,
    ) -> ClusterAssignmentResult:
        """Assign one candidate fragment to an existing active story cluster or seed a new one."""
        norm_vec = normalize_vector(vector)
        active_since = item_timestamp - dt.timedelta(hours=active_window_hours)

        # A revision retry may arrive after this fragment's assignment and
        # cluster-state update already committed. The unique fragment
        # assignment is the durable idempotency key; replaying it must not
        # increment the centroid/count a second time.
        existing_assignment = await self.cluster_repo.get_fragment_assignment(conn, fragment.id)
        if existing_assignment is not None:
            assignment_id, story_id, assignment_kind, similarity = existing_assignment
            return ClusterAssignmentResult(
                fragment_id=fragment.id,
                story_id=story_id,
                assignment_kind=cast(
                    Literal["new_story", "vector_join", "manual"], assignment_kind
                ),
                similarity=similarity,
                assignment_id=assignment_id,
            )

        # 1. Search candidate clusters
        candidates = await self._candidate_cache.find_candidates(
            conn,
            edition_id,
            norm_vec,
            model=model,
            dimensions=dimensions,
            active_since=active_since,
            limit=max_cluster_candidates,
        )

        # 2. Check if closest cluster meets join threshold
        if candidates and candidates[0].similarity >= join_similarity:
            best = candidates[0]
            target_story_id = best.story_id
            similarity = best.similarity

            # Lock exactly the target row before assignment and centroid
            # read-modify-write. The task owns the surrounding short
            # transaction, so concurrent workers cannot lose an increment.
            old_state = await self.cluster_repo.lock_cluster_state(conn, target_story_id)

            assignment_id = await self.cluster_repo.assign_fragment_to_story(
                conn,
                story_id=target_story_id,
                fragment_id=fragment.id,
                fragment_embedding_id=fragment_embedding_id,
                assignment_kind="vector_join",
                similarity=similarity,
            )

            # Update centroid and cluster state
            if old_state is not None:
                new_centroid = combine_centroids(
                    old_state.centroid, old_state.fragment_count, norm_vec
                )
                new_frag_count = old_state.fragment_count + 1
                first_seen = min(old_state.first_seen_at, item_timestamp)
                last_seen = max(old_state.last_seen_at, item_timestamp)
            else:
                new_centroid = norm_vec
                new_frag_count = 2
                first_seen = item_timestamp
                last_seen = item_timestamp

            unique_sources = await self.cluster_repo.get_unique_sources_for_story(
                conn, target_story_id
            )

            await self.cluster_repo.upsert_cluster_state(
                conn,
                story_id=target_story_id,
                centroid=new_centroid,
                model=model,
                dimensions=dimensions,
                fragment_count=new_frag_count,
                unique_source_count=unique_sources,
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                latest_assignment_id=assignment_id,
                analysis_dirty=True,
            )

            return ClusterAssignmentResult(
                fragment_id=fragment.id,
                story_id=target_story_id,
                assignment_kind="vector_join",
                similarity=similarity,
                assignment_id=assignment_id,
            )

        # 3. No match -> Seed new Story shell
        story_id = await self.story_repo.create_story_shell(
            conn, edition_id=edition_id, knowledge_source="event_first"
        )
        assignment_id = await self.cluster_repo.assign_fragment_to_story(
            conn,
            story_id=story_id,
            fragment_id=fragment.id,
            fragment_embedding_id=fragment_embedding_id,
            assignment_kind="new_story",
            similarity=None,
        )
        await self.cluster_repo.upsert_cluster_state(
            conn,
            story_id=story_id,
            centroid=norm_vec,
            model=model,
            dimensions=dimensions,
            fragment_count=1,
            unique_source_count=1,
            first_seen_at=item_timestamp,
            last_seen_at=item_timestamp,
            latest_assignment_id=assignment_id,
            analysis_dirty=True,
        )

        return ClusterAssignmentResult(
            fragment_id=fragment.id,
            story_id=story_id,
            assignment_kind="new_story",
            similarity=None,
            assignment_id=assignment_id,
        )
