"""Tests for the async psycopg connection pool with pgvector registration."""

from __future__ import annotations

import pytest

from src.db.pool import close_pool, open_pool


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pool_registers_pgvector(database_config):
    pool = await open_pool(database_config)
    try:
        async with pool.connection() as conn:
            row = await conn.execute("SELECT %s::vector AS v", ([1.0, 2.0, 3.0],))
            value = (await row.fetchone())[0]
            assert list(value) == [1.0, 2.0, 3.0]
    finally:
        await close_pool(pool)
