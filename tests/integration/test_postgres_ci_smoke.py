"""Smoke tests for CI environment proving PostgreSQL, pgvector, and Procrastinate (Task 13)."""

from __future__ import annotations

import os

import psycopg
import pytest
from pgvector.psycopg import register_vector_async

from src.config_loader import DatabaseConfig
from src.db.schema_version import require_schema_compatible


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_pgvector_extension_active(database_config: DatabaseConfig):
    """Verify PostgreSQL connection and pgvector vector type operations."""
    assert "TELEBRIEF_TEST_DATABASE_URL" in os.environ or "DATABASE_URL" in os.environ

    async with await psycopg.AsyncConnection.connect(database_config.url, autocommit=True) as conn:
        await register_vector_async(conn)
        cur = await conn.execute("SELECT '[1.0, 2.0, 3.0]'::vector AS v")
        row = await cur.fetchone()
        assert row is not None
        assert row[0].to_list() == [1.0, 2.0, 3.0]

        # Verify distance operators
        cur = await conn.execute(
            "SELECT '[1.0, 0.0]'::vector <=> '[0.0, 1.0]'::vector AS cosine_dist"
        )
        dist = (await cur.fetchone())[0]
        assert dist == pytest.approx(1.0)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_database_migrations_applied_and_compatible(database_config: DatabaseConfig):
    """Verify standard schema migrations are applied and compatible with the application."""
    from src.bootstrap import SCHEMA_VERSION_MAXIMUM, SCHEMA_VERSION_MINIMUM

    async with await psycopg.AsyncConnection.connect(database_config.url, autocommit=True) as conn:
        current_version = await require_schema_compatible(
            conn, minimum=SCHEMA_VERSION_MINIMUM, maximum=SCHEMA_VERSION_MAXIMUM
        )
        assert current_version >= SCHEMA_VERSION_MINIMUM
