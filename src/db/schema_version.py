"""Schema compatibility gate for the Telebrief PostgreSQL domain store."""

from __future__ import annotations

import psycopg
from psycopg import sql

from src.db.migrations import LEDGER_TABLE, domain_schema


class SchemaVersionError(RuntimeError):
    """Raised when the database schema version is outside the supported range."""


async def require_schema_compatible(
    conn: psycopg.AsyncConnection, *, minimum: int, maximum: int
) -> int:
    """Verify the applied schema version lies within [minimum, maximum].

    Returns the current schema version. Raises SchemaVersionError when the
    migration ledger is missing or reports a version outside the range.
    """
    if minimum > maximum:
        raise ValueError(f"minimum ({minimum}) must not exceed maximum ({maximum})")
    schema = await domain_schema(conn)
    try:
        async with conn.transaction():
            cursor = await conn.execute(
                sql.SQL("SELECT max(version) FROM {}.{}").format(
                    sql.Identifier(schema), sql.Identifier(LEDGER_TABLE)
                )
            )
            row = await cursor.fetchone()
    except psycopg.errors.UndefinedTable as exc:
        raise SchemaVersionError(
            f"migration ledger {schema}.{LEDGER_TABLE} not found; run scripts/migrate.py first"
        ) from exc
    current = int(row[0]) if row and row[0] is not None else 0
    if not minimum <= current <= maximum:
        raise SchemaVersionError(
            f"database schema version {current} is outside the supported range "
            f"[{minimum}, {maximum}]"
        )
    return current
