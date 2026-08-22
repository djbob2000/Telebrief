"""Fixtures for PostgreSQL-backed repository tests in tests/repositories.

Tests here are gated on TELEBRIEF_TEST_DATABASE_URL: when the variable is not
set, every test collected under tests/repositories is skipped instead of
failing.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

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


@pytest.fixture
async def repo_conn(database_config: DatabaseConfig):
    """Autocommit connection to a clean slice of the test database.

    Autocommit keeps the connection usable after expected constraint
    violations (the transaction never stays aborted). Only the three
    foundation tables are truncated (never telebrief_schema_migrations or
    collection_*); RESTART IDENTITY makes ids deterministic and every run
    re-runnable against the persistent database.
    """
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        database_config.url, autocommit=True
    )
    try:
        await conn.execute("TRUNCATE source_editions, sources, editions RESTART IDENTITY CASCADE")
        yield conn
    finally:
        await conn.execute("TRUNCATE source_editions, sources, editions RESTART IDENTITY CASCADE")
        await conn.close()
