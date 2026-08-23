"""Fixtures for migration tests under tests/migration."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from src.config_loader import DatabaseConfig
from src.db.migrations import migrate
from src.db.pool import close_pool, open_pool
from src.db.uow import DatabaseUnitOfWork

REPO_ROOT = Path(__file__).parent.parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
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


@pytest.fixture(scope="session", autouse=True)
def ensure_test_env() -> None:
    if "TELEBRIEF_TEST_DATABASE_URL" not in os.environ:
        return
    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    os.environ["DATABASE_URL"] = url


@pytest.fixture(scope="session", autouse=True)
async def ensure_schema_migrations() -> None:
    if "TELEBRIEF_TEST_DATABASE_URL" not in os.environ:
        return
    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await migrate(conn, MIGRATIONS_DIR)


_TRUNCATE_TABLES = """
    TRUNCATE legacy_imported_messages, messages,
             source_item_revisions, source_items,
             sources, editions
    CASCADE;
"""


@pytest.fixture
async def conn(database_config: DatabaseConfig) -> AsyncIterator[psycopg.AsyncConnection]:
    conn = await psycopg.AsyncConnection.connect(database_config.url, autocommit=False)
    await conn.execute(_TRUNCATE_TABLES)
    await conn.commit()
    try:
        yield conn
    finally:
        await conn.execute(_TRUNCATE_TABLES)
        await conn.commit()
        await conn.close()


@pytest.fixture
async def pool(conn, database_config: DatabaseConfig) -> AsyncIterator[AsyncConnectionPool]:
    pool = await open_pool(database_config)
    try:
        yield pool
    finally:
        await close_pool(pool)


@pytest.fixture
def uow(pool: AsyncConnectionPool) -> DatabaseUnitOfWork:
    return DatabaseUnitOfWork(pool)


@pytest.fixture
async def edition(conn: psycopg.AsyncConnection) -> SimpleNamespace:
    cursor = await conn.execute(
        """
        INSERT INTO editions (slug, name, timezone, language, enabled)
        VALUES ('berdyansk', 'Бердянск', 'Europe/Kyiv', 'ru', true)
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
        RETURNING id, slug, name, timezone, language, enabled
        """
    )
    row = await cursor.fetchone()
    await conn.commit()
    return SimpleNamespace(
        id=row[0],
        slug=row[1],
        name=row[2],
        timezone=row[3],
        language=row[4],
        enabled=row[5],
    )
