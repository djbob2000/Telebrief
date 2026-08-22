"""Tests for the DatabaseUnitOfWork transaction helper."""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from src.db.pool import close_pool, open_pool
from src.db.uow import DatabaseUnitOfWork
from src.domain.sources import NewSource
from src.repositories.sources import SourceRepository

NEW_SOURCE = NewSource(
    platform="telegram",
    kind="channel",
    external_id="-100100",
    url="https://t.me/news",
    name="UoW News",
    role="community",
)


@pytest.fixture
async def truncate_sources(database_config):
    """Leave the sources table empty before and after each test."""
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        database_config.url, autocommit=True
    )
    try:
        await conn.execute("TRUNCATE source_editions, sources, editions RESTART IDENTITY CASCADE")
        yield
    finally:
        await conn.execute("TRUNCATE source_editions, sources, editions RESTART IDENTITY CASCADE")
        await conn.close()


@pytest.fixture
async def pool(truncate_sources, database_config):
    pool = await open_pool(database_config)
    try:
        yield pool
    finally:
        await close_pool(pool)


@pytest.fixture
def uow(pool) -> DatabaseUnitOfWork:
    return DatabaseUnitOfWork(pool)


@pytest.fixture
def source_repo() -> SourceRepository:
    return SourceRepository()


async def _source_count(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute("SELECT count(*) FROM sources")
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_uow_rolls_back_on_exception(uow, source_repo):
    with pytest.raises(ValueError):
        async with uow.transaction() as conn:
            await source_repo.create(conn, NEW_SOURCE)
            raise ValueError("boom")
    assert await _source_count(uow.pool) == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_uow_commits_on_clean_exit_and_yields_pooled_connection(uow, source_repo):
    async with uow.transaction() as conn:
        assert isinstance(conn, psycopg.AsyncConnection)
        created = await source_repo.create(conn, NEW_SOURCE)
        assert created.id > 0

    assert await _source_count(uow.pool) == 1

    async with uow.pool.connection() as other_conn:
        cursor = await other_conn.execute(
            "SELECT platform, name FROM sources WHERE id = %s", (created.id,)
        )
        row = await cursor.fetchone()
    assert row is not None
    assert tuple(row) == ("telegram", "UoW News")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_uow_transaction_is_reusable_across_blocks(uow, source_repo):
    async with uow.transaction() as first_conn:
        await source_repo.create(first_conn, NEW_SOURCE)

    renamed = NewSource(
        platform="viber",
        kind="channel",
        name="UoW Second",
    )
    async with uow.transaction() as second_conn:
        await source_repo.create(second_conn, renamed)

    assert await _source_count(uow.pool) == 2
