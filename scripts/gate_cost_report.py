"""Read-only daily cost report for Gate V2 triage (no LLM calls, no writes).

Usage:
    DATABASE_URL=postgresql://... python scripts/gate_cost_report.py --days 7

The session is opened read-only, so the report cannot change publication or
processing state. Output is plain text: one row per day with the numbers the
call-reduction plan tracks (calls, failures, singleton splits, retries that
gave up, decisions by outcome, and the share of single-fragment stories).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import psycopg  # noqa: E402

DAILY_QUERY = """
WITH days AS (
    SELECT generate_series(
        date_trunc('day', now() - make_interval(days => %(days)s)),
        date_trunc('day', now()),
        interval '1 day'
    ) AS day
),
runs AS (
    SELECT date_trunc('day', started_at) AS day,
           count(*) AS calls,
           count(*) FILTER (WHERE status = 'failed') AS failed_calls,
           count(*) FILTER (WHERE story_count = 1) AS singleton_calls,
           coalesce(sum(input_chars), 0) AS input_chars
    FROM story_event_triage_runs
    GROUP BY 1
),
retries AS (
    SELECT date_trunc('day', updated_at) AS day,
           count(*) FILTER (WHERE exhausted_at IS NOT NULL) AS exhausted
    FROM story_event_processing_retries
    WHERE stage = 'triage'
    GROUP BY 1
),
decisions AS (
    SELECT date_trunc('day', created_at) AS day,
           count(*) AS decisions,
           count(*) FILTER (WHERE retention = 'KEEP') AS keep,
           count(*) FILTER (WHERE retention = 'DROP' AND exclusion_reason IS NULL) AS drop_conversational,
           count(*) FILTER (WHERE exclusion_reason = 'commercial_classified') AS drop_commercial
    FROM story_event_triage_decisions
    GROUP BY 1
)
SELECT to_char(d.day, 'YYYY-MM-DD'),
       coalesce(r.calls, 0),
       coalesce(r.failed_calls, 0),
       coalesce(r.singleton_calls, 0),
       coalesce(r.input_chars, 0),
       coalesce(t.exhausted, 0),
       coalesce(c.decisions, 0),
       coalesce(c.keep, 0),
       coalesce(c.drop_conversational, 0),
       coalesce(c.drop_commercial, 0)
FROM days d
LEFT JOIN runs r ON r.day = d.day
LEFT JOIN retries t ON t.day = d.day
LEFT JOIN decisions c ON c.day = d.day
ORDER BY d.day
"""


@dataclasses.dataclass(frozen=True)
class DailyRow:
    day: str
    calls: int
    failed_calls: int
    singleton_calls: int
    input_chars: int
    exhausted_retries: int
    decisions: int
    keep: int
    drop_conversational: int
    drop_commercial: int

    @property
    def failed_share(self) -> float:
        return self.failed_calls / self.calls if self.calls else 0.0


def row_from_db(values: tuple[Any, ...]) -> DailyRow:
    return DailyRow(*(int(v) if i else str(v) for i, v in enumerate(values)))


def format_report(rows: list[DailyRow]) -> str:
    header = (
        f"{'day':<12}{'calls':>7}{'failed':>8}{'fail%':>7}{'single':>8}"
        f"{'exhaust':>9}{'decis':>7}{'keep':>6}{'drop':>6}{'comm':>6}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(
            f"{row.day:<12}{row.calls:>7}{row.failed_calls:>8}"
            f"{row.failed_share * 100:>6.1f}%{row.singleton_calls:>8}"
            f"{row.exhausted_retries:>9}{row.decisions:>7}{row.keep:>6}"
            f"{row.drop_conversational:>6}{row.drop_commercial:>6}"
        )
    total_calls = sum(r.calls for r in rows)
    total_failed = sum(r.failed_calls for r in rows)
    lines.append("-" * len(header))
    lines.append(
        f"total: {total_calls} calls, {total_failed} failed"
        f" ({(total_failed / total_calls * 100) if total_calls else 0.0:.1f}%)"
    )
    return "\n".join(lines)


async def fetch_rows(database_url: str, days: int) -> list[DailyRow]:
    async with await psycopg.AsyncConnection.connect(
        database_url, options="-c default_transaction_read_only=on"
    ) as conn:
        cursor = await conn.execute(DAILY_QUERY, {"days": days})
        return [row_from_db(tuple(values)) for values in await cursor.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    rows = asyncio.run(fetch_rows(database_url, args.days))
    print(format_report(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
