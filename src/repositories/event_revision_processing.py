"""Durable processing state for source revisions entering Event-First."""

from __future__ import annotations

from collections.abc import Sequence

import psycopg


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
                completed_at, last_error_kind, updated_at
            )
            SELECT revision_id, 'running', 1, now(), NULL, NULL, now()
            FROM unnest(%s::bigint[]) AS revision_id
            ON CONFLICT (source_item_revision_id) DO UPDATE SET
                status = 'running',
                attempt_count = event_revision_processing_state.attempt_count + 1,
                last_error_kind = NULL,
                started_at = now(),
                completed_at = NULL,
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
                last_error_kind = NULL, updated_at = now()
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
