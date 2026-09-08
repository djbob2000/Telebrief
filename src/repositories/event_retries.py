"""Durable retry state for Event-First triage and analysis calls."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence, cast

import psycopg


@dataclass(frozen=True)
class EventProcessingRetryState:
    """Retry/quarantine state for one story assignment and processing stage."""

    story_id: int
    latest_assignment_id: int
    stage: str
    attempt_count: int
    next_retry_at: dt.datetime | None
    exhausted_at: dt.datetime | None
    last_error_kind: str | None
    last_prompt_hash: str | None
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def from_row(cls, row: Sequence[object]) -> "EventProcessingRetryState":
        return cls(
            story_id=cast(int, row[0]),
            latest_assignment_id=cast(int, row[1]),
            stage=str(row[2]),
            attempt_count=cast(int, row[3]),
            next_retry_at=cast(dt.datetime | None, row[4]),
            exhausted_at=cast(dt.datetime | None, row[5]),
            last_error_kind=str(row[6]) if row[6] is not None else None,
            last_prompt_hash=str(row[7]) if row[7] is not None else None,
            created_at=cast(dt.datetime, row[8]),
            updated_at=cast(dt.datetime, row[9]),
        )


class EventProcessingRetryRepository:
    """Persistence for assignment-scoped retry counters and backoff state."""

    _COLUMNS = (
        "story_id, latest_assignment_id, stage, attempt_count, next_retry_at, "
        "exhausted_at, last_error_kind, last_prompt_hash, created_at, updated_at"
    )

    async def record_failure(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        latest_assignment_id: int,
        stage: str,
        error_kind: str,
        next_retry_at: dt.datetime | None,
        exhausted: bool = False,
        prompt_hash: str | None = None,
    ) -> EventProcessingRetryState:
        """Atomically increment an assignment's failure count and store its disposition."""
        if stage not in {"triage", "analysis"}:
            raise ValueError(f"unsupported Event-First retry stage: {stage!r}")
        cursor = await conn.execute(
            f"""
            INSERT INTO story_event_processing_retries (
                story_id, latest_assignment_id, stage, attempt_count,
                next_retry_at, exhausted_at, last_error_kind, last_prompt_hash
            ) VALUES (%s, %s, %s, 1, %s, CASE WHEN %s THEN now() ELSE NULL END, %s, %s)
            ON CONFLICT (story_id, latest_assignment_id, stage) DO UPDATE SET
                attempt_count = story_event_processing_retries.attempt_count + 1,
                next_retry_at = EXCLUDED.next_retry_at,
                exhausted_at = CASE WHEN %s THEN now() ELSE NULL END,
                last_error_kind = EXCLUDED.last_error_kind,
                last_prompt_hash = EXCLUDED.last_prompt_hash,
                updated_at = now()
            RETURNING {self._COLUMNS}
            """,  # noqa: S608 -- fixed internal column list only
            (
                story_id,
                latest_assignment_id,
                stage,
                next_retry_at,
                exhausted,
                error_kind,
                prompt_hash,
                exhausted,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("record_failure produced no retry state")
        return EventProcessingRetryState.from_row(row)

    async def get_for_assignments(
        self,
        conn: psycopg.AsyncConnection,
        assignments: Sequence[tuple[int, int]],
        *,
        stage: str,
    ) -> dict[tuple[int, int], EventProcessingRetryState]:
        """Return retry state keyed by ``(story_id, latest_assignment_id)``."""
        if not assignments:
            return {}
        if stage not in {"triage", "analysis"}:
            raise ValueError(f"unsupported Event-First retry stage: {stage!r}")
        placeholders = ", ".join("(%s, %s)" for _ in assignments)
        params: list[object] = [value for pair in assignments for value in pair]
        params.append(stage)
        cursor = await conn.execute(
            f"""
            SELECT {self._COLUMNS}
            FROM story_event_processing_retries
            WHERE (story_id, latest_assignment_id) IN ({placeholders})
              AND stage = %s
            """,  # noqa: S608 -- placeholder count is generated from typed pairs
            params,
        )
        rows = await cursor.fetchall()
        return {
            (state.story_id, state.latest_assignment_id): state
            for state in (EventProcessingRetryState.from_row(row) for row in rows)
        }

    async def clear(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        latest_assignment_id: int,
        stage: str,
    ) -> None:
        """Clear state after successful processing of the current assignment."""
        await conn.execute(
            """
            DELETE FROM story_event_processing_retries
            WHERE story_id = %s AND latest_assignment_id = %s AND stage = %s
            """,  # noqa: S608 -- fixed internal column list only
            (story_id, latest_assignment_id, stage),
        )

    async def mark_exhausted(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        latest_assignment_id: int,
        stage: str,
        error_kind: str,
        prompt_hash: str | None = None,
    ) -> EventProcessingRetryState:
        """Mark an existing assignment as quarantined without resetting its count."""
        cursor = await conn.execute(
            f"""
            UPDATE story_event_processing_retries
            SET exhausted_at = COALESCE(exhausted_at, now()),
                next_retry_at = NULL,
                last_error_kind = %s,
                last_prompt_hash = %s,
                updated_at = now()
            WHERE story_id = %s AND latest_assignment_id = %s AND stage = %s
            RETURNING {self._COLUMNS}
            """,  # noqa: S608 -- fixed internal column list only
            (error_kind, prompt_hash, story_id, latest_assignment_id, stage),
        )
        row = await cursor.fetchone()
        if row is None:
            return await self.record_failure(
                conn,
                story_id=story_id,
                latest_assignment_id=latest_assignment_id,
                stage=stage,
                error_kind=error_kind,
                next_retry_at=None,
                exhausted=True,
                prompt_hash=prompt_hash,
            )
        return EventProcessingRetryState.from_row(row)
