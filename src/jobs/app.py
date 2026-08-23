"""Procrastinate application factory for Telebrief background jobs."""

from __future__ import annotations

from collections.abc import Iterable

import procrastinate

from src.config_loader import DatabaseConfig, load_database_config

PROCASTINATE_POOL_MIN_SIZE = 1
PROCASTINATE_POOL_MAX_SIZE = 2
JOB_IMPORT_PATHS = (
    "src.jobs.maintenance",
    "src.jobs.ingestion",
    "src.jobs.processing",
    "src.jobs.publication",
    "src.jobs.facebook",
)


def build_app(
    database_config: DatabaseConfig,
    *,
    blueprints: Iterable[procrastinate.Blueprint] = (),
) -> procrastinate.App:
    """Build a Procrastinate app bound to the configured PostgreSQL namespace.

    The connector pool is intentionally capped at two connections; deployment
    must account for the total connection budget across app and worker
    processes.
    """
    connector = procrastinate.PsycopgConnector(
        conninfo=database_config.url,
        min_size=PROCASTINATE_POOL_MIN_SIZE,
        max_size=PROCASTINATE_POOL_MAX_SIZE,
        kwargs={"options": f"-c search_path={database_config.procrastinate_schema}"},
    )
    app = procrastinate.App(connector=connector, import_paths=JOB_IMPORT_PATHS)
    for blueprint in blueprints:
        app.add_tasks_from(blueprint, namespace="")
    return app


# Production entry point built at import time so `procrastinate
# --app=src.jobs.app.procrastinate_app` CLI commands resolve it directly.
# load_database_config(require_enabled=True) keeps CLI usage free of Telegram
# and AI credentials while failing clearly when invoked before the database is
# enabled (database.enabled + DATABASE_URL).
procrastinate_app: procrastinate.App = build_app(load_database_config(require_enabled=True))
