"""Tests for deferred legacy messages drop and finalize cutover script (Task 11)."""

from __future__ import annotations

import pathlib

import psycopg
import pytest

from scripts.finalize_legacy_cutover import finalize_cutover
from src.config_loader import DatabaseConfig
from src.db.migrations import discover_migrations, migrate

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
MIGRATION_10_SQL = (MIGRATIONS_DIR / "0010_legacy_import_tracking.sql").read_text()


async def _restore_canonical_legacy_tables(conn: psycopg.AsyncConnection) -> None:
    await conn.execute("DROP TABLE IF EXISTS legacy_imported_messages CASCADE")
    await conn.execute("DROP TABLE IF EXISTS messages CASCADE")
    await conn.execute(MIGRATION_10_SQL)
    await conn.execute("DELETE FROM telebrief_schema_migrations WHERE version = 11")


@pytest.mark.postgres
class TestLegacyCutoverSafety:
    async def test_discover_migrations_does_not_include_deferred(self):
        migrations = discover_migrations(MIGRATIONS_DIR)
        versions = [m.version for m in migrations]
        # Version 11 is now deferred, so it shouldn't be in normal migrations directory
        assert 11 not in versions
        assert all(m.path.parent == MIGRATIONS_DIR for m in migrations)

    async def test_normal_migrate_does_not_drop_messages_table(
        self, conn: psycopg.AsyncConnection, database_config: DatabaseConfig
    ):
        try:
            await conn.execute("DROP TABLE IF EXISTS messages CASCADE")
            # Create a mock legacy messages table
            await conn.execute(
                """
                CREATE TABLE messages (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    channel_name TEXT NOT NULL,
                    text TEXT,
                    date TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                "INSERT INTO messages (channel_name, text) VALUES ('@chan', 'hello')"
            )

            # Run normal migrate
            await migrate(conn, MIGRATIONS_DIR)

            # Verify messages table still exists and data intact
            cur = await conn.execute("SELECT count(*) FROM messages")
            count = (await cur.fetchone())[0]
            assert count == 1
        finally:
            await _restore_canonical_legacy_tables(conn)

    async def test_finalize_cutover_fails_if_tracking_table_missing(
        self, conn: psycopg.AsyncConnection, database_config: DatabaseConfig
    ):
        try:
            await conn.execute("DROP TABLE IF EXISTS messages CASCADE")
            await conn.execute("DROP TABLE IF EXISTS legacy_imported_messages CASCADE")

            await conn.execute(
                """
                CREATE TABLE messages (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    channel_name TEXT NOT NULL,
                    text TEXT
                )
                """
            )
            await conn.execute("INSERT INTO messages (channel_name, text) VALUES ('@chan', 'msg1')")

            # finalize_cutover should fail with code 1
            rc = await finalize_cutover(database_config.url, domain_schema="public")
            assert rc == 1

            # messages table still exists
            cur = await conn.execute("SELECT count(*) FROM messages")
            assert (await cur.fetchone())[0] == 1
        finally:
            await _restore_canonical_legacy_tables(conn)

    async def test_finalize_cutover_fails_if_unimported_rows_remain(
        self, conn: psycopg.AsyncConnection, database_config: DatabaseConfig
    ):
        try:
            await conn.execute("DROP TABLE IF EXISTS messages CASCADE")
            await conn.execute("DROP TABLE IF EXISTS legacy_imported_messages CASCADE")

            await conn.execute(
                """
                CREATE TABLE messages (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    channel_name TEXT NOT NULL,
                    text TEXT
                )
                """
            )
            cur = await conn.execute(
                "INSERT INTO messages (channel_name, text) VALUES ('@chan', 'msg1'), ('@chan', 'msg2') RETURNING id"
            )
            rows = await cur.fetchall()
            id1, _ = rows[0][0], rows[1][0]

            await conn.execute(
                """
                CREATE TABLE legacy_imported_messages (
                    legacy_message_id BIGINT PRIMARY KEY,
                    source_item_id BIGINT NOT NULL,
                    source_item_revision_id BIGINT NOT NULL,
                    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            # Only import id1, leaving id2 unimported
            await conn.execute(
                "INSERT INTO legacy_imported_messages (legacy_message_id, source_item_id, source_item_revision_id) VALUES (%s, 1, 1)",
                (id1,),
            )

            rc = await finalize_cutover(database_config.url, domain_schema="public")
            assert rc == 1

            # messages table still exists
            cur = await conn.execute("SELECT count(*) FROM messages")
            assert (await cur.fetchone())[0] == 2
        finally:
            await _restore_canonical_legacy_tables(conn)

    async def test_finalize_cutover_succeeds_when_fully_imported(
        self, conn: psycopg.AsyncConnection, database_config: DatabaseConfig
    ):
        try:
            await conn.execute("DROP TABLE IF EXISTS messages CASCADE")
            await conn.execute("DROP TABLE IF EXISTS legacy_imported_messages CASCADE")

            await conn.execute(
                """
                CREATE TABLE messages (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    channel_name TEXT NOT NULL,
                    text TEXT
                )
                """
            )
            cur = await conn.execute(
                "INSERT INTO messages (channel_name, text) VALUES ('@chan', 'msg1') RETURNING id"
            )
            id1 = (await cur.fetchone())[0]

            await conn.execute(
                """
                CREATE TABLE legacy_imported_messages (
                    legacy_message_id BIGINT PRIMARY KEY,
                    source_item_id BIGINT NOT NULL,
                    source_item_revision_id BIGINT NOT NULL,
                    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            await conn.execute(
                "INSERT INTO legacy_imported_messages (legacy_message_id, source_item_id, source_item_revision_id) VALUES (%s, 1, 1)",
                (id1,),
            )

            rc = await finalize_cutover(database_config.url, domain_schema="public")
            assert rc == 0

            # messages table has been dropped
            cur = await conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'messages'"
            )
            assert await cur.fetchone() is None

            # Ledger contains version 11
            cur = await conn.execute("SELECT 1 FROM telebrief_schema_migrations WHERE version = 11")
            assert await cur.fetchone() is not None
        finally:
            await _restore_canonical_legacy_tables(conn)
