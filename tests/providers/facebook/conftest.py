"""Fixtures for PostgreSQL-backed Facebook tests in tests/providers/facebook."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import procrastinate
import psycopg
import pytest
from procrastinate.testing import InMemoryConnector
from psycopg_pool import AsyncConnectionPool

from src.bootstrap import ApplicationInfrastructure
from src.config_loader import DatabaseConfig
from src.db.migrations import migrate
from src.db.pool import close_pool, open_pool
from src.db.uow import DatabaseUnitOfWork
from src.runtime import install_runtime

REPO_ROOT = Path(__file__).parent.parent.parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    del config
    gate = Path(__file__).parent.resolve()
    for item in items:
        if item.path.resolve().is_relative_to(gate):
            item.add_marker(
                pytest.mark.skipif(
                    "TELEBRIEF_TEST_DATABASE_URL" not in os.environ,
                    reason="TELEBRIEF_TEST_DATABASE_URL is not set",
                )
            )


@pytest.fixture(scope="session", autouse=True)
def ensure_test_env(tmp_path_factory) -> None:
    if "TELEBRIEF_TEST_DATABASE_URL" not in os.environ:
        return
    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    os.environ["DATABASE_URL"] = url
    p = tmp_path_factory.mktemp("fb_test_cfg")
    (p / "config.yaml").write_text("database:\n  enabled: true\n", encoding="utf-8")
    os.chdir(p)


@pytest.fixture(scope="session", autouse=True)
async def ensure_schema_migrations() -> None:
    if "TELEBRIEF_TEST_DATABASE_URL" not in os.environ:
        return
    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await migrate(conn, MIGRATIONS_DIR)


_TRUNCATE_TABLES = """
    TRUNCATE collector_artifacts,
             facebook_comment_collection_state,
             facebook_source_configs,
             facebook_auth_profiles,
             source_editions, sources, editions
    CASCADE;
"""


@pytest.fixture
async def conn() -> AsyncIterator[psycopg.AsyncConnection]:
    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(url, autocommit=False) as connection:
        await connection.execute(_TRUNCATE_TABLES)
        await connection.commit()
        try:
            yield connection
        finally:
            await connection.rollback()


@pytest.fixture
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    cfg = DatabaseConfig(
        enabled=True,
        url=url,
        min_pool_size=1,
        max_pool_size=4,
        domain_schema="public",
        procrastinate_schema="procrastinate",
    )
    p = await open_pool(cfg)
    try:
        yield p
    finally:
        await close_pool(p)


@pytest.fixture
def uow(pool: AsyncConnectionPool) -> DatabaseUnitOfWork:
    return DatabaseUnitOfWork(pool)


@pytest.fixture(autouse=True)
def install_test_runtime(
    pool: AsyncConnectionPool, uow: DatabaseUnitOfWork
) -> ApplicationInfrastructure:
    fake_app = procrastinate.App(connector=InMemoryConnector())
    rt = ApplicationInfrastructure(
        pool=pool,
        uow=uow,
        procrastinate_app=fake_app,
    )
    install_runtime(rt)
    return rt


@pytest.fixture
async def edition(conn: psycopg.AsyncConnection) -> SimpleNamespace:
    cursor = await conn.execute(
        """
        INSERT INTO editions (slug, name, timezone, language, enabled)
        VALUES ('berdyansk', 'Бердянск', 'Europe/Kyiv', 'ru', true)
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
        RETURNING id, slug, name, timezone, language, enabled
        """
    )
    row = await cursor.fetchone()
    assert row is not None
    return SimpleNamespace(
        id=row[0],
        slug=row[1],
        name=row[2],
        timezone=row[3],
        language=row[4],
        enabled=row[5],
    )
