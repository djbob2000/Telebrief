"""Worker entry point: run the Procrastinate worker for all Telebrief queues.

Usage: ``python -m src.worker [--concurrency=N]``

This module is the single supported way to run jobs: it opens
:class:`~src.bootstrap.ApplicationInfrastructure` (domain pool, schema gate,
queue app) and installs it as the process runtime before serving tasks. The
bare ``procrastinate ... worker`` CLI must not be used for Telebrief queues —
it never installs the runtime and every task body requires
:func:`src.runtime.get_runtime`.

Startup sequence: load the database configuration (no Telegram/AI credentials
needed), open infrastructure, install the runtime, then serve every queue
(``collection``, ``maintenance``, ``processing``, ``publication``,
``enrichment``) until interrupted.

Task bodies resolve services through :func:`src.runtime.get_runtime`; no job
constructs a fresh connection pool. All heavy imports live inside the worker
coroutine so ``python -c "import src.worker"`` works without DATABASE_URL.
"""

from __future__ import annotations

import argparse
import asyncio

# Every queue used by registered tasks: collection (Telegram/Facebook),
# maintenance (retention, sweeps), processing (knowledge pipeline),
# publication (snapshot -> delivery), enrichment (Facebook comment refresh).
WORKER_QUEUES = ("collection", "maintenance", "processing", "publication", "enrichment", "default")
DEFAULT_CONCURRENCY = 2


async def run_worker(*, concurrency: int = DEFAULT_CONCURRENCY) -> None:
    """Open infrastructure, install the runtime, and serve jobs until stopped."""
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    from src.bootstrap import build_infrastructure
    from src.config_loader import load_database_config
    from src.jobs.app import procrastinate_app
    from src.runtime import clear_runtime, install_runtime

    config = load_database_config(require_enabled=True)
    # build_infrastructure opens pool + queue app (and gates on the schema
    # version); an explicit open() here would double-open the connector.
    infrastructure = await build_infrastructure(config)
    install_runtime(infrastructure)
    try:
        await procrastinate_app.run_worker_async(
            queues=list(WORKER_QUEUES),
            concurrency=concurrency,
        )
    finally:
        clear_runtime(infrastructure)
        # Cached platform collectors hold provider clients (Telethon, ...);
        # release them before the pool/connector they may still reference.
        from src.jobs.ingestion import collector_registry

        await collector_registry.aclose()
        await infrastructure.close()


def main(argv: list[str] | None = None) -> None:
    """Synchronous entry point for ``python -m src.worker``."""
    parser = argparse.ArgumentParser(description="Run the Telebrief Procrastinate worker.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"number of concurrent jobs (default: {DEFAULT_CONCURRENCY})",
    )
    args = parser.parse_args(argv)
    asyncio.run(run_worker(concurrency=args.concurrency))


if __name__ == "__main__":
    main()
