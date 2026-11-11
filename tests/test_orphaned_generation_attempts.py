# ruff: noqa: S101
"""Attempts left `running` by a stopped worker are closed when the run resumes."""

from __future__ import annotations

from typing import Any

import pytest

from src.publication.repository import PublicationRepository


class _Cursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _Conn:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, params: tuple[Any, ...]) -> _Cursor:
        self.calls.append((sql, params))
        return _Cursor(self.rowcount)


@pytest.mark.asyncio
async def test_orphaned_running_attempts_are_closed_for_the_run_only():
    conn = _Conn(rowcount=2)

    closed = await PublicationRepository.fail_orphaned_generation_attempts(
        None,  # type: ignore[arg-type]
        conn,  # type: ignore[arg-type]
        388,
    )

    assert closed == 2
    sql, params = conn.calls[0]
    assert "SET status = 'failed'" in sql
    assert "status = 'running'" in sql
    assert "publication_run_id = %s" in sql
    assert params == ("worker_interrupted", 388)


@pytest.mark.asyncio
async def test_nothing_to_close_returns_zero():
    conn = _Conn(rowcount=0)

    assert (
        await PublicationRepository.fail_orphaned_generation_attempts(
            None,  # type: ignore[arg-type]
            conn,  # type: ignore[arg-type]
            7,
        )
        == 0
    )
