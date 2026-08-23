#!/usr/bin/env python3
"""CLI operator script for finalizing legacy messages table cutover (Task 11).

Safely drops the legacy `messages` table only after verifying all rows
have been imported into `legacy_imported_messages`. Records migration 0011 in
`telebrief_schema_migrations` upon success.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from src.config_loader import load_database_config  # noqa: E402
from src.db.migrations import PendingMigration, _record_version  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFERRED_MIGRATION_PATH = REPO_ROOT / "migrations" / "deferred" / "0011_drop_legacy_messages.sql"


def _default_database_url() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("TELEBRIEF_TEST_DATABASE_URL") or ""


def _resolve_domain_schema(explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    try:
        return load_database_config().domain_schema
    except FileNotFoundError:
        return "public"


async def finalize_cutover(
    database_url: str, domain_schema: str = "public", dry_run: bool = False
) -> int:
    """Check import status and safely drop legacy messages table."""
    if not database_url:
        logger.error("No database URL provided or found in environment.")
        return 1

    async with await psycopg.AsyncConnection.connect(database_url, autocommit=False) as conn:
        await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(domain_schema)))

        # 1. Check if messages table exists
        cur = await conn.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = %s AND table_name = 'messages'
            """,
            (domain_schema,),
        )
        if not await cur.fetchone():
            logger.info("Table 'messages' does not exist; legacy cutover is already complete.")
            # Ensure version 11 is recorded if deferred migration file exists
            if DEFERRED_MIGRATION_PATH.exists():
                cur_v = await conn.execute(
                    "SELECT 1 FROM telebrief_schema_migrations WHERE version = 11"
                )
                if not await cur_v.fetchone():
                    migration = PendingMigration(
                        version=11,
                        name=DEFERRED_MIGRATION_PATH.name,
                        path=DEFERRED_MIGRATION_PATH,
                        sql_text=DEFERRED_MIGRATION_PATH.read_text(encoding="utf-8"),
                        transactional=True,
                    )
                    await _record_version(conn, domain_schema, migration)
                    await conn.commit()
            return 0

        # 2. Check if legacy_imported_messages table exists
        cur = await conn.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = %s AND table_name = 'legacy_imported_messages'
            """,
            (domain_schema,),
        )
        if not await cur.fetchone():
            logger.error(
                "Cannot drop messages: legacy_imported_messages tracking table does not exist. "
                "Run scripts/import_legacy_messages.py first."
            )
            return 1

        # 3. Check message counts
        cur = await conn.execute("SELECT count(*) FROM messages")
        total_messages = (await cur.fetchone())[0]

        cur = await conn.execute("SELECT count(*) FROM legacy_imported_messages")
        imported_messages = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            SELECT count(*) FROM messages m
            WHERE NOT EXISTS (
                SELECT 1 FROM legacy_imported_messages lim WHERE lim.legacy_message_id = m.id
            )
            """
        )
        unimported_messages = (await cur.fetchone())[0]

        logger.info(
            "Legacy messages status: total=%d, imported_records=%d, unimported=%d",
            total_messages,
            imported_messages,
            unimported_messages,
        )

        if unimported_messages > 0:
            logger.error(
                "Refusing unsafe cutover: %d unimported legacy messages remain. "
                "Run scripts/import_legacy_messages.py first.",
                unimported_messages,
            )
            return 1

        if dry_run:
            logger.info("Dry-run mode: all %d messages verified imported. Safe to drop.", total_messages)
            return 0

        # 4. Execute deferred SQL and record migration ledger
        if not DEFERRED_MIGRATION_PATH.exists():
            logger.error("Deferred migration file not found: %s", DEFERRED_MIGRATION_PATH)
            return 1

        sql_text = DEFERRED_MIGRATION_PATH.read_text(encoding="utf-8")
        logger.info("Executing deferred migration 0011...")
        await conn.execute(sql_text)

        cur = await conn.execute(
            "SELECT 1 FROM telebrief_schema_migrations WHERE version = 11"
        )
        if not await cur.fetchone():
            migration = PendingMigration(
                version=11,
                name=DEFERRED_MIGRATION_PATH.name,
                path=DEFERRED_MIGRATION_PATH,
                sql_text=sql_text,
                transactional=True,
            )
            await _record_version(conn, domain_schema, migration)
        await conn.commit()
        logger.info("Successfully dropped legacy messages table and recorded migration 0011.")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Finalize legacy cutover by safely dropping legacy messages table."
    )
    parser.add_argument(
        "--database-url",
        default=_default_database_url(),
        help="PostgreSQL DSN (default: $DATABASE_URL or $TELEBRIEF_TEST_DATABASE_URL)",
    )
    parser.add_argument(
        "--domain-schema",
        default=None,
        help="Target schema (default: from config.yaml database.domain_schema or 'public')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify import state and print counts without dropping tables.",
    )
    args = parser.parse_args(argv)

    schema = _resolve_domain_schema(args.domain_schema)
    return asyncio.run(finalize_cutover(args.database_url, schema, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
