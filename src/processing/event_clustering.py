"""Deterministic streaming event clustering and centroid maintenance service."""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Sequence

import psycopg

from src.domain.event_clusters import ClusterAssignmentResult
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
    ) -> ClusterAssignmentResult:
        """Assign one candidate fragment to an existing active story cluster or seed a new one."""
        norm_vec = normalize_vector(vector)
        active_since = item_timestamp - dt.timedelta(hours=active_window_hours)

        # 1. Search candidate clusters
        candidates = await self.cluster_repo.find_candidate_clusters(
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

            assignment_id = await self.cluster_repo.assign_fragment_to_story(
                conn,
                story_id=target_story_id,
                fragment_id=fragment.id,
                fragment_embedding_id=fragment_embedding_id,
                assignment_kind="vector_join",
                similarity=similarity,
            )

            # Update centroid and cluster state
            old_state = await self.cluster_repo.get_cluster_state(conn, target_story_id)
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
