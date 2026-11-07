"""Replay one frozen article run and write the full traceback to a file (scratch, no delivery)."""

import asyncio
import sys
import traceback

sys.path.insert(0, "/app")

from src.bootstrap import build_infrastructure  # noqa: E402
from src.config_loader import load_config  # noqa: E402
from src.publication.article_preview import build_article_preview_from_run  # noqa: E402
from src.runtime import install_runtime  # noqa: E402


import src.publication.article_preview as article_preview  # noqa: E402

_original_diag = article_preview._preview_diagnostics


def _traced_diag(*args, error=None, **kwargs):
    if error is not None:
        with open("/work/out/trace.txt", "w") as fh:
            traceback.print_exception(error, file=fh)
    return _original_diag(*args, error=error, **kwargs)


article_preview._preview_diagnostics = _traced_diag


async def main() -> None:
    config = load_config()
    infra = await build_infrastructure(config.database)
    install_runtime(infra)
    try:
        outcome = await build_article_preview_from_run(360, config=config)
        print("status:", getattr(outcome, "status", outcome))
    except Exception:  # noqa: BLE001 - diagnostic run
        with open("/work/out/trace.txt", "w") as fh:
            traceback.print_exc(file=fh)
        print("traceback written")


asyncio.run(main())
