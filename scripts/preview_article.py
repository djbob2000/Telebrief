"""Generate a canonical daily article preview without delivery side effects.

Usage:
    python scripts/preview_article.py [--edition berdyansk] [--hours 24]
        [--date YYYY-MM-DD] [--output PATH]

The preview uses the same Event-First snapshot, selection, and generation
pipeline as production publication. It creates no Telegraph page, cover image,
delivery payload, delivery attempt, or Telegram message.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.publication.facade import build_publication_preview
from src.runtime import install_runtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a daily Event-First article preview without delivery."
    )
    parser.add_argument("--edition", default="berdyansk", help="Edition slug")
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Lookback window in hours (default: 24)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Snapshot date in the edition timezone (YYYY-MM-DD, at 20:00)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional local path for the Markdown preview",
    )
    return parser.parse_args()


def _snapshot_at(date_value: str | None, timezone: str) -> dt.datetime | None:
    if date_value is None:
        return None
    parsed = dt.date.fromisoformat(date_value)
    return dt.datetime(parsed.year, parsed.month, parsed.day, 20, 0, tzinfo=ZoneInfo(timezone))


async def main() -> None:
    args = parse_args()
    config = load_config()
    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    try:
        preview = await build_publication_preview(
            publication_type="daily_article",
            edition_slug=args.edition,
            snapshot_at=_snapshot_at(args.date, config.settings.timezone),
            lookback_hours=args.hours,
            config=config,
        )
        parts = [f"# {preview.title}" if preview.title else "# Вечерняя статья"]
        if preview.lead:
            parts.extend(["", preview.lead])
        if preview.body:
            parts.extend(["", preview.body])
        article = "\n".join(parts).rstrip() + "\n"

        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(article, encoding="utf-8")
            print(f"Preview saved to {args.output}")
        else:
            print(article, end="")
    finally:
        await infra.close()


if __name__ == "__main__":
    asyncio.run(main())
