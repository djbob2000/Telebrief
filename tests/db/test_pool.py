"""Tests for the async psycopg connection pool with pgvector registration."""

from __future__ import annotations

import logging

import pytest
from psycopg.types import TypeInfo

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


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pool_logs_debug_when_vector_type_missing(database_config, caplog, monkeypatch):
    original_fetch = TypeInfo.fetch

    async def fetch_without_vector(conn, name):
        if name == "vector":
            return None
        return await original_fetch(conn, name)

    # pgvector's own registration raises when the type is absent; stub it out
    # so only src.db.pool's fallback branch is exercised.
    async def register_nothing(conn):
        return None

    monkeypatch.setattr(TypeInfo, "fetch", fetch_without_vector)
    monkeypatch.setattr("src.db.pool.register_vector_async", register_nothing)

    with caplog.at_level(logging.DEBUG, logger="src.db.pool"):
        pool = await open_pool(database_config)
        try:
            async with pool.connection():
                pass
        finally:
            await close_pool(pool)

    assert any(
        record.levelno == logging.DEBUG and "vector" in record.message.lower()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
