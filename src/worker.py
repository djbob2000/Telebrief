"""Worker entry point: run the Procrastinate worker for Telebrief queues.

Usage: ``python -m src.worker``

Startup sequence: load the database configuration (no Telegram/AI credentials
needed), open :class:`~src.bootstrap.ApplicationInfrastructure` (domain pool,
schema gate, queue app — build_infrastructure opens the app), install it as
the process runtime, then run workers for the ``collection`` and
``maintenance`` queues until interrupted.

Task bodies resolve services through :func:`src.runtime.get_runtime`; no job
constructs a fresh connection pool. All heavy imports live inside the worker
coroutine so ``python -c "import src.worker"`` works without DATABASE_URL.
"""

from __future__ import annotations

import asyncio

WORKER_QUEUES = ("collection", "maintenance")


async def run_worker() -> None:
    """Open infrastructure, install the runtime, and serve jobs until stopped."""
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
        await procrastinate_app.run_worker_async(queues=list(WORKER_QUEUES))
    finally:
        clear_runtime(infrastructure)
        await infrastructure.close()


def main() -> None:
    """Synchronous entry point for ``python -m src.worker``."""
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
