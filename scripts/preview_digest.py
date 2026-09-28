"""Generate a canonical digest preview without delivery side effects.

Usage:
    python scripts/preview_digest.py [--edition berdyansk] [--hours 24] [--as-of ISO8601] [--output PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure  # noqa: E402
from src.config_loader import load_config  # noqa: E402
from src.publication.facade import build_publication_preview  # noqa: E402
from src.runtime import install_runtime  # noqa: E402


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
        help="Optional local path for the exact plain-text Telegram post",
    )
    parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="Reproducible snapshot cutoff as ISO-8601 with timezone, e.g. 2026-09-28T18:00:00+03:00",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    snapshot_at = None
    if args.as_of:
        try:
            snapshot_at = dt.datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SystemExit(f"Invalid --as-of ISO-8601 timestamp: {exc}") from exc
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() is None:
            raise SystemExit("--as-of must include a timezone offset (for example +00:00)")
    config = load_config()
    infra = await build_infrastructure(config.database)
    install_runtime(infra)

    try:
        preview = await build_publication_preview(
            publication_type="digest_grouped",
            edition_slug=args.edition,
            lookback_hours=args.hours,
            snapshot_at=snapshot_at,
            config=config,
        )
        artifact = preview.rendered_artifact
        if artifact is None:
            raise RuntimeError("preview returned no canonical rendered Telegram artifact")
        digest_text = artifact.visible_text

        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(digest_text, encoding="utf-8")
            print(f"Digest preview saved to {args.output}")
        else:
            sys.stdout.write(digest_text)
    finally:
        await infra.close()


if __name__ == "__main__":
    asyncio.run(main())
