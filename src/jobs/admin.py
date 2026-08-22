"""Operational helpers for the Procrastinate PostgreSQL namespace.

Telebrief owns only the namespace itself; the official Procrastinate schema is
the sole owner of every table and function inside it.
"""

from __future__ import annotations

import psycopg
from procrastinate import PsycopgConnector
from procrastinate.schema import SchemaManager
from psycopg import sql

DEFAULT_SCHEMA = "procrastinate"


async def ensure_schema(
    database_url: str,
    schema_name: str = DEFAULT_SCHEMA,
) -> None:
    """Create the Procrastinate namespace if missing.

    The schema name is bound through ``psycopg.sql.Identifier`` so configured
    identifiers never reach SQL as interpolated text.
    """
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
        await conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema_name))
        )


async def official_tables_exist(database_url: str, schema_name: str = DEFAULT_SCHEMA) -> bool:
    """Return True when the official job table exists inside ``schema_name``."""
    async with await psycopg.AsyncConnection.connect(database_url) as conn:
        cursor = await conn.execute(
            "SELECT to_regclass(%s)", (f"{schema_name}.procrastinate_jobs",)
        )
        row = await cursor.fetchone()
        return row is not None and row[0] is not None


async def apply_official_schema(
    database_url: str,
    schema_name: str = DEFAULT_SCHEMA,
) -> None:
    """Apply the official Procrastinate schema into ``schema_name``.

    Uses procrastinate's own SchemaManager over a connector whose search path
    targets the namespace, exactly like ``procrastinate schema --apply`` does.
    """
    connector = PsycopgConnector(
        conninfo=database_url,
        kwargs={"options": f"-c search_path={schema_name}"},
    )
    await connector.open_async()
    try:
        await SchemaManager(connector).apply_schema_async()
    finally:
        await connector.close_async()


async def ensure_official_tables(
    database_url: str,
    schema_name: str = DEFAULT_SCHEMA,
) -> None:
    """Idempotently make sure the official tables exist in ``schema_name``."""
    await ensure_schema(database_url, schema_name)
    if not await official_tables_exist(database_url, schema_name):
        await apply_official_schema(database_url, schema_name)
