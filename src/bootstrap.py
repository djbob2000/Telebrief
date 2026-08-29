"""Application infrastructure bootstrap: connection pool, UoW, job queue.

:func:`build_infrastructure` owns the startup sequence (open pool -> verify
schema version -> open Procrastinate). The Procrastinate app is imported
lazily inside that sequence so legacy deployments (``database.enabled=false``)
never import :mod:`src.jobs.app`: that module builds its app from
``load_database_config(require_enabled=True)`` at import time and would
otherwise demand DATABASE_URL on every startup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

from src.config_loader import DatabaseConfig
from src.db.pool import close_pool, open_pool
from src.db.schema_version import require_schema_compatible
from src.db.uow import DatabaseUnitOfWork

if TYPE_CHECKING:
    import procrastinate

# Supported domain schema range; Plan 1 migrations land versions 1..3,
# Plan 2 Task 1 (source items) lands version 4, Plan 3 Task 1
# (relevance + claims) lands version 5, Plan 3 Task 5
# (embeddings + stories) lands version 6, Plan 3 Task 8
# (places + versioned place resolution) lands version 7, and the
# retention/trigger migration lands version 12.
SCHEMA_VERSION_MINIMUM = 7
SCHEMA_VERSION_MAXIMUM = 21

__all__ = ["ApplicationInfrastructure", "build_infrastructure"]


class ApplicationInfrastructure:
    """Long-lived domain resources owned by a running Telebrief process."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        uow: DatabaseUnitOfWork,
        procrastinate_app: procrastinate.App,
    ) -> None:
        self.pool = pool
        self.uow = uow
        self.procrastinate_app = procrastinate_app

    async def open(self) -> None:
        """Open the Procrastinate application.

        The pool must already be open (the schema gate in build_infrastructure
        runs over it); open() completes the final step of the startup sequence
        and stays the symmetric counterpart of close().
        """
        await self.procrastinate_app.open_async()

    async def close(self) -> None:
        """Close Procrastinate first, then the pool.

        Sequential teardown with per-step error collection: both attempts
        always run, and the first collected error is re-raised only after the
        remaining resource got its chance to close. (An asyncio.gather of the
        two closes was rejected: it interleaves teardown of dependent
        resources for no wall-clock gain.)
        """
        errors: list[BaseException] = []
        try:
            await self.procrastinate_app.close_async()
        except BaseException as exc:
            errors.append(exc)
        try:
            await self.pool.close()
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise errors[0]


async def build_infrastructure(database_config: DatabaseConfig) -> ApplicationInfrastructure:
    """Open the domain pool, gate on the schema version, start the queue app.

    Fails fast (SchemaVersionError) when the applied migration ledger is
    missing or outside [SCHEMA_VERSION_MINIMUM, SCHEMA_VERSION_MAXIMUM]; a
    partially opened pool is closed before the error propagates.
    """
    pool = await open_pool(database_config)
    try:
        async with pool.connection() as conn:
            await require_schema_compatible(
                conn,
                minimum=SCHEMA_VERSION_MINIMUM,
                maximum=SCHEMA_VERSION_MAXIMUM,
            )
    except BaseException:
        await close_pool(pool)
        raise

    from src.jobs.app import procrastinate_app  # lazy: kept off legacy startup

    infrastructure = ApplicationInfrastructure(
        pool=pool,
        uow=DatabaseUnitOfWork(pool),
        procrastinate_app=procrastinate_app,
    )
    try:
        await infrastructure.open()
    except BaseException:
        await close_pool(pool)
        raise
    return infrastructure
