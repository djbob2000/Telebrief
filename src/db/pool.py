"""Async psycopg connection pool for the Telebrief PostgreSQL domain store.

Every connection handed out by the pool gets pgvector types registered, a
session pinned to UTC, and a search path addressing both the Telebrief domain
schema and the Procrastinate queue schema. The shared search path matters
because Procrastinate jobs are deferred on externally-managed domain
connections (``Task.configure(connection=conn).defer_async(...)``).

pgvector 0.5.0 loads ``vector`` values into its own ``Vector`` wrapper, which
predates iteration support, so this module additionally registers loaders that
hand back plain ``list[float]`` on reads. Writes keep using pgvector's
dumpers unchanged.
"""

from __future__ import annotations

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector_async
from psycopg import sql
from psycopg.abc import Buffer
from psycopg.adapt import Loader
from psycopg.pq import Format
from psycopg.types import TypeInfo
from psycopg_pool import AsyncConnectionPool

from src.config_loader import DatabaseConfig

__all__ = ["close_pool", "open_pool"]


class _FloatListLoader(Loader):
    """Load ``vector`` values as ``list[float]`` (text format)."""

    format = Format.TEXT

    def load(self, data: Buffer) -> list[float]:
        if isinstance(data, memoryview):
            data = data.tobytes()
        return Vector.from_text(data.decode("utf8")).to_list()


class _FloatListBinaryLoader(Loader):
    """Load ``vector`` values as ``list[float]`` (binary format)."""

    format = Format.BINARY

    def load(self, data: Buffer) -> list[float]:
        return Vector.from_binary(data).to_list()


async def open_pool(config: DatabaseConfig) -> AsyncConnectionPool:
    """Open an async connection pool configured from ``config``."""

    async def _configure_connection(conn: psycopg.AsyncConnection) -> None:
        await register_vector_async(conn)
        info = await TypeInfo.fetch(conn, "vector")
        if info is not None:
            conn.adapters.register_loader(info.oid, _FloatListLoader)
            conn.adapters.register_loader(info.oid, _FloatListBinaryLoader)
        await conn.execute("SET TIME ZONE 'UTC'")
        await conn.execute(
            sql.SQL("SET search_path TO {}, {}, public").format(
                sql.Identifier(config.domain_schema),
                sql.Identifier(config.procrastinate_schema),
            )
        )
        await conn.commit()

    pool = AsyncConnectionPool(
        conninfo=config.url,
        min_size=config.min_pool_size,
        max_size=config.max_pool_size,
        open=False,
        configure=_configure_connection,
    )
    await pool.open()
    await pool.wait()
    return pool


async def close_pool(pool: AsyncConnectionPool) -> None:
    """Gracefully close the pool."""
    await pool.close()
