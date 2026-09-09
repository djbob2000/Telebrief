"""Durable repository for pre-publication collection and processing readiness."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg


@dataclass(frozen=True)
class PublicationRefreshRun:
    """A durable readiness barrier for one edition/publication slot."""

    id: int
    edition_id: int
    publication_type: str
    slot_at: dt.datetime
    requested_at: dt.datetime
    normal_source_cutoff_at: dt.datetime
    fallback_snapshot_at: dt.datetime
    deadline_at: dt.datetime
    status: str
    collection_ready_at: dt.datetime | None
    processing_ready_at: dt.datetime | None
    prepared_at: dt.datetime | None
    publication_run_id: int | None
    fallback_used: bool
    error_kind: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> PublicationRefreshRun:
        return cls(
            id=int(row[0]),
            edition_id=int(row[1]),
            publication_type=str(row[2]),
            slot_at=row[3],
            requested_at=row[4],
            normal_source_cutoff_at=row[5],
            fallback_snapshot_at=row[6],
            deadline_at=row[7],
            status=str(row[8]),
            collection_ready_at=row[9],
            processing_ready_at=row[10],
            prepared_at=row[11],
            publication_run_id=int(row[12]) if row[12] is not None else None,
            fallback_used=bool(row[13]),
            error_kind=row[14],
            metadata=row[15] if isinstance(row[15], dict) else {},
        )


@dataclass(frozen=True)
class PublicationRefreshSource:
    """Readiness state for one source participating in a refresh run."""

    refresh_run_id: int
    source_id: int
    required_since_at: dt.datetime
    status: str
    collection_run_id: int | None
    collection_outcome: str | None
    last_enqueue_attempt_at: dt.datetime | None
    completed_at: dt.datetime | None

    @classmethod
    def from_row(cls, row: Any) -> PublicationRefreshSource:
        return cls(
            refresh_run_id=int(row[0]),
            source_id=int(row[1]),
            required_since_at=row[2],
            status=str(row[3]),
            collection_run_id=int(row[4]) if row[4] is not None else None,
            collection_outcome=row[5],
            last_enqueue_attempt_at=row[6],
            completed_at=row[7],
        )


class PublicationReadinessRepository:
    """SQL authority for refresh-run state transitions and source barriers."""

    _RUN_SELECT = """
        SELECT
        id, edition_id, publication_type, slot_at, requested_at,
        normal_source_cutoff_at, fallback_snapshot_at, deadline_at, status,
        collection_ready_at, processing_ready_at, prepared_at,
        publication_run_id, fallback_used, error_kind, metadata
        FROM publication_refresh_runs
    """
    _SOURCE_SELECT = """
        SELECT
        refresh_run_id, source_id, required_since_at, status,
        collection_run_id, collection_outcome, last_enqueue_attempt_at,
        completed_at
        FROM publication_refresh_sources
    """

    async def get_or_create_refresh_run(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        publication_type: str,
        slot_at: dt.datetime,
        requested_at: dt.datetime,
        normal_source_cutoff_at: dt.datetime,
        fallback_snapshot_at: dt.datetime,
        deadline_at: dt.datetime,
        source_ids: Sequence[int],
    ) -> PublicationRefreshRun:
        """Create the refresh barrier once and add any newly bound sources."""
        cursor = await conn.execute(
            """
            INSERT INTO publication_refresh_runs (
                edition_id, publication_type, slot_at, requested_at,
                normal_source_cutoff_at, fallback_snapshot_at, deadline_at,
                status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'collecting')
            ON CONFLICT (edition_id, publication_type, slot_at)
            DO UPDATE SET updated_at = now()
            RETURNING id, edition_id, publication_type, slot_at, requested_at,
                      normal_source_cutoff_at, fallback_snapshot_at, deadline_at,
                      status, collection_ready_at, processing_ready_at, prepared_at,
                      publication_run_id, fallback_used, error_kind, metadata
            """,
            (
                edition_id,
                publication_type,
                slot_at,
                requested_at,
                normal_source_cutoff_at,
                fallback_snapshot_at,
                deadline_at,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("refresh run insert returned no row")
        refresh = PublicationRefreshRun.from_row(row)

        unique_source_ids = list(dict.fromkeys(int(source_id) for source_id in source_ids))
        if unique_source_ids:
            await conn.execute(
                """
                INSERT INTO publication_refresh_sources (
                    refresh_run_id, source_id, required_since_at
                )
                SELECT %s, source_id, %s
                FROM unnest(%s::bigint[]) AS source_id
                ON CONFLICT (refresh_run_id, source_id) DO NOTHING
                """,
                (refresh.id, requested_at, unique_source_ids),
            )
        return refresh

    async def get_refresh_run(
        self,
        conn: psycopg.AsyncConnection,
        refresh_run_id: int,
        *,
        for_update: bool = False,
    ) -> PublicationRefreshRun | None:
        query = self._RUN_SELECT + "\nWHERE id = %s"
        if for_update:
            query += "\nFOR UPDATE"
        cursor = await conn.execute(query, (refresh_run_id,))
        row = await cursor.fetchone()
        return PublicationRefreshRun.from_row(row) if row is not None else None

    async def list_reconcilable_refresh_runs(
        self, conn: psycopg.AsyncConnection
    ) -> list[PublicationRefreshRun]:
        cursor = await conn.execute(
            self._RUN_SELECT
            + """
            WHERE status IN (
                'collecting', 'processing', 'ready_for_preparation', 'fallback_ready'
            )
            ORDER BY slot_at, id
            """
        )
        return [PublicationRefreshRun.from_row(row) for row in await cursor.fetchall()]

    async def list_refresh_sources(
        self, conn: psycopg.AsyncConnection, refresh_run_id: int
    ) -> list[PublicationRefreshSource]:
        cursor = await conn.execute(
            self._SOURCE_SELECT
            + """
            WHERE refresh_run_id = %s
            ORDER BY source_id
            """,
            (refresh_run_id,),
        )
        return [PublicationRefreshSource.from_row(row) for row in await cursor.fetchall()]

    async def mark_source_enqueue_attempt(
        self,
        conn: psycopg.AsyncConnection,
        *,
        refresh_run_id: int,
        source_id: int,
        attempted_at: dt.datetime,
    ) -> None:
        await conn.execute(
            """
            UPDATE publication_refresh_sources
            SET last_enqueue_attempt_at = %s, updated_at = now()
            WHERE refresh_run_id = %s AND source_id = %s
            """,
            (attempted_at, refresh_run_id, source_id),
        )

    async def reconcile_qualifying_collection_runs(
        self, conn: psycopg.AsyncConnection, refresh_run_id: int
    ) -> list[PublicationRefreshSource]:
        """Attach the best qualifying scan, retaining success over later failures."""
        sources = await self.list_refresh_sources(conn, refresh_run_id)
        for source in sources:
            success_cursor = await conn.execute(
                """
                SELECT id, status, completed_at
                FROM collection_runs
                WHERE source_id = %s
                  AND started_at >= %s
                  AND status = 'success'
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC, id DESC
                LIMIT 1
                """,
                (source.source_id, source.required_since_at),
            )
            success = await success_cursor.fetchone()
            if success is not None:
                await conn.execute(
                    """
                    UPDATE publication_refresh_sources
                    SET status = 'succeeded', collection_run_id = %s,
                        collection_outcome = %s, completed_at = %s, updated_at = now()
                    WHERE refresh_run_id = %s AND source_id = %s
                    """,
                    (
                        success[0],
                        success[1],
                        success[2],
                        refresh_run_id,
                        source.source_id,
                    ),
                )
                continue

            latest_cursor = await conn.execute(
                """
                SELECT id, status, completed_at
                FROM collection_runs
                WHERE source_id = %s
                  AND started_at >= %s
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC, id DESC
                LIMIT 1
                """,
                (source.source_id, source.required_since_at),
            )
            latest = await latest_cursor.fetchone()
            if latest is not None:
                await conn.execute(
                    """
                    UPDATE publication_refresh_sources
                    SET status = CASE WHEN %s IN ('success', 'skipped')
                                      THEN 'succeeded' ELSE 'degraded' END,
                        collection_run_id = %s, collection_outcome = %s,
                        completed_at = %s, updated_at = now()
                    WHERE refresh_run_id = %s AND source_id = %s
                    """,
                    (
                        latest[1],
                        latest[0],
                        latest[1],
                        latest[2],
                        refresh_run_id,
                        source.source_id,
                    ),
                )

        return await self.list_refresh_sources(conn, refresh_run_id)

    async def count_unprocessed_refresh_revisions(
        self, conn: psycopg.AsyncConnection, refresh_run_id: int
    ) -> int:
        cursor = await conn.execute(
            """
            SELECT COUNT(*)
            FROM publication_refresh_sources prs
            JOIN source_item_revisions sir
              ON sir.collection_run_id = prs.collection_run_id
            LEFT JOIN event_revision_processing_state erps
              ON erps.source_item_revision_id = sir.id
            WHERE prs.refresh_run_id = %s
              AND prs.status = 'succeeded'
              AND COALESCE(erps.status, 'missing') <> 'succeeded'
            """,
            (refresh_run_id,),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0

    async def transition_refresh(
        self,
        conn: psycopg.AsyncConnection,
        refresh_run_id: int,
        *,
        status: str,
        error_kind: str | None = None,
        collection_ready_at: dt.datetime | None = None,
        processing_ready_at: dt.datetime | None = None,
    ) -> None:
        await conn.execute(
            """
            UPDATE publication_refresh_runs
            SET status = %s,
                error_kind = COALESCE(%s, error_kind),
                collection_ready_at = COALESCE(%s, collection_ready_at),
                processing_ready_at = COALESCE(%s, processing_ready_at),
                updated_at = now()
            WHERE id = %s
            """,
            (
                status,
                error_kind,
                collection_ready_at,
                processing_ready_at,
                refresh_run_id,
            ),
        )

    async def mark_preparing(
        self,
        conn: psycopg.AsyncConnection,
        *,
        refresh_run_id: int,
        fallback_used: bool,
    ) -> PublicationRefreshRun | None:
        cursor = await conn.execute(
            """
            UPDATE publication_refresh_runs
            SET status = 'preparing', fallback_used = %s, updated_at = now()
            WHERE id = %s
              AND status IN ('ready_for_preparation', 'fallback_ready')
            RETURNING id, edition_id, publication_type, slot_at, requested_at,
                      normal_source_cutoff_at, fallback_snapshot_at, deadline_at,
                      status, collection_ready_at, processing_ready_at, prepared_at,
                      publication_run_id, fallback_used, error_kind, metadata
            """,
            (fallback_used, refresh_run_id),
        )
        row = await cursor.fetchone()
        return PublicationRefreshRun.from_row(row) if row is not None else None

    async def mark_publication_queued(
        self,
        conn: psycopg.AsyncConnection,
        *,
        refresh_run_id: int,
        publication_run_id: int,
        prepared_at: dt.datetime,
    ) -> None:
        await conn.execute(
            """
            UPDATE publication_refresh_runs
            SET status = 'publication_queued', publication_run_id = %s,
                prepared_at = %s, updated_at = now()
            WHERE id = %s
            """,
            (publication_run_id, prepared_at, refresh_run_id),
        )
