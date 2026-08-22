"""Fixtures for PostgreSQL-backed tests in tests/db.

Tests here are gated on TELEBRIEF_TEST_DATABASE_URL: when the variable is not
set, every test collected under tests/db is skipped instead of failing.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from src.config_loader import DatabaseConfig


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


@pytest.fixture(scope="session")
def procrastinate_schema_ready() -> None:
    """Ensure the official Procrastinate tables exist in the test database.

    Idempotent: creates the configured namespace if needed (identifier-quoted
    via src.jobs.admin) and applies the official schema through procrastinate's
    own library API only when its tables are missing. Runs once per session,
    before any test that requests it.
    """
    import asyncio

    from src.jobs.admin import ensure_official_tables

    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    asyncio.run(ensure_official_tables(url, "procrastinate"))


@pytest.fixture
def database_config() -> DatabaseConfig:
    """DatabaseConfig pointing at the persistent PostgreSQL test database."""
    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    return DatabaseConfig(
        enabled=True,
        url=url,
        min_pool_size=1,
        max_pool_size=4,
        domain_schema="public",
        procrastinate_schema="procrastinate",
    )


@pytest.fixture
async def pg_conn(database_config: DatabaseConfig):
    """Async connection to the test database.

    Not autocommit: anything a test leaves uncommitted (for example the
    TRUNCATE in the compatibility test) is rolled back on teardown so the
    persistent database keeps its applied migration ledger.
    """
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(database_config.url)
    try:
        yield conn
    finally:
        await conn.rollback()
        await conn.close()


@pytest.fixture
async def isolated_pg_conn(database_config: DatabaseConfig):
    """Autocommit connection scoped to a disposable schema with its own ledger.

    migrate() derives the target schema from search_path, so pointing this
    connection at a throwaway schema gives migration-probe tests an isolated
    telebrief_schema_migrations table that never collides with the real one.
    """
    schema_name = f"telebrief_test_{uuid.uuid4().hex[:10]}"
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        database_config.url, autocommit=True
    )
    await conn.execute(
        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
    )
    await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
    await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
    try:
        yield conn
    finally:
        await conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
        )
        await conn.close()
