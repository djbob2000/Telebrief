"""Queries over rich Event-First analysis calls used by orchestration guards."""

from __future__ import annotations

import datetime as dt

import psycopg


class EventAnalysisRunRepository:
    """Read-only budget queries over succeeded and failed analysis attempts."""

    async def count_calls_since(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        since: dt.datetime,
    ) -> int:
        cursor = await conn.execute(
            """
            SELECT COUNT(*)
            FROM story_event_analysis_runs
            WHERE story_id = %s AND started_at >= %s
            """,
            (story_id, since),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row is not None else 0
