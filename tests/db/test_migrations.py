"""Tests for the SQL-first migration runner and schema compatibility gate."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from src.db.migrations import migrate
from src.db.schema_version import SchemaVersionError, require_schema_compatible

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"

# Probe versions use a high range so they can never collide with real
# migration versions recorded in a shared ledger.
PROBE_FIRST = 900003
PROBE_SECOND = 900004
PROBE_DUP_A = 900010
PROBE_OK = 900020
PROBE_BROKEN = 900021
PROBE_NT_TABLE = 900030
PROBE_NT_REBUILD = 900031


@pytest.mark.postgres
async def test_migrate_applies_each_version_once(pg_conn):
    version = await migrate(pg_conn, MIGRATIONS_DIR)
    assert version >= 5
    again = await migrate(pg_conn, MIGRATIONS_DIR)
    assert again == version


@pytest.mark.postgres
async def test_require_schema_compatible_returns_current_version(pg_conn):
    current = await require_schema_compatible(pg_conn, minimum=5, maximum=5)
    assert current >= 5


@pytest.mark.postgres
async def test_schema_compatibility_rejects_old_database(pg_conn):
    await pg_conn.execute("TRUNCATE telebrief_schema_migrations")
    with pytest.raises(SchemaVersionError):
        await require_schema_compatible(pg_conn, minimum=3, maximum=3)


@pytest.mark.postgres
async def test_schema_compatibility_rejects_newer_database(pg_conn):
    with pytest.raises(SchemaVersionError):
        await require_schema_compatible(pg_conn, minimum=1, maximum=2)


@pytest.mark.postgres
async def test_migrate_records_versions_in_order(isolated_pg_conn, tmp_path):
    (tmp_path / f"{PROBE_SECOND:06d}_second.sql").write_text(
        f"CREATE TABLE order_probe_{PROBE_SECOND} (id integer);\n"
        f"SELECT * FROM order_probe_{PROBE_FIRST};\n",
        encoding="utf-8",
    )
    (tmp_path / f"{PROBE_FIRST:06d}_first.sql").write_text(
        f"CREATE TABLE order_probe_{PROBE_FIRST} (id integer);\n",
        encoding="utf-8",
    )

    version = await migrate(isolated_pg_conn, tmp_path)

    assert version == PROBE_SECOND
    cursor = await isolated_pg_conn.execute(
        "SELECT version FROM telebrief_schema_migrations ORDER BY version"
    )
    rows = await cursor.fetchall()
    assert [row[0] for row in rows] == [PROBE_FIRST, PROBE_SECOND]


@pytest.mark.postgres
async def test_migrate_rejects_duplicate_versions(isolated_pg_conn, tmp_path):
    (tmp_path / f"{PROBE_DUP_A:06d}_alpha.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / f"{PROBE_DUP_A:06d}_beta.sql").write_text("SELECT 2;\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        await migrate(isolated_pg_conn, tmp_path)


@pytest.mark.postgres
async def test_failed_migration_is_not_recorded(isolated_pg_conn, tmp_path):
    (tmp_path / f"{PROBE_OK:06d}_create_probe.sql").write_text(
        "CREATE TABLE rollback_probe_ok (id integer);\n", encoding="utf-8"
    )
    (tmp_path / f"{PROBE_BROKEN:06d}_broken.sql").write_text(
        "THIS IS NOT VALID SQL;\n", encoding="utf-8"
    )

    with pytest.raises(psycopg.errors.SyntaxError):
        await migrate(isolated_pg_conn, tmp_path)

    cursor = await isolated_pg_conn.execute("SELECT to_regclass('rollback_probe_ok')")
    assert (await cursor.fetchone())[0] is not None
    cursor = await isolated_pg_conn.execute(
        "SELECT version FROM telebrief_schema_migrations ORDER BY version"
    )
    rows = await cursor.fetchall()
    assert [row[0] for row in rows] == [PROBE_OK]


@pytest.mark.postgres
async def test_non_transactional_migration_header(isolated_pg_conn, tmp_path):
    (tmp_path / f"{PROBE_NT_TABLE:06d}_table.sql").write_text(
        "CREATE TABLE nontransactional_probe (id integer);\n", encoding="utf-8"
    )
    (tmp_path / f"{PROBE_NT_REBUILD:06d}_rebuild.sql").write_text(
        "-- telebrief: non-transactional\n"
        "DROP TABLE IF EXISTS nontransactional_probe;\n"
        "CREATE TABLE nontransactional_probe (id integer);\n",
        encoding="utf-8",
    )

    version = await migrate(isolated_pg_conn, tmp_path)

    assert version == PROBE_NT_REBUILD
    cursor = await isolated_pg_conn.execute("SELECT count(*) FROM nontransactional_probe")
    assert (await cursor.fetchone())[0] == 0
    cursor = await isolated_pg_conn.execute(
        "SELECT version FROM telebrief_schema_migrations ORDER BY version"
    )
    rows = await cursor.fetchall()
    assert [row[0] for row in rows] == [PROBE_NT_TABLE, PROBE_NT_REBUILD]
