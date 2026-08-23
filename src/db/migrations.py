"""SQL-first migration runner for the Telebrief domain schema.

Discovers numbered ``NNNN_name.sql`` files, applies each pending migration in
its own transaction (or outside a transaction when the file carries the
``-- telebrief: non-transactional`` header), and records applied versions in
the ``telebrief_schema_migrations`` ledger. The ledger bootstrap lives here
because it belongs to migration tooling only — normal application startup must
never create tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

MIGRATION_FILENAME_RE = re.compile(r"^(?P<version>\d+)_(?P<name>[A-Za-z0-9_]+)\.sql$")
NON_TRANSACTIONAL_HEADER = "-- telebrief: non-transactional"
LEDGER_TABLE = "telebrief_schema_migrations"


class MigrationError(ValueError):
    """Raised when a migrations directory cannot be processed."""


@dataclass(frozen=True)
class PendingMigration:
    """A single discovered migration file."""

    version: int
    name: str
    path: Path
    sql_text: str
    transactional: bool


async def domain_schema(conn: psycopg.AsyncConnection) -> str:
    """Return the leading schema from the connection's current search_path.

    Runs inside its own transaction so it never leaves an implicit
    transaction dangling on the caller's connection: a dangling transaction
    would downgrade every later conn.transaction() block to a mere savepoint
    and silently discard all migration commits.
    """
    async with conn.transaction():
        cursor = await conn.execute("SHOW search_path")
        row = await cursor.fetchone()
    raw = (row[0] if row else "") or "public"
    first = raw.split(",")[0].strip()
    if first.startswith('"') and first.endswith('"') and len(first) >= 2:
        first = first[1:-1].replace('""', '"')
    if first in ("", "$user"):
        return "public"
    return first


def discover_migrations(migrations_dir: Path) -> list[PendingMigration]:
    """Parse migration files, ordered by numeric prefix, rejecting duplicates."""
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")

    by_version: dict[int, PendingMigration] = {}
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file():
            continue
        match = MIGRATION_FILENAME_RE.match(path.name)
        if match is None:
            continue
        version = int(match.group("version"))
        sql_text = path.read_text(encoding="utf-8")
        header_line = next((line.strip() for line in sql_text.splitlines() if line.strip()), "")
        transactional = header_line != NON_TRANSACTIONAL_HEADER
        existing = by_version.get(version)
        if existing is not None:
            raise MigrationError(
                f"duplicate migration version {version}: {existing.path.name} and {path.name}"
            )
        by_version[version] = PendingMigration(
            version=version,
            name=path.name,
            path=path,
            sql_text=sql_text,
            transactional=transactional,
        )
    return [by_version[v] for v in sorted(by_version)]


async def migrate(conn: psycopg.AsyncConnection, migrations_dir: Path) -> int:
    """Apply pending migrations and return the resulting schema version.

    The target domain schema is taken from the connection's search_path. The
    schema and the migration ledger are bootstrapped with safely quoted
    identifiers before any numbered migration runs; each migration executes in
    exactly one transaction whose local search_path is pinned to the domain
    schema, and its version is recorded only after the SQL succeeds.
    """
    migrations = discover_migrations(migrations_dir)
    schema = await domain_schema(conn)

    async with conn.transaction():
        await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        await conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        await conn.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {}.{} ("
                "version integer PRIMARY KEY, "
                "name text NOT NULL, "
                "applied_at timestamptz NOT NULL DEFAULT now())"
            ).format(sql.Identifier(schema), sql.Identifier(LEDGER_TABLE))
        )

    applied = await _applied_versions(conn, schema)

    for migration in migrations:
        if migration.version in applied:
            continue
        if migration.transactional:
            async with conn.transaction():
                await _set_local_search_path(conn, schema)
                await conn.execute(migration.sql_text)
                await _record_version(conn, schema, migration)
        else:
            await _apply_non_transactional(conn, schema, migration)

    return await _current_version(conn, schema)


async def _apply_non_transactional(
    conn: psycopg.AsyncConnection, schema: str, migration: PendingMigration
) -> None:
    """Run one migration outside a transaction, then record it separately.

    Commits any prior transaction, switches to autocommit so statements such as
    ``CREATE INDEX CONCURRENTLY`` are legal, restores normal mode afterwards,
    then records the version in its own transaction. The SQL itself must be
    idempotent because execution and recording cannot share a transaction.
    """
    status = conn.info.transaction_status
    if status == TransactionStatus.INERROR:
        await conn.rollback()
    elif status == TransactionStatus.INTRANS:
        await conn.commit()

    previous_autocommit = conn.autocommit
    await conn.set_autocommit(True)
    try:
        cursor = await conn.execute("SELECT current_setting('search_path')")
        row = await cursor.fetchone()
        previous_search_path = (row[0] if row else "") or "public"
        await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        await conn.execute(migration.sql_text)
        # Restore the caller's session search_path rather than resetting:
        # unqualified queries issued after migrate() must keep resolving the
        # way they did before.
        await conn.execute("SELECT set_config('search_path', %s, false)", (previous_search_path,))
    finally:
        await conn.set_autocommit(previous_autocommit)

    async with conn.transaction():
        await _set_local_search_path(conn, schema)
        await _record_version(conn, schema, migration)


async def _applied_versions(conn: psycopg.AsyncConnection, schema: str) -> set[int]:
    async with conn.transaction():
        cursor = await conn.execute(
            sql.SQL("SELECT version FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(LEDGER_TABLE)
            )
        )
        rows = await cursor.fetchall()
    return {int(row[0]) for row in rows}


async def _current_version(conn: psycopg.AsyncConnection, schema: str) -> int:
    async with conn.transaction():
        cursor = await conn.execute(
            sql.SQL("SELECT max(version) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(LEDGER_TABLE)
            )
        )
        row = await cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def _record_version(
    conn: psycopg.AsyncConnection, schema: str, migration: PendingMigration
) -> None:
    await conn.execute(
        sql.SQL("INSERT INTO {}.{} (version, name) VALUES (%s, %s)").format(
            sql.Identifier(schema), sql.Identifier(LEDGER_TABLE)
        ),
        (migration.version, migration.name),
    )


async def _set_local_search_path(conn: psycopg.AsyncConnection, schema: str) -> None:
    await conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
