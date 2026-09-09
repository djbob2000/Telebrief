"""Unified publication runner for digests and articles (CLI)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import build_infrastructure
from src.config_loader import load_config
from src.publication.facade import request_publication
from src.runtime import install_runtime

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")


async def run_cli_publication(
    publication_type: str = "digest_grouped",
    edition_slug: str = "berdyansk",
    hours: int | None = None,
    save_markdown: bool = True,
) -> int:
    """Submit a production publication intent for CLI tools."""
    config = load_config()
    infra = await build_infrastructure(config.database)
    install_runtime(infra)
    try:
        result = await request_publication(
            publication_type=publication_type,
            edition_slug=edition_slug,
            lookback_hours=hours,
            config=config,
        )
        label = "статьи" if publication_type == "article" else "дайджеста"
        print(
            f"Обновляю источники и ставлю свежую публикацию {label} в очередь "
            f"(intent={result.intent_id}, status={result.readiness_status})."
        )
        if save_markdown:
            print("Файл Markdown будет доступен после асинхронной генерации публикации.")
        return 0
    except Exception as e:
        print(f"\n❌ Ошибка постановки публикации в очередь: {e}")
        return 1
    finally:
        await infra.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telebrief Publication CLI Runner")
    parser.add_argument(
        "--type",
        choices=["digest", "article"],
        default="digest",
        help="Publication type to generate (default: digest)",
    )
    parser.add_argument(
        "--edition",
        default="berdyansk",
        help="Edition slug (default: berdyansk)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Lookback window in hours (overrides configuration default)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not save markdown file to disk",
    )
    args = parser.parse_args()
    pub_type = "article" if args.type == "article" else "digest_grouped"
    exit_code = asyncio.run(
        run_cli_publication(
            publication_type=pub_type,
            edition_slug=args.edition,
            hours=args.hours,
            save_markdown=not args.no_save,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
