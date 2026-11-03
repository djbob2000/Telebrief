"""Deterministic streaming event clustering and centroid maintenance service."""

from __future__ import annotations

import datetime as dt
import logging
import math
import operator
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


@dataclass(frozen=True)
class _ChangedStory:
    """A story that this batch joined or created, with its current centroid."""

    edition_id: int
    centroid: list[float]
    fragment_count: int
    last_seen_at: dt.datetime


class BatchCandidateIndex:
    """Join candidates for one revision batch, read from the database once.

    The database snapshot is taken before the batch assigns its fragments. The
    fragments of the batch then search it in memory. Stories that this batch
    joins or creates are tracked separately and scored with their current
    centroids. A snapshot entry for such a story is dropped, because its
    snapshot centroid is stale. Writes from other jobs become visible at the
    next batch, while the per-story row lock still protects each update.
    """

    def __init__(self) -> None:
        self._snapshot: dict[int, list[ClusterJoinCandidate]] = {}
        self._changed: dict[int, _ChangedStory] = {}
        self._staged: tuple[int, _ChangedStory] | None = None

    def add_snapshot(self, results: dict[int, list[ClusterJoinCandidate]]) -> None:
        self._snapshot.update(results)

    def candidates(
        self,
        fragment_id: int,
        *,
        edition_id: int,
        vector: Sequence[float],
        active_since: dt.datetime,
        limit: int,
    ) -> list[ClusterJoinCandidate]:
        """Return the best ``limit`` candidates, as a fresh database search would."""
        merged = [
            candidate
            for candidate in self._snapshot.get(fragment_id, [])
            if candidate.story_id not in self._changed
        ]
        for story_id, changed in self._changed.items():
            if changed.edition_id != edition_id or changed.last_seen_at < active_since:
                continue
            merged.append(
                ClusterJoinCandidate(
                    story_id=story_id,
                    similarity=sum(map(operator.mul, changed.centroid, vector)),
                    fragment_count=changed.fragment_count,
                    last_seen_at=changed.last_seen_at,
                )
            )
        merged.sort(key=lambda candidate: candidate.similarity, reverse=True)
        return merged[:limit]

    def stage(
        self,
        story_id: int,
        *,
        edition_id: int,
        centroid: list[float],
        fragment_count: int,
        last_seen_at: dt.datetime,
    ) -> None:
        """Record the change of the fragment being assigned; applied by commit()."""
        self._staged = (
            story_id,
            _ChangedStory(
                edition_id=edition_id,
                centroid=centroid,
                fragment_count=fragment_count,
                last_seen_at=last_seen_at,
            ),
        )

    def commit(self) -> None:
        """Apply the staged change after its database transaction has committed."""
        if self._staged is not None:
            story_id, changed = self._staged
            self._changed[story_id] = changed
        self._staged = None

    def discard(self) -> None:
        """Drop the staged change after a failed or retried transaction."""
        self._staged = None


class EventClusteringService:
    """Assigns candidate fragments to active stories or seeds new story shells."""

    def __init__(
        self,
        cluster_repo: EventClusterRepository | None = None,
        story_repo: StoryRepository | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        self.cluster_repo = cluster_repo or EventClusterRepository()
        self.story_repo = story_repo or StoryRepository()
        self.logger = logger_instance or logger

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
        candidate_index: BatchCandidateIndex | None = None,
    ) -> ClusterAssignmentResult:
        """Assign one candidate fragment to an existing active story cluster or seed a new one.

        With ``candidate_index`` the search reads the batch snapshot instead of the
        table, and the caller must call ``candidate_index.commit()`` after the
        transaction succeeds.
        """
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
        if candidate_index is None:
            candidates = await self.cluster_repo.find_candidate_clusters(
                conn,
                edition_id,
                norm_vec,
                model=model,
                dimensions=dimensions,
                active_since=active_since,
                limit=max_cluster_candidates,
            )
        else:
            candidates = candidate_index.candidates(
                fragment.id,
                edition_id=edition_id,
                vector=norm_vec,
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
            if candidate_index is not None:
                candidate_index.stage(
                    target_story_id,
                    edition_id=edition_id,
                    centroid=new_centroid,
                    fragment_count=new_frag_count,
                    last_seen_at=last_seen,
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
        if candidate_index is not None:
            candidate_index.stage(
                story_id,
                edition_id=edition_id,
                centroid=norm_vec,
                fragment_count=1,
                last_seen_at=item_timestamp,
            )

        return ClusterAssignmentResult(
            fragment_id=fragment.id,
            story_id=story_id,
            assignment_kind="new_story",
            similarity=None,
            assignment_id=assignment_id,
        )
