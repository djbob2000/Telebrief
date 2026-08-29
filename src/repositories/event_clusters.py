"""Repository for story cluster states and fragment assignments."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector_async

from src.domain.event_clusters import ClusterJoinCandidate, StoryClusterState
from src.repositories.embeddings import _vec_to_list


class EventClusterRepository:
    """Persistence for story_cluster_state and story_fragments."""

    async def find_candidate_clusters(
        self,
        conn: psycopg.AsyncConnection,
        edition_id: int,
        query_vector: Sequence[float],
        *,
        model: str,
        dimensions: int,
        active_since: dt.datetime,
        limit: int = 20,
    ) -> list[ClusterJoinCandidate]:
        """Find candidate clusters in the active window ordered by cosine distance."""
        await register_vector_async(conn)
        vec = Vector(list(query_vector))
        cursor = await conn.execute(
            """
            SELECT sc.story_id, 1.0 - (sc.centroid <=> %s) AS similarity,
                   sc.fragment_count, sc.last_seen_at
            FROM story_cluster_state sc
            JOIN stories s ON s.id = sc.story_id
            WHERE s.edition_id = %s
              AND s.lifecycle_state IN ('candidate', 'active', 'reopened')
              AND sc.model = %s
              AND sc.dimensions = %s
              AND sc.last_seen_at >= %s
            ORDER BY sc.centroid <=> %s ASC
            LIMIT %s
            """,
            (vec, edition_id, model, dimensions, active_since, vec, limit),
        )
        candidates: list[ClusterJoinCandidate] = []
        async for row in cursor:
            candidates.append(
                ClusterJoinCandidate(
                    story_id=int(row[0]),
                    similarity=float(row[1]),
                    fragment_count=int(row[2]),
                    last_seen_at=row[3],
                )
            )
        return candidates

    async def get_cluster_state(
        self, conn: psycopg.AsyncConnection, story_id: int
    ) -> StoryClusterState | None:
        cursor = await conn.execute(
            """
            SELECT story_id, centroid, model, dimensions, fragment_count,
                   unique_source_count, first_seen_at, last_seen_at,
                   latest_assignment_id, last_analyzed_assignment_id,
                   last_analyzed_at, analysis_dirty, updated_at
            FROM story_cluster_state
            WHERE story_id = %s
            """,
            (story_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        vec = _vec_to_list(row[1])
        return StoryClusterState.from_row(row, vec)

    async def list_dirty_cluster_states(
        self,
        conn: psycopg.AsyncConnection,
        edition_id: int | None = None,
        *,
        limit: int = 100,
    ) -> list[StoryClusterState]:
        if edition_id is not None:
            cursor = await conn.execute(
                """
                SELECT sc.story_id, sc.centroid, sc.model, sc.dimensions, sc.fragment_count,
                       sc.unique_source_count, sc.first_seen_at, sc.last_seen_at,
                       sc.latest_assignment_id, sc.last_analyzed_assignment_id,
                       sc.last_analyzed_at, sc.analysis_dirty, sc.updated_at
                FROM story_cluster_state sc
                JOIN stories s ON s.id = sc.story_id
                WHERE sc.analysis_dirty = TRUE AND s.edition_id = %s
                ORDER BY sc.last_seen_at ASC
                LIMIT %s
                """,
                (edition_id, limit),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT sc.story_id, sc.centroid, sc.model, sc.dimensions, sc.fragment_count,
                       sc.unique_source_count, sc.first_seen_at, sc.last_seen_at,
                       sc.latest_assignment_id, sc.last_analyzed_assignment_id,
                       sc.last_analyzed_at, sc.analysis_dirty, sc.updated_at
                FROM story_cluster_state sc
                WHERE sc.analysis_dirty = TRUE
                ORDER BY sc.last_seen_at ASC
                LIMIT %s
                """,
                (limit,),
            )
        res: list[StoryClusterState] = []
        async for row in cursor:
            vec = _vec_to_list(row[1])
            res.append(StoryClusterState.from_row(row, vec))
        return res

    async def list_dirty_edition_ids(self, conn: psycopg.AsyncConnection) -> list[int]:
        cur = await conn.execute(
            """
            SELECT DISTINCT s.edition_id
            FROM story_cluster_state sc
            JOIN stories s ON s.id = sc.story_id
            WHERE sc.analysis_dirty = TRUE
              AND s.knowledge_source = 'event_first'
            ORDER BY s.edition_id
            """
        )
        return [int(row[0]) for row in await cur.fetchall()]

    async def assign_fragment_to_story(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        fragment_id: int,
        fragment_embedding_id: int,
        assignment_kind: str,
        similarity: float | None = None,
    ) -> int:
        cursor = await conn.execute(
            """
            INSERT INTO story_fragments (
                story_id, fragment_id, fragment_embedding_id, assignment_kind, similarity
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (fragment_id) DO UPDATE SET
                story_id = EXCLUDED.story_id,
                fragment_embedding_id = EXCLUDED.fragment_embedding_id,
                assignment_kind = EXCLUDED.assignment_kind,
                similarity = EXCLUDED.similarity
            RETURNING id
            """,
            (story_id, fragment_id, fragment_embedding_id, assignment_kind, similarity),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("assign_fragment_to_story produced no row")
        return int(row[0])

    async def upsert_cluster_state(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        centroid: Sequence[float],
        model: str,
        dimensions: int,
        fragment_count: int,
        unique_source_count: int,
        first_seen_at: dt.datetime,
        last_seen_at: dt.datetime,
        latest_assignment_id: int,
        analysis_dirty: bool = True,
    ) -> None:
        await register_vector_async(conn)
        vec = Vector(list(centroid))
        await conn.execute(
            """
            INSERT INTO story_cluster_state (
                story_id, centroid, model, dimensions, fragment_count,
                unique_source_count, first_seen_at, last_seen_at,
                latest_assignment_id, analysis_dirty, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (story_id) DO UPDATE SET
                centroid = EXCLUDED.centroid,
                model = EXCLUDED.model,
                dimensions = EXCLUDED.dimensions,
                fragment_count = EXCLUDED.fragment_count,
                unique_source_count = EXCLUDED.unique_source_count,
                last_seen_at = EXCLUDED.last_seen_at,
                latest_assignment_id = EXCLUDED.latest_assignment_id,
                analysis_dirty = EXCLUDED.analysis_dirty,
                updated_at = now()
            """,
            (
                story_id,
                vec,
                model,
                dimensions,
                fragment_count,
                unique_source_count,
                first_seen_at,
                last_seen_at,
                latest_assignment_id,
                analysis_dirty,
            ),
        )

    async def update_cluster_analysis_analyzed(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        assignment_id: int,
        analyzed_at: dt.datetime,
    ) -> None:
        await conn.execute(
            """
            UPDATE story_cluster_state
            SET last_analyzed_assignment_id = %s,
                last_analyzed_at = %s,
                analysis_dirty = CASE WHEN latest_assignment_id = %s THEN FALSE ELSE TRUE END,
                updated_at = now()
            WHERE story_id = %s
            """,
            (assignment_id, analyzed_at, assignment_id, story_id),
        )

    async def mark_cluster_processed_without_analysis(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        assignment_id: int,
    ) -> None:
        """Clear analysis_dirty if assignment is current without changing last_analyzed_*."""
        await conn.execute(
            """
            UPDATE story_cluster_state
            SET analysis_dirty = CASE WHEN latest_assignment_id = %s THEN FALSE ELSE TRUE END,
                updated_at = now()
            WHERE story_id = %s
            """,
            (assignment_id, story_id),
        )

    async def get_unique_sources_for_story(
        self, conn: psycopg.AsyncConnection, story_id: int
    ) -> int:
        cursor = await conn.execute(
            """
            SELECT COUNT(DISTINCT si.source_id)
            FROM story_fragments sf
            JOIN source_fragments f ON f.id = sf.fragment_id
            JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
            JOIN source_items si ON si.id = sir.source_item_id
            WHERE sf.story_id = %s
            """,
            (story_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row is not None else 1
