"""Report what retention would evict, or run one retention pass on request.

Usage:
    python scripts/retention_report.py            # read-only report
    python scripts/retention_report.py --apply    # run the nightly cleanup now

The report opens one read-only session and prints, for each step, the rows it
would touch and the stored bytes of the vectors it would drop. ``--apply``
runs the same ``RetentionService.cleanup`` the nightly maintenance job runs,
in small batches with one transaction each.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from src.config.parsers.common import load_database_config  # noqa: E402
from src.db.pool import close_pool, open_pool  # noqa: E402
from src.db.uow import DatabaseUnitOfWork  # noqa: E402
from src.retention import RetentionPolicy, RetentionReportRow, RetentionService  # noqa: E402


class _ReadOnlyUnitOfWork:
    """One read-only connection standing in for the pooled unit of work."""

    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self.conn = conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self.conn.transaction():
            yield self.conn


def format_report(rows: list[RetentionReportRow]) -> str:
    header = f"{'step':<38}{'rows':>12}{'stored MB':>12}"
    lines = [header, "-" * len(header)]
    for row in rows:
        size = f"{row.bytes / 1024 / 1024:.1f}" if row.bytes else "-"
        lines.append(f"{row.step:<38}{row.rows:>12}{size:>12}")
    return "\n".join(lines)


async def run_report(config_path: str, policy: RetentionPolicy) -> str:
    db = load_database_config(config_path, require_enabled=True)
    async with await psycopg.AsyncConnection.connect(
        db.url, options="-c default_transaction_read_only=on"
    ) as conn:
        await conn.execute(
            sql.SQL("SET search_path TO {}, {}, public").format(
                sql.Identifier(db.domain_schema), sql.Identifier(db.procrastinate_schema)
            )
        )
        await conn.commit()
        service = RetentionService(_ReadOnlyUnitOfWork(conn), policy=policy)  # type: ignore[arg-type]
        return format_report(await service.report())


async def run_apply(config_path: str, policy: RetentionPolicy) -> str:
    db = load_database_config(config_path, require_enabled=True)
    pool = await open_pool(db)
    try:
        service = RetentionService(DatabaseUnitOfWork(pool), policy=policy)
        result = await service.cleanup(now=dt.datetime.now(dt.timezone.utc))
    finally:
        await close_pool(pool)
    return str(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--apply", action="store_true", help="run the cleanup now")
    parser.add_argument("--fragment-vector-days", type=int, default=None)
    parser.add_argument("--centroid-days", type=int, default=None)
    args = parser.parse_args()
    defaults = RetentionPolicy()
    policy = RetentionPolicy(
        fragment_vector_days=args.fragment_vector_days or defaults.fragment_vector_days,
        centroid_days=args.centroid_days or defaults.centroid_days,
    )
    runner = run_apply if args.apply else run_report
    print(asyncio.run(runner(args.config, policy)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
