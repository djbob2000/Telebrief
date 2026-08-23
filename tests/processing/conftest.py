"""Fixtures for PostgreSQL-backed processing tests in tests/processing.

Tests here are gated on TELEBRIEF_TEST_DATABASE_URL: when the variable is not
set, every test collected under tests/processing is skipped instead of
failing.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import procrastinate
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from src.config_loader import DatabaseConfig
from src.db.pool import close_pool, open_pool
from src.db.uow import DatabaseUnitOfWork

_TRUNCATE_TABLES = """
    TRUNCATE story_relation_proposals, story_match_decisions,
             story_matching_candidates, story_matching_runs,
             story_matching_policy_versions,
             story_relations, story_state_events, story_claims,
               story_revision_embeddings, story_revisions, stories,
             claim_embeddings,
             place_resolution_results, place_resolution_runs,
             place_resolution_policy_versions,
             claim_entities, claim_place_mentions, place_aliases, places,
             claim_state_events, claim_relations, claims,
             claim_extraction_runs, claim_extraction_policy_versions,
             processing_attempts,
             vision_observations, vision_analysis_runs,
             vision_policy_versions,
             edition_relevance_decisions, relevance_policy_versions,
             source_items, source_item_revisions, source_assets,
             source_item_state_events, collection_checkpoints,
             collection_runs, source_editions, sources, editions
    RESTART IDENTITY CASCADE
"""


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests in this directory unless a test database is configured."""
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


@pytest.fixture
async def conn(database_config: DatabaseConfig):
    """Autocommit connection to a clean slice of the test database.

    Autocommit keeps the connection usable after expected constraint
    violations (the transaction never stays aborted). Every domain table is
    truncated (never telebrief_schema_migrations or collection_* bookkeeping
    beyond what CASCADE needs); RESTART IDENTITY makes ids deterministic and
    every run re-runnable against the persistent database.
    """
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        database_config.url, autocommit=True
    )
    try:
        await _clear_slice(conn)
        yield conn
    finally:
        await _clear_slice(conn)
        await conn.close()


async def _clear_slice(conn: psycopg.AsyncConnection) -> None:
    await conn.execute(_TRUNCATE_TABLES)
    # Procrastinate's own DELETE trigger references its tables unqualified,
    # so cleanup needs the queue schema on the search path.
    await conn.execute("SET search_path TO public, procrastinate")
    await conn.execute("DELETE FROM procrastinate.procrastinate_jobs")


@pytest.fixture
async def edition(conn: psycopg.AsyncConnection) -> SimpleNamespace:
    """One registered edition; exposes ``id`` for policy/decision inserts."""
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk', 'Berdyansk') RETURNING id"
    )
    row = await cursor.fetchone()
    return SimpleNamespace(id=row[0])


@pytest.fixture
async def second_edition(conn: psycopg.AsyncConnection) -> SimpleNamespace:
    """A second edition used to prove edition-scoping of policies."""
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('mariupol', 'Mariupol') RETURNING id"
    )
    row = await cursor.fetchone()
    return SimpleNamespace(id=row[0])


@pytest.fixture
async def revision(conn: psycopg.AsyncConnection) -> SimpleNamespace:
    """One source item revision; exposes ``id`` for decision/run inserts."""
    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name)
        VALUES ('telegram', 'channel', '-1004242', 'https://t.me/example', 'Example')
        RETURNING id
        """
    )
    source_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', '42', now())
        RETURNING id
        """,
        (source_id,),
    )
    item_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'hash-1', 'На АКЗ возле почты вода уже появилась')
        RETURNING id
        """,
        (item_id,),
    )
    row = await cursor.fetchone()
    return SimpleNamespace(id=row[0], item_id=item_id)


@pytest.fixture
async def pool(conn, database_config: DatabaseConfig) -> AsyncIterator[AsyncConnectionPool]:
    """Pooled connections over the same truncated slice as ``conn``."""
    pool = await open_pool(database_config)
    try:
        yield pool
    finally:
        await close_pool(pool)


@pytest.fixture
def uow(pool) -> DatabaseUnitOfWork:
    return DatabaseUnitOfWork(pool)


@pytest.fixture
async def revision_factory(conn: psycopg.AsyncConnection):
    """Create fresh source item revisions with custom text on demand."""
    counter = {"n": 0}

    async def _make(
        *, text_content: str | None = "Какой-то текст сообщения", with_photo: bool = False
    ) -> SimpleNamespace:
        counter["n"] += 1
        n = counter["n"]
        cursor = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name)
            VALUES ('telegram', 'channel', %s, 'https://t.me/example', 'Factory')
            RETURNING id
            """,
            (f"factory-{n}",),
        )
        source_id = (await cursor.fetchone())[0]
        cursor = await conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
            VALUES (%s, 'message', %s, now())
            RETURNING id
            """,
            (source_id, f"factory-item-{n}"),
        )
        item_id = (await cursor.fetchone())[0]
        cursor = await conn.execute(
            """
            INSERT INTO source_item_revisions (
                source_item_id, revision_no, content_hash, text_content
            )
            VALUES (%s, 1, %s, %s)
            RETURNING id
            """,
            (item_id, f"factory-hash-{n}", text_content),
        )
        revision_id = (await cursor.fetchone())[0]
        if with_photo:
            await conn.execute(
                """
                INSERT INTO source_assets (
                    source_item_revision_id, kind, mime_type, content_hash, metadata
                )
                VALUES (%s, 'photo', 'image/jpeg', 'asset-hash', '{}'::jsonb)
                """,
                (revision_id,),
            )
        return SimpleNamespace(id=revision_id, item_id=item_id, source_id=source_id)

    return _make


@pytest.fixture(scope="session")
def procrastinate_schema_ready() -> None:
    """Ensure the official Procrastinate tables exist in the test database."""
    import asyncio

    from src.jobs.admin import ensure_official_tables

    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    asyncio.run(ensure_official_tables(url, "procrastinate"))


@pytest.fixture
def jobs_import_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Environment so importing src.jobs.app works without repo-root DB config.

    The production module builds ``procrastinate_app`` at import time from
    ``load_database_config(require_enabled=True)``, so the test database URL is
    injected into DATABASE_URL and a minimal enabled config.yaml is provided
    from a temporary working directory before the first import.
    """
    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    monkeypatch.setenv("DATABASE_URL", url)
    (tmp_path / "config.yaml").write_text(
        "database:\n  enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return url


@pytest.fixture
async def production_jobs_app(
    jobs_import_env: str, procrastinate_schema_ready: None
) -> AsyncIterator[procrastinate.App]:
    """Opened production Procrastinate app bound to the test database.

    Production tasks (evaluate_relevance) are registered against the singleton
    app in src.jobs.app; opening that singleton lets tests defer real tasks.
    """
    from src.jobs.app import procrastinate_app

    async with procrastinate_app.open_async():
        yield procrastinate_app
