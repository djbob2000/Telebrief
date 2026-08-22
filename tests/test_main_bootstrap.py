"""Lifecycle tests for application bootstrap and the process-local runtime.

Covers both startup modes:

- ``database.enabled=false`` (legacy): scheduler/bot/MCP initialize without
  touching PostgreSQL, and neither ``src.jobs.app`` nor ``DATABASE_URL`` is
  ever required.
- ``database.enabled=true``: config load -> open DB -> schema gate -> open
  Procrastinate -> install runtime -> init scheduler/bot, with graceful
  shutdown that clears the runtime and closes Procrastinate/DB even when one
  component fails.

Database-backed tests are gated on TELEBRIEF_TEST_DATABASE_URL like tests/db;
everything else runs on fakes with no environment requirements.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

import main as main_module
import src.bootstrap as bootstrap_module
import src.runtime as runtime_module
from src.bootstrap import ApplicationInfrastructure, build_infrastructure
from src.config_loader import DatabaseConfig
from src.db.schema_version import SchemaVersionError
from src.db.uow import DatabaseUnitOfWork
from src.runtime import clear_runtime, get_runtime, install_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL_ENV = "TELEBRIEF_TEST_DATABASE_URL"

requires_test_db = pytest.mark.skipif(
    TEST_DATABASE_URL_ENV not in os.environ,
    reason=f"{TEST_DATABASE_URL_ENV} is not set",
)


class FakeProcrastinateApp:
    """Duck-typed procrastinate.App recording open/close calls."""

    def __init__(self, events: list[str], *, close_error: Exception | None = None) -> None:
        self.events = events
        self.close_error = close_error
        self.opened = False
        self.closed = False

    async def open_async(self) -> None:
        self.opened = True
        self.events.append("procrastinate_open")

    async def close_async(self) -> None:
        self.closed = True
        self.events.append("procrastinate_close")
        if self.close_error is not None:
            raise self.close_error


class FakePool:
    """Duck-typed connection pool recording close."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.events.append("pool_close")


class ExplodingPool(FakePool):
    async def close(self) -> None:
        self.events.append("pool_close")
        raise RuntimeError("pool close exploded")


def make_infrastructure(
    events: list[str], *, procrastinate_close_error: Exception | None = None
) -> tuple[ApplicationInfrastructure, FakePool]:
    """Build an ApplicationInfrastructure around recording fakes."""
    pool = FakePool(events)
    infrastructure = ApplicationInfrastructure(
        pool=pool,
        uow=DatabaseUnitOfWork(pool),
        procrastinate_app=FakeProcrastinateApp(events, close_error=procrastinate_close_error),
    )
    return infrastructure, pool


def write_app_config(tmp_path: Path, *, database_enabled: bool) -> None:
    database_block = "database:\n  enabled: true\n" if database_enabled else ""
    (tmp_path / "config.yaml").write_text(
        "channels:\n"
        '  - id: "@test_channel"\n'
        '    name: "Test Channel"\n'
        "\n"
        "settings:\n"
        '  schedule_time: "08:00"\n'
        "  timezone: UTC\n"
        "  lookback_hours: 24\n"
        "  target_user_id: 123456789\n"
        "\n"
        "mcp:\n"
        "  enabled: true\n"
        "\n" + database_block,
        encoding="utf-8",
    )


class _StubScheduler:
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get_next_run_time(self) -> str:
        return "--:--"


