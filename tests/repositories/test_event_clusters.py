"""Repository-level concurrency contracts for story cluster state."""

from __future__ import annotations

import datetime as dt

import pytest

from src.repositories.event_clusters import EventClusterRepository


@pytest.mark.unit
async def test_lock_cluster_state_uses_row_lock():
    now = dt.datetime.now(dt.timezone.utc)

    class Cursor:
        async def fetchone(self):
            return (7, [1.0, 0.0], "model", 2, 3, 2, now, now, 11, None, None, True, now)

    class Connection:
        def __init__(self):
            self.query = ""

        async def execute(self, query, params):
            self.query = query
            assert params == (7,)
            return Cursor()

    conn = Connection()
    state = await EventClusterRepository().lock_cluster_state(conn, 7)

    assert state is not None
    assert state.story_id == 7
    assert state.fragment_count == 3
    assert "FOR UPDATE" in conn.query
