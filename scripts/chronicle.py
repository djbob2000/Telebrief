"""Estimate or advance the storyline chronicle behind period reviews.

Usage:
    python scripts/chronicle.py                    # read-only estimate of due work
    python scripts/chronicle.py --apply            # build due weeks and rollups now
    python scripts/chronicle.py --apply --max-weeks 3 --max-rollups 4

The estimate opens one read-only session, plans every closed week that has no
chronicle entries yet and prints its Stories, model calls and prompt size. It
makes no model call. ``--apply`` runs the same ``advance_chronicle`` step the
periodic maintenance job runs, with the configured provider and model.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
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
from src.publication import chronicle  # noqa: E402

# Cyrillic JSON prompts run at roughly this many characters per token.
_CHARS_PER_TOKEN = 3.3
_SYSTEM_TOKENS = 1_200


class _ReadOnlyUnitOfWork:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self.conn = conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self.conn.transaction():
            yield self.conn


def _assume_empty_chronicle() -> None:
    """Estimate against a database without the chronicle tables (before 0042)."""

    async def no_periods(conn: object, edition_id: int) -> dict:
        del conn, edition_id
        return {}

    chronicle.completed_periods = no_periods
    chronicle._STORY_SQL = chronicle._STORY_SQL.replace(
        "OR NOT EXISTS (SELECT 1 FROM story_storyline_links l WHERE l.story_id = s.id)",
        "OR TRUE",
    )


async def _edition_ids(conn: psycopg.AsyncConnection, slug: str | None) -> list[int]:
    if slug:
        cursor = await conn.execute("SELECT id FROM editions WHERE slug = %s", (slug,))
    else:
        cursor = await conn.execute("SELECT id FROM editions WHERE enabled ORDER BY id")
    return [int(row[0]) for row in await cursor.fetchall()]


async def run_estimate(config_path: str, slug: str | None) -> str:
    db = load_database_config(config_path, require_enabled=True)
    now = dt.datetime.now(dt.timezone.utc)
    lines: list[str] = []
    async with await psycopg.AsyncConnection.connect(
        db.url, options="-c default_transaction_read_only=on"
    ) as conn:
        await conn.execute(
            sql.SQL("SET search_path TO {}, {}, public").format(
                sql.Identifier(db.domain_schema), sql.Identifier(db.procrastinate_schema)
            )
        )
        cursor = await conn.execute("SELECT to_regclass('chronicle_periods') IS NOT NULL")
        row = await cursor.fetchone()
        migrated = bool(row and row[0])
        await conn.commit()
        if not migrated:
            _assume_empty_chronicle()
            lines.append("schema 42 is not applied yet: every closed week counts as due")
        uow = _ReadOnlyUnitOfWork(conn)
        for edition_id in await _edition_ids(conn, slug):
            async with uow.transaction() as tx:
                _name, tz = await chronicle._edition(tx, edition_id)
                due = await chronicle.due_periods(tx, edition_id=edition_id, now=now, tz=tz)
            header = f"{'week':<24}{'stories':>9}{'calls':>7}{'in tokens':>11}"
            lines += [f"edition {edition_id}", header, "-" * len(header)]
            total_calls = total_tokens = 0
            for week in due.weeks:
                async with uow.transaction() as tx:
                    plan = await chronicle.plan_week(tx, edition_id=edition_id, period=week, tz=tz)
                tokens = int(plan.prompt_chars(tz) / _CHARS_PER_TOKEN)
                tokens += plan.model_calls * _SYSTEM_TOKENS
                total_calls += plan.model_calls
                total_tokens += tokens
                span = f"{week.start}..{week.last_day}"
                lines.append(f"{span:<24}{len(plan.stories):>9}{plan.model_calls:>7}{tokens:>11}")
            lines.append(f"{'total weeks':<24}{'':>9}{total_calls:>7}{total_tokens:>11}")
            if due.blocked_week is not None:
                lines.append(
                    f"week {due.blocked_week.start} waits for processing "
                    f"({due.backlog} items pending)"
                )
            rollups = ", ".join(f"{p.kind}:{p.start}" for p in due.rollups) or "none"
            lines.append(f"rollups due after the weeks: {rollups}")
    return "\n".join(lines)


async def run_apply(config_path: str, slug: str | None, max_weeks: int, max_rollups: int) -> str:
    from src.config_loader import load_config
    from src.db.pool import close_pool, open_pool
    from src.db.uow import DatabaseUnitOfWork
    from src.processing.providers import resolve_processing_provider

    log = logging.getLogger("chronicle")
    config = load_config(config_path)
    editorial = config.settings.publication_editorial
    # Same model as the scheduled chronicle job (background processing, not the editorial default).
    provider = resolve_processing_provider(
        None, config, log, request_timeout=chronicle.CHRONICLE_REQUEST_TIMEOUT_SECONDS
    )
    model = getattr(config, "openrouter_processing_model", None) or None
    db = load_database_config(config_path, require_enabled=True)
    pool = await open_pool(db)
    results: list[str] = []
    try:
        uow = DatabaseUnitOfWork(pool)
        async with uow.transaction() as conn:
            edition_ids = await _edition_ids(conn, slug)
        for edition_id in edition_ids:
            summary = await chronicle.advance_chronicle(
                uow=uow,
                provider=provider,
                model=model,
                edition_id=edition_id,
                now=dt.datetime.now(dt.timezone.utc),
                max_weeks=max_weeks,
                max_rollups=max_rollups,
                reasoning_effort=editorial.chronicle_reasoning_effort,
                headroom_tokens=editorial.reasoning_headroom_tokens,
                log=log,
            )
            results.append(f"edition {edition_id}: {summary}")
    finally:
        await close_pool(pool)
    return "\n".join(results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--edition", default=None, help="edition slug (default: all enabled)")
    parser.add_argument("--apply", action="store_true", help="build due weeks and rollups")
    parser.add_argument("--max-weeks", type=int, default=1)
    parser.add_argument("--max-rollups", type=int, default=2)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.apply:
        output = asyncio.run(run_apply(args.config, args.edition, args.max_weeks, args.max_rollups))
    else:
        output = asyncio.run(run_estimate(args.config, args.edition))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
