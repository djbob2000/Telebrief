"""Deterministic streaming event clustering and centroid maintenance service."""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Sequence
from itertools import islice
from typing import Literal, cast

import psycopg

from src.domain.event_clusters import (
    ClusterAssignmentResult,
    ClusterJoinCandidate,
    ClusterStateChange,
)
from src.domain.event_pipeline import SourceFragment
from src.processing.cluster_index import ClusterIndex
from src.processing.conversation import ThinFragmentRouting, is_thin_fragment
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
        candidate_index: ClusterIndex | None = None,
        thin_routing: ThinFragmentRouting | None = None,
    ) -> ClusterAssignmentResult:
        """Assign one candidate fragment to an existing active story cluster or seed a new one.

        With candidate_index, join candidates are ranked in memory instead of by
        the database query. The caller applies the returned state_change to that
        index only after this transaction commits.

        With thin_routing, a remark too short for its vector to name an event
        joins through its reply link or its conversation instead of by global
        similarity.
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
                    Literal["new_story", "vector_join", "reply_join", "manual"], assignment_kind
                ),
                similarity=similarity,
                assignment_id=assignment_id,
            )

        async def rank_candidates(since: dt.datetime) -> list[ClusterJoinCandidate]:
            if candidate_index is None:
                return await self.cluster_repo.find_candidate_clusters(
                    conn,
                    edition_id,
                    norm_vec,
                    model=model,
                    dimensions=dimensions,
                    active_since=since,
                    limit=max_cluster_candidates,
                )
            return candidate_index.nearest(
                norm_vec, active_since=since, limit=max_cluster_candidates
            )

        is_thin = thin_routing is not None and is_thin_fragment(
            fragment.text_content, max_content_words=thin_routing.max_content_words
        )

        # 1. A reply belongs to the conversation it answers, whatever its vector says.
        if thin_routing is not None and is_thin:
            parent_story_id = await self.cluster_repo.find_reply_parent_story(
                conn,
                source_item_revision_id=fragment.source_item_revision_id,
                edition_id=edition_id,
                active_since=active_since,
                max_hops=thin_routing.max_reply_hops,
            )
            if parent_story_id is not None:
                return await self._join_story(
                    conn,
                    fragment,
                    story_id=parent_story_id,
                    fragment_embedding_id=fragment_embedding_id,
                    vector=norm_vec,
                    model=model,
                    dimensions=dimensions,
                    item_timestamp=item_timestamp,
                    assignment_kind="reply_join",
                    similarity=None,
                )

        # 2. Search candidate clusters
        candidates = await rank_candidates(active_since)
        join_target: ClusterJoinCandidate | None = None
        if candidates and candidates[0].similarity >= join_similarity:
            join_target = candidates[0]

        if thin_routing is not None and is_thin and join_target is not None:
            # A thin remark may still repeat a report almost word for word.
            # Anything less only counts inside its own running conversation.
            if join_target.similarity < thin_routing.near_identical_similarity:
                join_target = await self._nearby_conversation_story(
                    conn,
                    fragment,
                    await rank_candidates(item_timestamp - thin_routing.conversation_window),
                    join_similarity=join_similarity,
                    routing=thin_routing,
                )

        # 3. Join the closest cluster that met the threshold
        if join_target is not None:
            return await self._join_story(
                conn,
                fragment,
                story_id=join_target.story_id,
                fragment_embedding_id=fragment_embedding_id,
                vector=norm_vec,
                model=model,
                dimensions=dimensions,
                item_timestamp=item_timestamp,
                assignment_kind="vector_join",
                similarity=join_target.similarity,
            )

        # 4. No match -> Seed new Story shell
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
            state_change=ClusterStateChange(
                story_id=story_id,
                centroid=norm_vec,
                fragment_count=1,
                last_seen_at=item_timestamp,
            ),
        )

    async def _nearby_conversation_story(
        self,
        conn: psycopg.AsyncConnection,
        fragment: SourceFragment,
        candidates: Sequence[ClusterJoinCandidate],
        *,
        join_similarity: float,
        routing: ThinFragmentRouting,
    ) -> ClusterJoinCandidate | None:
        """Return the closest Story discussed in this fragment's chat around the same time."""
        eligible = (c for c in candidates if c.similarity >= join_similarity)
        for candidate in islice(eligible, routing.max_conversation_candidates):
            if await self.cluster_repo.has_fragment_near_in_same_source(
                conn,
                story_id=candidate.story_id,
                source_item_revision_id=fragment.source_item_revision_id,
                window=routing.conversation_window,
            ):
                return candidate
        return None

    async def _join_story(
        self,
        conn: psycopg.AsyncConnection,
        fragment: SourceFragment,
        *,
        story_id: int,
        fragment_embedding_id: int,
        vector: list[float],
        model: str,
        dimensions: int,
        item_timestamp: dt.datetime,
        assignment_kind: Literal["vector_join", "reply_join"],
        similarity: float | None,
    ) -> ClusterAssignmentResult:
        """Add a fragment to a Story and update that Story's cluster state."""
        # Lock exactly the target row before assignment and centroid
        # read-modify-write. The task owns the surrounding short
        # transaction, so concurrent workers cannot lose an increment.
        old_state = await self.cluster_repo.lock_cluster_state(conn, story_id)

        assignment_id = await self.cluster_repo.assign_fragment_to_story(
            conn,
            story_id=story_id,
            fragment_id=fragment.id,
            fragment_embedding_id=fragment_embedding_id,
            assignment_kind=assignment_kind,
            similarity=similarity,
        )

        # Update centroid and cluster state
        if old_state is not None:
            # A reply joined by its link says nothing about the Story's subject,
            # and letting it move the centroid is how a Story becomes a noise sink.
            new_centroid = (
                list(old_state.centroid)
                if assignment_kind == "reply_join"
                else combine_centroids(old_state.centroid, old_state.fragment_count, vector)
            )
            new_frag_count = old_state.fragment_count + 1
            first_seen = min(old_state.first_seen_at, item_timestamp)
            last_seen = max(old_state.last_seen_at, item_timestamp)
        else:
            new_centroid = vector
            new_frag_count = 2
            first_seen = item_timestamp
            last_seen = item_timestamp

        unique_sources = await self.cluster_repo.get_unique_sources_for_story(conn, story_id)

        await self.cluster_repo.upsert_cluster_state(
            conn,
            story_id=story_id,
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
            story_id=story_id,
            assignment_kind=assignment_kind,
            similarity=similarity,
            assignment_id=assignment_id,
            state_change=ClusterStateChange(
                story_id=story_id,
                centroid=new_centroid,
                fragment_count=new_frag_count,
                last_seen_at=last_seen,
            ),
        )
