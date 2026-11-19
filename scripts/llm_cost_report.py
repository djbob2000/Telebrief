"""Read-only LLM cost report by stage, model, origin and day (no LLM calls, no writes).

Usage:
    DATABASE_URL=postgresql://... python scripts/llm_cost_report.py --days 7
    DATABASE_URL=postgresql://... python scripts/llm_cost_report.py --days 30 --by model

Reads ``llm_usage_events``: one row per completed model request, with the cost
OpenRouter reported for it. The session is opened read-only. ``--by`` picks the
grouping (stage, model, origin or day); a stage-by-day table follows the summary.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

GROUPINGS = {
    "stage": sql.SQL("stage"),
    "model": sql.SQL("model"),
    "origin": sql.SQL("origin"),
    "day": sql.SQL("to_char(date_trunc('day', created_at), 'YYYY-MM-DD')"),
}

SUMMARY_QUERY = """
SELECT {key} AS key,
       count(*) AS calls,
       coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
       coalesce(sum(cached_tokens), 0) AS cached_tokens,
       coalesce(sum(completion_tokens), 0) AS completion_tokens,
       coalesce(sum(reasoning_tokens), 0) AS reasoning_tokens,
       coalesce(sum(cost_usd), 0) AS cost_usd,
       count(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls
FROM llm_usage_events
WHERE created_at >= now() - make_interval(days => %(days)s)
GROUP BY 1
ORDER BY cost_usd DESC, calls DESC
"""

DAILY_STAGE_QUERY = """
SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day,
       stage,
       coalesce(sum(cost_usd), 0) AS cost_usd
FROM llm_usage_events
WHERE created_at >= now() - make_interval(days => %(days)s)
GROUP BY 1, 2
ORDER BY 1, 2
"""


@dataclasses.dataclass(frozen=True)
class UsageRow:
    key: str
    calls: int
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: Decimal
    unpriced_calls: int

    @property
    def cached_share(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    @property
    def cost_per_call(self) -> Decimal:
        return self.cost_usd / self.calls if self.calls else Decimal(0)


def usage_row(values: tuple[Any, ...]) -> UsageRow:
    key, calls, prompt, cached, completion, reasoning, cost, unpriced = values
    return UsageRow(
        key=str(key),
        calls=int(calls),
        prompt_tokens=int(prompt),
        cached_tokens=int(cached),
        completion_tokens=int(completion),
        reasoning_tokens=int(reasoning),
        cost_usd=Decimal(cost),
        unpriced_calls=int(unpriced),
    )


def _tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def format_summary(rows: list[UsageRow], by: str) -> str:
    total_cost = sum((row.cost_usd for row in rows), Decimal(0))
    header = (
        f"{by:<24}{'calls':>8}{'prompt':>9}{'cached%':>9}{'compl':>8}{'reason':>8}"
        f"{'cost $':>10}{'$/call':>10}{'share':>7}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        share = float(row.cost_usd / total_cost) if total_cost else 0.0
        lines.append(
            f"{row.key[:23]:<24}{row.calls:>8}{_tokens(row.prompt_tokens):>9}"
            f"{row.cached_share * 100:>8.1f}%{_tokens(row.completion_tokens):>8}"
            f"{_tokens(row.reasoning_tokens):>8}{row.cost_usd:>10.4f}"
            f"{row.cost_per_call:>10.5f}{share * 100:>6.1f}%"
        )
    lines.append("-" * len(header))
    calls = sum(row.calls for row in rows)
    unpriced = sum(row.unpriced_calls for row in rows)
    lines.append(f"total: {calls} calls, ${total_cost:.4f}")
    if unpriced:
        lines.append(f"{unpriced} calls carry no reported cost (not counted in $)")
    return "\n".join(lines)


def format_daily_stages(values: list[tuple[str, str, Any]]) -> str:
    days = sorted({day for day, _, _ in values})
    stages = sorted({stage for _, stage, _ in values})
    cost = {(day, stage): Decimal(amount) for day, stage, amount in values}
    width = max([len(stage) for stage in stages] + [5]) + 2
    header = f"{'day':<12}" + "".join(f"{stage:>{width}}" for stage in stages) + f"{'total':>10}"
    lines = [header, "-" * len(header)]
    for day in days:
        row = [cost.get((day, stage), Decimal(0)) for stage in stages]
        lines.append(
            f"{day:<12}"
            + "".join(f"{amount:>{width}.4f}" for amount in row)
            + f"{sum(row, Decimal(0)):>10.4f}"
        )
    return "\n".join(lines)


async def fetch(
    database_url: str, days: int, by: str
) -> tuple[list[UsageRow], list[tuple[str, str, Any]]]:
    async with await psycopg.AsyncConnection.connect(
        database_url, options="-c default_transaction_read_only=on"
    ) as conn:
        summary = sql.SQL(SUMMARY_QUERY).format(key=GROUPINGS[by])
        cursor = await conn.execute(summary, {"days": days})
        rows = [usage_row(tuple(values)) for values in await cursor.fetchall()]
        cursor = await conn.execute(DAILY_STAGE_QUERY, {"days": days})
        daily = [tuple(values) for values in await cursor.fetchall()]
    return rows, daily


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--by", choices=sorted(GROUPINGS), default="stage")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    rows, daily = asyncio.run(fetch(database_url, args.days, args.by))
    if not rows:
        print(f"no LLM usage recorded in the last {args.days} days")
        return 0
    print(format_summary(rows, args.by))
    print()
    print(format_daily_stages(daily))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
