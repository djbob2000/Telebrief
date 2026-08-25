#!/usr/bin/env python3
"""CLI wrapper applying Telebrief SQL migrations.

Examples:
    python scripts/migrate.py
    python scripts/migrate.py --database-url postgresql://... --migrations-dir migrations
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from dotenv import load_dotenv

load_dotenv()

from src.config_loader import load_database_config  # noqa: E402
from src.db.migrations import migrate  # noqa: E402


def _default_database_url() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("TELEBRIEF_TEST_DATABASE_URL") or ""


def _resolve_domain_schema(explicit: str | None) -> str:
    """Prefer an explicit --domain-schema, else config.yaml's database.domain_schema.

    Falls back to 'public' when no configuration file exists so the CLI keeps
    working for standalone database-only setups.
    """
    if explicit is not None:
        return explicit
    try:
        return load_database_config().domain_schema
    except FileNotFoundError:
        return "public"


async def _apply(
    database_url: str, migrations_dir: Path, domain_schema_name: str
) -> tuple[int, list[int]]:
    # Autocommit: migrate() manages its own transactions; a session-level
    # search_path pin would otherwise dangle inside an implicit transaction.
    async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
        await conn.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(domain_schema_name))
        )
        version = await migrate(conn, migrations_dir)
        cursor = await conn.execute(
            "SELECT version FROM telebrief_schema_migrations ORDER BY version"
        )
        rows = await cursor.fetchall()
    return version, [int(row[0]) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply Telebrief PostgreSQL migrations.")
    parser.add_argument(
        "--database-url",
        default=_default_database_url(),
        help="PostgreSQL DSN (default: $DATABASE_URL or $TELEBRIEF_TEST_DATABASE_URL)",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=Path("migrations"),
        help="Directory containing numbered .sql migration files (default: migrations/)",
    )
    parser.add_argument(
        "--domain-schema",
        default=None,
        help=(
            "Domain schema receiving the migrations "
            "(default: database.domain_schema from config.yaml, else public)"
        ),
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        parser.error(
            "--database-url is required (or set DATABASE_URL / TELEBRIEF_TEST_DATABASE_URL)"
        )

    try:
        version, applied = asyncio.run(
            _apply(
                args.database_url,
                args.migrations_dir,
                _resolve_domain_schema(args.domain_schema),
            )
        )
    except Exception as exc:  # CLI boundary: report and fail non-zero
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1

    for applied_version in applied:
        print(f"applied: {applied_version}")
    print(f"schema version: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
