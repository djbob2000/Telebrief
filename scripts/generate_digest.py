"""Generate and print the latest Event-First Reader-First Telegram digest."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.publish import run_cli_publication


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate and print Telegram digest")
    parser.add_argument("--edition", default="berdyansk", help="Edition slug (default: berdyansk)")
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Lookback window in hours (overrides configuration default)",
    )
    parser.add_argument("--no-save", action="store_true", help="Do not save markdown file to disk")
    args = parser.parse_args()

    exit_code = asyncio.run(
        run_cli_publication(
            publication_type="digest_grouped",
            edition_slug=args.edition,
            hours=args.hours,
            save_markdown=not args.no_save,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
