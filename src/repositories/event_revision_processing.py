"""Durable processing state for source revisions entering Event-First."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

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

    async def list_queueable(
        self, conn: psycopg.AsyncConnection, revision_ids: Sequence[int]
    ) -> list[int]:
        """Return revisions that may be queued without stealing a live claim."""
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
            WHERE state.source_item_revision_id IS NULL
               OR state.status IN ('pending', 'failed')
               OR (state.status = 'running' AND state.claim_expires_at <= now())
            ORDER BY requested.ordinal
            """,
            (list(revision_ids),),
        )
        return [int(row[0]) for row in await cursor.fetchall()]

    async def claim_revision_ids(
        self,
        conn: psycopg.AsyncConnection,
        revision_ids: Sequence[int],
        *,
        claim_token: UUID,
        lease_seconds: int,
        allow_succeeded: bool = False,
    ) -> list[int]:
        """Atomically acquire only revisions without a live processing owner."""
        if not revision_ids:
            return []
        await conn.execute(
            """
            INSERT INTO event_revision_processing_state (
                source_item_revision_id, status, attempt_count, started_at,
                completed_at, last_error_kind, processing_mode,
                reused_from_revision_id, updated_at
            )
            SELECT revision_id, 'pending', 0, NULL, NULL, NULL, 'full', NULL, now()
            FROM unnest(%s::bigint[]) AS revision_id
            ON CONFLICT (source_item_revision_id) DO NOTHING
            """,
            (list(revision_ids),),
        )
        cursor = await conn.execute(
            """
            UPDATE event_revision_processing_state
            SET status = 'running',
                attempt_count = attempt_count + 1,
                last_error_kind = NULL,
                started_at = now(),
                completed_at = NULL,
                processing_mode = 'full',
                reused_from_revision_id = NULL,
                claim_token = %s,
                claim_expires_at = now() + (%s * interval '1 second'),
                updated_at = now()
            WHERE source_item_revision_id = ANY(%s)
              AND (
                  status IN ('pending', 'failed')
                  OR (%s AND status = 'succeeded')
                  OR (status = 'running' AND claim_expires_at <= now())
              )
            RETURNING source_item_revision_id
            """,
            (claim_token, lease_seconds, allow_succeeded, list(revision_ids)),
        )
        claimed = {int(row[0]) for row in await cursor.fetchall()}
        return [revision_id for revision_id in revision_ids if revision_id in claimed]

    async def mark_running(
        self,
        conn: psycopg.AsyncConnection,
        revision_ids: Sequence[int],
        *,
        claim_token: UUID,
        lease_seconds: int,
    ) -> list[int]:
        """Compatibility name for callers migrating to the atomic claim API."""
        return await self.claim_revision_ids(
            conn,
            revision_ids,
            claim_token=claim_token,
            lease_seconds=lease_seconds,
            allow_succeeded=True,
        )

    async def mark_succeeded(
        self,
        conn: psycopg.AsyncConnection,
        revision_ids: Sequence[int],
        *,
        claim_token: UUID,
    ) -> None:
        if not revision_ids:
            return
        await conn.execute(
            """
            UPDATE event_revision_processing_state
            SET status = 'succeeded', completed_at = now(),
                last_error_kind = NULL, processing_mode = 'full',
                reused_from_revision_id = NULL, claim_token = NULL,
                claim_expires_at = NULL, updated_at = now()
            WHERE source_item_revision_id = ANY(%s)
              AND claim_token = %s
            """,
            (list(revision_ids), claim_token),
        )

    async def mark_failed(
        self,
        conn: psycopg.AsyncConnection,
        revision_ids: Sequence[int],
        *,
        claim_token: UUID,
        error_kind: str,
    ) -> None:
        if not revision_ids:
            return
        await conn.execute(
            """
            UPDATE event_revision_processing_state
            SET status = 'failed', last_error_kind = %s,
                completed_at = now(), claim_token = NULL,
                claim_expires_at = NULL, updated_at = now()
            WHERE source_item_revision_id = ANY(%s)
              AND claim_token = %s
            """,
            (error_kind, list(revision_ids), claim_token),
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
                claim_token = NULL,
                claim_expires_at = NULL,
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
