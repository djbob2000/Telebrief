"""Persistence for final publication-readiness failure notifications."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class PublicationFailureNotification:
    id: int
    refresh_run_id: int
    recipient_user_id: int
    failure_kind: str
    status: str
    attempt_count: int
    last_error: str | None


class PublicationNotificationRepository:
    """Repository with an idempotency boundary per intent/recipient/reason."""

    async def insert_new(
        self,
        conn: psycopg.AsyncConnection,
        *,
        refresh_run_id: int,
        recipient_user_ids: list[int],
        failure_kind: str,
    ) -> list[int]:
        recipients = list(dict.fromkeys(int(user_id) for user_id in recipient_user_ids))
        if not recipients:
            return []
        cursor = await conn.execute(
            """
            INSERT INTO publication_failure_notifications (
                refresh_run_id, recipient_user_id, failure_kind
            )
            SELECT %s, recipient_user_id, %s
            FROM unnest(%s::bigint[]) AS recipient_user_id
            ON CONFLICT (refresh_run_id, recipient_user_id, failure_kind) DO NOTHING
            RETURNING id
            """,
            (refresh_run_id, failure_kind, recipients),
        )
        return [int(row[0]) for row in await cursor.fetchall()]

    async def get(
        self,
        conn: psycopg.AsyncConnection,
        notification_id: int,
        *,
        for_update: bool = False,
    ) -> PublicationFailureNotification | None:
        query = """
            SELECT id, refresh_run_id, recipient_user_id, failure_kind,
                   status, attempt_count, last_error
            FROM publication_failure_notifications
            WHERE id = %s
        """
        if for_update:
            query += " FOR UPDATE"
        cursor = await conn.execute(query, (notification_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return PublicationFailureNotification(
            id=int(row[0]),
            refresh_run_id=int(row[1]),
            recipient_user_id=int(row[2]),
            failure_kind=str(row[3]),
            status=str(row[4]),
            attempt_count=int(row[5]),
            last_error=row[6],
        )

    async def list_dispatchable(
        self, conn: psycopg.AsyncConnection, *, limit: int = 100
    ) -> list[int]:
        """Return durable outbox rows that still need a delivery job."""
        cursor = await conn.execute(
            """
            SELECT id
            FROM publication_failure_notifications
            WHERE status IN ('pending', 'failed')
            ORDER BY updated_at ASC, id ASC
            LIMIT %s
            """,
            (limit,),
        )
        return [int(row[0]) for row in await cursor.fetchall()]

    async def mark_sent(
        self, conn: psycopg.AsyncConnection, *, notification_id: int, sent_at: dt.datetime
    ) -> None:
        await conn.execute(
            """
            UPDATE publication_failure_notifications
            SET status = 'sent', sent_at = %s, updated_at = now()
            WHERE id = %s
            """,
            (sent_at, notification_id),
        )

    async def mark_failed(
        self, conn: psycopg.AsyncConnection, *, notification_id: int, error: str
    ) -> None:
        await conn.execute(
            """
            UPDATE publication_failure_notifications
            SET status = 'failed', attempt_count = attempt_count + 1,
                last_error = %s, updated_at = now()
            WHERE id = %s
            """,
            (error[:1000], notification_id),
        )
