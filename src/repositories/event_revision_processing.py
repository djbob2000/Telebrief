"""Durable processing state for source revisions entering Event-First."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class RevisionProcessingState:
    """Persisted processing state for one source-item revision."""

    revision_id: int
    status: str
    processing_mode: str
    reused_from_revision_id: int | None
    completed_at: dt.datetime | None


class EventRevisionProcessingRepository:
    """Persist at-least-once processing state for exact source revisions."""

    async def mark_pending(
        self, conn: psycopg.AsyncConnection, revision_ids: Sequence[int]
    ) -> None:
        if not revision_ids:
            return
        await conn.execute(
            """
            INSERT INTO event_revision_processing_state (
                source_item_revision_id, status, attempt_count
            )
            SELECT revision_id, 'pending', 0
            FROM unnest(%s::bigint[]) AS revision_id
            ON CONFLICT (source_item_revision_id) DO NOTHING
            """,
            (list(revision_ids),),
        )

    async def list_incomplete(
        self, conn: psycopg.AsyncConnection, revision_ids: Sequence[int]
    ) -> list[int]:
        if not revision_ids:
            return []
        cursor = await conn.execute(
            """
            WITH requested AS (
                SELECT revision_id, ordinal
                FROM unnest(%s::bigint[]) WITH ORDINALITY AS items(revision_id, ordinal)
            )
            SELECT requested.revision_id
            FROM requested
            LEFT JOIN event_revision_processing_state state
              ON state.source_item_revision_id = requested.revision_id
            WHERE state.source_item_revision_id IS NULL OR state.status <> 'succeeded'
            ORDER BY requested.ordinal
            """,
            (list(revision_ids),),
        )
        return [int(row[0]) for row in await cursor.fetchall()]

    async def mark_running(
        self, conn: psycopg.AsyncConnection, revision_ids: Sequence[int]
    ) -> None:
        if not revision_ids:
            return
        await conn.execute(
            """
            INSERT INTO event_revision_processing_state (
                source_item_revision_id, status, attempt_count, started_at,
                completed_at, last_error_kind, processing_mode,
                reused_from_revision_id, updated_at
            )
            SELECT revision_id, 'running', 1, now(), NULL, NULL, 'full', NULL, now()
            FROM unnest(%s::bigint[]) AS revision_id
            ON CONFLICT (source_item_revision_id) DO UPDATE SET
                status = 'running',
                attempt_count = event_revision_processing_state.attempt_count + 1,
                last_error_kind = NULL,
                started_at = now(),
                completed_at = NULL,
                processing_mode = 'full',
                reused_from_revision_id = NULL,
                updated_at = now()
            """,
            (list(revision_ids),),
        )

    async def mark_succeeded(
        self, conn: psycopg.AsyncConnection, revision_ids: Sequence[int]
    ) -> None:
        if not revision_ids:
            return
        await conn.execute(
            """
            UPDATE event_revision_processing_state
            SET status = 'succeeded', completed_at = now(),
                last_error_kind = NULL, processing_mode = 'full',
                reused_from_revision_id = NULL, updated_at = now()
            WHERE source_item_revision_id = ANY(%s)
            """,
            (list(revision_ids),),
        )

    async def mark_failed(
        self,
        conn: psycopg.AsyncConnection,
        revision_ids: Sequence[int],
        *,
        error_kind: str,
    ) -> None:
        if not revision_ids:
            return
        await conn.execute(
            """
            UPDATE event_revision_processing_state
            SET status = 'failed', last_error_kind = %s,
                completed_at = now(), updated_at = now()
            WHERE source_item_revision_id = ANY(%s)
            """,
            (error_kind, list(revision_ids)),
        )

    async def mark_reused(
        self,
        conn: psycopg.AsyncConnection,
        *,
        revision_id: int,
        reused_from_revision_id: int,
    ) -> None:
        """Mark a revision complete by reusing its immediate predecessor's work."""
        await conn.execute(
            """
            INSERT INTO event_revision_processing_state (
                source_item_revision_id, status, attempt_count,
                last_error_kind, completed_at, processing_mode,
                reused_from_revision_id, updated_at
            )
            VALUES (%s, 'succeeded', 0, NULL, now(), 'reused', %s, now())
            ON CONFLICT (source_item_revision_id) DO UPDATE SET
                status = 'succeeded',
                last_error_kind = NULL,
                completed_at = now(),
                processing_mode = 'reused',
                reused_from_revision_id = EXCLUDED.reused_from_revision_id,
                updated_at = now()
            """,
            (revision_id, reused_from_revision_id),
        )

    async def get_state(
        self, conn: psycopg.AsyncConnection, revision_id: int
    ) -> RevisionProcessingState | None:
        cursor = await conn.execute(
            """
            SELECT source_item_revision_id, status, processing_mode,
                   reused_from_revision_id, completed_at
            FROM event_revision_processing_state
            WHERE source_item_revision_id = %s
            """,
            (revision_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return RevisionProcessingState(
            revision_id=int(row[0]),
            status=str(row[1]),
            processing_mode=str(row[2]),
            reused_from_revision_id=(int(row[3]) if row[3] is not None else None),
            completed_at=row[4],
        )
