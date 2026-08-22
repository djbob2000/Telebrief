#!/usr/bin/env python3
"""Create the Procrastinate PostgreSQL namespace safely.

Runs ``CREATE SCHEMA IF NOT EXISTS`` with the configured schema name bound via
psycopg.sql.Identifier, so custom identifiers never reach SQL as interpolated
text. The tables and functions inside the namespace stay owned by the official
Procrastinate tooling:

    procrastinate --app=src.jobs.app.procrastinate_app schema --apply

Examples:
    python scripts/bootstrap_procrastinate_schema.py
    python scripts/bootstrap_procrastinate_schema.py --schema queue_v2
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import load_database_config  # noqa: E402
from src.jobs.admin import ensure_schema  # noqa: E402


def _default_database_url() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("TELEBRIEF_TEST_DATABASE_URL") or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the Procrastinate schema namespace.")
    parser.add_argument(
        "--database-url",
        default=_default_database_url(),
        help="PostgreSQL DSN (default: $DATABASE_URL or $TELEBRIEF_TEST_DATABASE_URL)",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help=(
            "Namespace to create (default: database.procrastinate_schema from config.yaml "
            "via load_database_config(require_enabled=True))"
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Config file providing the default schema name (default: config.yaml)",
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        parser.error(
            "--database-url is required (or set DATABASE_URL / TELEBRIEF_TEST_DATABASE_URL)"
        )

    if args.schema is not None:
        schema_name = args.schema
    else:
        try:
            schema_name = load_database_config(
                args.config, require_enabled=True
            ).procrastinate_schema
        except (FileNotFoundError, ValueError) as exc:
            print(f"cannot resolve schema name: {exc}", file=sys.stderr)
            return 1

    try:
        asyncio.run(ensure_schema(args.database_url, schema_name))
    except Exception as exc:  # CLI boundary: report and fail non-zero
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        return 1

    print(f"schema ready: {schema_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
