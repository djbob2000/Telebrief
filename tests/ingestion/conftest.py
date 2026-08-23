"""Fixtures for PostgreSQL-backed ingestion tests in tests/ingestion.

Tests here are gated on TELEBRIEF_TEST_DATABASE_URL: when the variable is not
set, every test collected under tests/ingestion is skipped instead of failing.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import procrastinate
import psycopg
import pytest

from src.config_loader import DatabaseConfig
from src.db.pool import close_pool, open_pool
from src.db.uow import DatabaseUnitOfWork


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests in this directory unless a test database is configured."""
    del config
    gate = Path(__file__).parent.resolve()
    for item in items:
        if item.path.resolve().is_relative_to(gate):
            item.add_marker(
                pytest.mark.skipif(
                    "TELEBRIEF_TEST_DATABASE_URL" not in os.environ,
                    reason="TELEBRIEF_TEST_DATABASE_URL is not set",
                )
            )


_TRUNCATE_TABLES = """
    TRUNCATE source_items, source_item_revisions, source_assets,
             source_item_state_events, collection_checkpoints,
             collection_runs, source_editions, sources, editions
    RESTART IDENTITY CASCADE
"""


async def _clear_slice(conn: psycopg.AsyncConnection) -> None:
    await conn.execute(_TRUNCATE_TABLES)
    # Procrastinate's own DELETE trigger references its tables unqualified,
    # so cleanup needs the queue schema on the search path.
    await conn.execute("SET search_path TO public, procrastinate")
    await conn.execute("DELETE FROM procrastinate.procrastinate_jobs")


@pytest.fixture
async def conn(database_config: DatabaseConfig):
    """Autocommit connection to a clean slice of the test database.

    Autocommit keeps the connection usable after expected constraint
    violations (the transaction never stays aborted). Binding rows are
    truncated so the CASCADE reaches every ingestion child table; RESTART
    IDENTITY makes ids deterministic and every run re-runnable against the
    persistent database.
    """
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        database_config.url, autocommit=True
    )
    try:
        await _clear_slice(conn)
        yield conn
    finally:
        await _clear_slice(conn)
        await conn.close()


@pytest.fixture
async def pool(conn, database_config: DatabaseConfig):
    """Pooled connections over the same truncated slice as ``conn``."""
    pool = await open_pool(database_config)
    try:
        yield pool
    finally:
        await close_pool(pool)


@pytest.fixture
def uow(pool) -> DatabaseUnitOfWork:
    return DatabaseUnitOfWork(pool)


@pytest.fixture
async def source(conn: psycopg.AsyncConnection) -> SimpleNamespace:
    """One registered telegram channel; exposes ``id`` for item inserts."""
    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name)
        VALUES ('telegram', 'channel', '-1004242', 'https://t.me/example', 'Example')
        RETURNING id
        """
    )
    row = await cursor.fetchone()
    return SimpleNamespace(id=row[0])


@pytest.fixture
async def edition(conn: psycopg.AsyncConnection) -> SimpleNamespace:
    """One registered edition for relevance-policy bindings."""
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk', 'Berdyansk') RETURNING id"
    )
    row = await cursor.fetchone()
    return SimpleNamespace(id=row[0])


@pytest.fixture(scope="session")
def procrastinate_schema_ready() -> None:
    """Ensure the official Procrastinate tables exist in the test database."""
    import asyncio

    from src.jobs.admin import ensure_official_tables

    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    asyncio.run(ensure_official_tables(url, "procrastinate"))


@pytest.fixture
def jobs_import_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Environment so importing src.jobs.app works without repo-root DB config.

    The production module builds ``procrastinate_app`` at import time from
    ``load_database_config(require_enabled=True)``, so the test database URL is
    injected into DATABASE_URL and a minimal enabled config.yaml is provided
    from a temporary working directory before the first import.
    """
    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", url)
    (tmp_path / "config.yaml").write_text(
        "database:\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return url


@pytest.fixture
async def production_jobs_app(
    jobs_import_env: str, procrastinate_schema_ready: None
) -> AsyncIterator[procrastinate.App]:
    """Opened production Procrastinate app bound to the test database.

    Production tasks (evaluate_relevance) are registered against the singleton
    app in src.jobs.app; opening that singleton lets ingestion defer real jobs.
    """
    from src.jobs.app import procrastinate_app

    async with procrastinate_app.open_async():
        yield procrastinate_app