class _StubBotHandler:
    def setup_application(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def install_stub_components(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Replace DigestScheduler/BotCommandHandler/build_server in main with recorders."""

    def scheduler_factory(config, logger):
        del config, logger
        events.append("scheduler")
        return _StubScheduler()

    def bot_handler_factory(config, logger, scheduler):
        del config, logger, scheduler
        events.append("bot_handler")
        return _StubBotHandler()

    def mcp_factory(config, logger):
        del config, logger
        events.append("mcp")
        return object()

    monkeypatch.setattr(main_module, "DigestScheduler", scheduler_factory)
    monkeypatch.setattr(main_module, "BotCommandHandler", bot_handler_factory)
    monkeypatch.setattr(main_module, "build_server", mcp_factory)


# ---------------------------------------------------------------------------
# Process-local runtime registry
# ---------------------------------------------------------------------------


async def test_get_runtime_raises_before_install() -> None:
    with pytest.raises(RuntimeError, match="Telebrief runtime is not initialized"):
        get_runtime()


def test_install_then_get_returns_exact_instance() -> None:
    infrastructure, _ = make_infrastructure([])
    install_runtime(infrastructure)
    try:
        assert get_runtime() is infrastructure
    finally:
        clear_runtime(infrastructure)


def test_second_install_raises() -> None:
    first, _ = make_infrastructure([])
    second, _ = make_infrastructure([])
    install_runtime(first)
    try:
        with pytest.raises(RuntimeError, match="Telebrief runtime already installed"):
            install_runtime(second)
    finally:
        clear_runtime(first)


def test_clear_runtime_ignores_mismatched_instance() -> None:
    installed, _ = make_infrastructure([])
    stranger, _ = make_infrastructure([])
    install_runtime(installed)
    clear_runtime(stranger)
    assert get_runtime() is installed
    clear_runtime(installed)
    with pytest.raises(RuntimeError, match="Telebrief runtime is not initialized"):
        get_runtime()


# ---------------------------------------------------------------------------
# ApplicationInfrastructure open/close lifecycle
# ---------------------------------------------------------------------------


async def test_open_opens_procrastinate_app() -> None:
    events: list[str] = []
    infrastructure, _ = make_infrastructure(events)
    await infrastructure.open()
    assert infrastructure.procrastinate_app.opened


async def test_close_closes_procrastinate_then_pool() -> None:
    events: list[str] = []
    infrastructure, pool = make_infrastructure(events)
    await infrastructure.close()
    assert pool.closed
    assert events[-2:] == ["procrastinate_close", "pool_close"]


async def test_close_raises_first_error_but_still_closes_pool() -> None:
    events: list[str] = []
    infrastructure, pool = make_infrastructure(
        events, procrastinate_close_error=RuntimeError("queue close exploded")
    )
    with pytest.raises(RuntimeError, match="queue close exploded"):
        await infrastructure.close()
    assert pool.closed
    assert events[-2:] == ["procrastinate_close", "pool_close"]


async def test_close_closes_procrastinate_even_when_pool_fails() -> None:
    events: list[str] = []
    pool = ExplodingPool(events)
    infrastructure = ApplicationInfrastructure(
        pool=pool,
        uow=DatabaseUnitOfWork(pool),
        procrastinate_app=FakeProcrastinateApp(events),
    )
    with pytest.raises(RuntimeError, match="pool close exploded"):
        await infrastructure.close()
    assert infrastructure.procrastinate_app.closed
    assert events == ["procrastinate_close", "pool_close"]


# ---------------------------------------------------------------------------
# Legacy mode (database.enabled=false)
# ---------------------------------------------------------------------------


async def test_disabled_mode_initializes_without_database(
    mock_env_vars, monkeypatch, tmp_path
) -> None:
    write_app_config(tmp_path, database_enabled=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    events: list[str] = []
    install_stub_components(monkeypatch, events)

    def forbidden_build(database_config):
        raise AssertionError("legacy startup must not build infrastructure")

    monkeypatch.setattr(main_module, "build_infrastructure", forbidden_build)

    app = main_module.TelebriefApp()
    assert await app.initialize() is True

    assert events == ["scheduler", "bot_handler", "mcp"]
    assert app.infrastructure is None
    with pytest.raises(RuntimeError, match="Telebrief runtime is not initialized"):
        get_runtime()


def test_legacy_startup_never_imports_jobs_app(tmp_path) -> None:
    """Disabled startup must not import src.jobs.app nor require DATABASE_URL."""
    write_app_config(tmp_path, database_enabled=False)
    env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import main, src.bootstrap; print('src.jobs.app' in sys.modules)",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# Enabled mode (database.enabled=true) — fakes, no live DB required
# ---------------------------------------------------------------------------


def setup_enabled_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, events: list[str]):
    write_app_config(tmp_path, database_enabled=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://bootstrap-fake/unused")
    install_stub_components(monkeypatch, events)

    async def fake_build_infrastructure(database_config):
        events.append("build_infrastructure")
        assert database_config.enabled is True
        infrastructure, _pool = make_infrastructure(events)
        return infrastructure

    monkeypatch.setattr(main_module, "build_infrastructure", fake_build_infrastructure)

    real_install = runtime_module.install_runtime
    real_clear = runtime_module.clear_runtime

    def spying_install(infrastructure):
        events.append("install_runtime")
        real_install(infrastructure)

    def spying_clear(infrastructure):
        events.append("clear_runtime")
        real_clear(infrastructure)

    monkeypatch.setattr(main_module, "install_runtime", spying_install)
    monkeypatch.setattr(main_module, "clear_runtime", spying_clear)


async def test_enabled_mode_orders_bootstrap_and_installs_runtime(
    mock_env_vars, monkeypatch, tmp_path
) -> None:
    events: list[str] = []
    setup_enabled_mode(monkeypatch, tmp_path, events)

    app = main_module.TelebriefApp()
    assert await app.initialize() is True

    assert events == [
        "build_infrastructure",
        "install_runtime",
        "scheduler",
        "bot_handler",
        "mcp",
    ]
    running = app.infrastructure
    assert isinstance(running, ApplicationInfrastructure)
    assert get_runtime() is running

    events.clear()
    await app.shutdown()

    assert events == ["clear_runtime", "procrastinate_close", "pool_close"]
    assert app.infrastructure is None
    with pytest.raises(RuntimeError, match="Telebrief runtime is not initialized"):
        get_runtime()


async def test_shutdown_survives_procrastinate_failure_and_still_closes_db(
    mock_env_vars, monkeypatch, tmp_path
) -> None:
    events: list[str] = []
    setup_enabled_mode(monkeypatch, tmp_path, events)

    app = main_module.TelebriefApp()
    assert await app.initialize() is True
    installed = app.infrastructure
    assert installed is not None
    installed.procrastinate_app.close_error = RuntimeError("queue close exploded")

    events.clear()

    # Shutdown must not raise even though the queue refuses to close.
    await app.shutdown()

    assert events == ["clear_runtime", "procrastinate_close", "pool_close"]
    assert installed.pool.closed
    assert app.infrastructure is None
    with pytest.raises(RuntimeError, match="Telebrief runtime is not initialized"):
        get_runtime()


async def test_initialize_fails_fast_on_schema_version_error(
    mock_env_vars, monkeypatch, tmp_path, capsys
) -> None:
    events: list[str] = []
    write_app_config(tmp_path, database_enabled=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://bootstrap-fake/unused")
    install_stub_components(monkeypatch, events)

    async def incompatible_schema(database_config):
        raise SchemaVersionError("database schema version 2 is outside the supported range [3, 3]")

    monkeypatch.setattr(main_module, "build_infrastructure", incompatible_schema)

    app = main_module.TelebriefApp()
    assert await app.initialize() is False

    assert events == []  # scheduler/bot/MCP never constructed after failed gate
    assert app.infrastructure is None
    output = capsys.readouterr().out
    assert "outside the supported range" in output


# ---------------------------------------------------------------------------
# Enabled mode against the real PostgreSQL test database
# ---------------------------------------------------------------------------


@pytest.fixture
def jobs_import_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Environment so the lazy import of src.jobs.app inside bootstrap succeeds.

    Mirrors tests/db/test_transactional_defer.py: the production jobs module
    builds ``procrastinate_app`` at import time from
    ``load_database_config(require_enabled=True)``, so DATABASE_URL and an
    enabled config.yaml must be in place before the first import.
    """
    url = os.environ[TEST_DATABASE_URL_ENV]
    monkeypatch.setenv("DATABASE_URL", url)
    (tmp_path / "config.yaml").write_text("database:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return url


@requires_test_db
async def test_build_infrastructure_real_database_roundtrip(jobs_import_env: str) -> None:
    config = DatabaseConfig(
        enabled=True,
        url=jobs_import_env,
        min_pool_size=1,
        max_pool_size=4,
        domain_schema="public",
        procrastinate_schema="procrastinate",
    )

    infrastructure = await build_infrastructure(config)
    try:
        async with infrastructure.uow.transaction() as conn:
            cursor = await conn.execute("SELECT 1")
            row = await cursor.fetchone()
            assert row is not None and row[0] == 1
        install_runtime(infrastructure)
        assert get_runtime() is infrastructure
    finally:
        clear_runtime(infrastructure)
        await infrastructure.close()

    with pytest.raises(RuntimeError, match="Telebrief runtime is not initialized"):
        get_runtime()


@requires_test_db
async def test_build_infrastructure_fails_fast_on_missing_ledger_and_closes_pool(
    jobs_import_env: str, monkeypatch
) -> None:
    url = jobs_import_env
    schema_name = f"bootstrap_probe_{uuid.uuid4().hex[:10]}"
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
        )
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))

    created_pools = []
    real_open_pool = bootstrap_module.open_pool

    async def spying_open_pool(database_config):
        pool = await real_open_pool(database_config)
        created_pools.append(pool)
        return pool

    monkeypatch.setattr(bootstrap_module, "open_pool", spying_open_pool)

    config = DatabaseConfig(
        enabled=True,
        url=url,
        min_pool_size=1,
        max_pool_size=4,
        domain_schema=schema_name,
        procrastinate_schema="procrastinate",
    )
    try:
        with pytest.raises(SchemaVersionError, match="not found"):
            await build_infrastructure(config)
        assert created_pools and all(pool.closed for pool in created_pools)
    finally:
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )
