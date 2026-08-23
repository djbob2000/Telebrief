"""Tests for Procrastinate application factory and connection budget (Task 12)."""

from __future__ import annotations

from src.config_loader import DatabaseConfig
from src.jobs.app import (
    PROCASTINATE_POOL_MAX_SIZE,
    PROCASTINATE_POOL_MIN_SIZE,
    build_app,
)


def test_connection_budget_within_postgres_max_connections():
    """Assert theoretical 2-process total (app + worker) stays below PostgreSQL max_connections=10."""
    db_config = DatabaseConfig()
    domain_max_pool = db_config.max_pool_size
    procrastinate_max_pool = PROCASTINATE_POOL_MAX_SIZE

    assert domain_max_pool == 3, f"Expected default domain pool max_size=3, got {domain_max_pool}"
    assert (
        procrastinate_max_pool == 1
    ), f"Expected Procrastinate pool max_size=1, got {procrastinate_max_pool}"

    # 2 processes: telebrief-app and telebrief-worker
    num_processes = 2
    per_process_connections = domain_max_pool + procrastinate_max_pool
    total_connections = num_processes * per_process_connections

    assert total_connections == 8
    assert (
        total_connections <= 10
    ), f"Total connections {total_connections} exceeds PostgreSQL max_connections=10"


def test_build_app_configures_procrastinate_connector():
    config = DatabaseConfig(
        enabled=True,
        url="postgresql://user:pass@localhost:5432/testdb",
        min_pool_size=1,
        max_pool_size=3,
        procrastinate_schema="custom_procrastinate",
    )
    app = build_app(config)
    connector = app.connector

    assert connector._pool_args["min_size"] == PROCASTINATE_POOL_MIN_SIZE
    assert connector._pool_args["max_size"] == PROCASTINATE_POOL_MAX_SIZE
    assert "search_path=custom_procrastinate" in connector._pool_args.get("kwargs", {}).get(
        "options", ""
    )
