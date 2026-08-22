"""Fixtures for PostgreSQL-backed ingestion tests in tests/ingestion.

Tests here are gated on TELEBRIEF_TEST_DATABASE_URL: when the variable is not
set, every test collected under tests/ingestion is skipped instead of failing.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

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
        await conn.execute("TRUNCATE source_editions, sources, editions RESTART IDENTITY CASCADE")
        yield conn
    finally:
        await conn.execute("TRUNCATE source_editions, sources, editions RESTART IDENTITY CASCADE")
        await conn.close()


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
