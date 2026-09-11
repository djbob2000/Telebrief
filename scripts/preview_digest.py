"""Generate a canonical digest preview without delivery side effects.

Usage:
    python scripts/preview_digest.py [--edition berdyansk] [--hours 24] [--output PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.publication.facade import build_publication_preview
from src.runtime import install_runtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a daily Event-First digest preview without delivery."
    )
    parser.add_argument("--edition", default="berdyansk", help="Edition slug (default: berdyansk)")
    parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Lookback window in hours (default: 24)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional local path for the Markdown preview",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    config = load_config()
    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    try:
        preview = await build_publication_preview(
            publication_type="digest_grouped",
            edition_slug=args.edition,
            lookback_hours=args.hours,
            config=config,
        )
        parts = [f"# {preview.title}" if preview.title else "# Дайджест"]
        if preview.lead:
            parts.extend(["", preview.lead])
        if preview.body:
            parts.extend(["", preview.body])
        digest_text = "\n".join(parts).rstrip() + "\n"

        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(digest_text, encoding="utf-8")
            print(f"Digest preview saved to {args.output}")
        else:
            print(digest_text, end="")
    finally:
        await infra.close()


if __name__ == "__main__":
    asyncio.run(main())
