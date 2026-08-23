"""Transactional-defer invariant: domain writes and Procrastinate deferrals.

Domain writes (a ``sources`` row) and job deferrals share one psycopg
connection, so they must become visible together after commit and disappear
together after rollback.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import procrastinate
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from src.config_loader import DatabaseConfig
from src.db.pool import close_pool, open_pool
from src.domain.sources import NewSource
from src.repositories.sources import SourceRepository

test_blueprint = procrastinate.Blueprint()


@test_blueprint.task(name="process_source")
async def process_source(source_id: int) -> None:
    """Probe task registered only by these tests; never executed (no worker)."""
    del source_id


@pytest.fixture
def jobs_import_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Environment so importing src.jobs.app works without repo-root DB config.

    The production module builds ``procrastinate_app`` at import time from
    ``load_database_config(require_enabled=True)``, so the test database URL is
    injected into DATABASE_URL and a minimal enabled config.yaml is provided
    from a temporary working directory before the first import.
    """
    url = os.environ.get("TELEBRIEF_TEST_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", url)
    (tmp_path / "config.yaml").write_text(
        "database:\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return url


@pytest.fixture
async def pool(database_config: DatabaseConfig) -> AsyncIterator[AsyncConnectionPool]:
    """Domain connection pool with search_path public,procrastinate,public."""
    domain_pool = await open_pool(database_config)
    try:
        yield domain_pool
    finally:
        await close_pool(domain_pool)


@pytest.fixture
async def jobs_app(
    jobs_import_env: str, procrastinate_schema_ready: None
) -> AsyncIterator[procrastinate.App]:
    """Opened Procrastinate app built through the production factory."""
    from src.jobs.app import build_app

    config = DatabaseConfig(
        enabled=True,
        url=jobs_import_env,
        min_pool_size=1,
        max_pool_size=4,
        domain_schema="public",
        procrastinate_schema="procrastinate",
    )
    app = build_app(config, blueprints=(test_blueprint,))
    async with app.open_async():
        yield app


@pytest.fixture
async def clean_tables(database_config: DatabaseConfig) -> AsyncIterator[None]:
    """Truncate domain tables and queued jobs around each test."""
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        database_config.url, autocommit=True
    )
    # Procrastinate's own DELETE trigger references its tables unqualified,
    # so cleanup needs the queue schema on the search path.
    await conn.execute("SET search_path TO public, procrastinate")
    try:
        await _clear_tables(conn)
        yield
        await _clear_tables(conn)
    finally:
        await conn.close()


async def _clear_tables(conn: psycopg.AsyncConnection) -> None:
    await conn.execute("TRUNCATE source_editions, sources, editions RESTART IDENTITY CASCADE")
    await conn.execute("DELETE FROM procrastinate.procrastinate_jobs")


@asynccontextmanager
async def _borrow(pool_or_conn: AsyncConnectionPool | psycopg.AsyncConnection):
    if isinstance(pool_or_conn, AsyncConnectionPool):
        async with pool_or_conn.connection() as conn:
            yield conn
    else:
        yield pool_or_conn


async def count_sources(pool_or_conn: AsyncConnectionPool | psycopg.AsyncConnection) -> int:
    async with _borrow(pool_or_conn) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM sources")
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])


async def count_procrastinate_jobs(
    pool_or_conn: AsyncConnectionPool | psycopg.AsyncConnection,
) -> int:
    """Count queued jobs; qualified because observer connections lack the search path."""
    async with _borrow(pool_or_conn) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM procrastinate.procrastinate_jobs")
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])


def _new_source() -> NewSource:
    return NewSource(platform="telegram", kind="channel", name="rollback-probe-source")


@pytest.mark.postgres
@pytest.mark.usefixtures("jobs_app", "clean_tables")
async def test_rollback_discards_source_and_deferred_job_together(
    pool: AsyncConnectionPool,
) -> None:
    sources = SourceRepository()

    async with pool.connection() as conn:
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                source = await sources.create(conn, _new_source())
                await process_source.configure(connection=conn).defer_async(source_id=source.id)
                raise RuntimeError("force rollback")

    assert await count_sources(pool) == 0
    assert await count_procrastinate_jobs(pool) == 0


@pytest.mark.postgres
@pytest.mark.usefixtures("jobs_app", "clean_tables")
async def test_commit_persists_source_and_deferred_job_together(
    pool: AsyncConnectionPool,
    pg_conn: psycopg.AsyncConnection,
) -> None:
    sources = SourceRepository()

    async with pool.connection() as conn:
        async with conn.transaction():
            source = await sources.create(conn, _new_source())
            await process_source.configure(connection=conn).defer_async(source_id=source.id)

            # Pre-commit, an outside connection sees neither the source nor the job.
            assert await count_sources(pg_conn) == 0
            assert await count_procrastinate_jobs(pg_conn) == 0

    # After commit both are visible to fresh connections.
    assert await count_sources(pool) == 1
    assert await count_procrastinate_jobs(pool) == 1


@pytest.mark.postgres
@pytest.mark.usefixtures("jobs_import_env")
async def test_ensure_schema_quotes_custom_identifier(database_config: DatabaseConfig) -> None:
    """The admin helper safely quotes custom schema identifiers."""
    from src.jobs.admin import ensure_schema

    schema_name = 'queue weird"schema'
    try:
        await ensure_schema(database_config.url, schema_name)
        async with await psycopg.AsyncConnection.connect(database_config.url) as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM pg_namespace WHERE nspname = %s", (schema_name,)
            )
            row = await cursor.fetchone()
            assert row is not None
            assert int(row[0]) == 1
    finally:
        async with await psycopg.AsyncConnection.connect(
            database_config.url, autocommit=True
        ) as conn:
            await conn.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    psycopg.sql.Identifier(schema_name)
                )
            )
