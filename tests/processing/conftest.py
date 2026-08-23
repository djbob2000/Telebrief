"""Fixtures for PostgreSQL-backed processing tests in tests/processing.

Tests here are gated on TELEBRIEF_TEST_DATABASE_URL: when the variable is not
set, every test collected under tests/processing is skipped instead of
failing.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from src.config_loader import DatabaseConfig


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
        await conn.execute(
            """
            TRUNCATE claim_state_events, claim_relations, claims,
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
        )
        yield conn
    finally:
        await conn.execute(
            """
            TRUNCATE claim_state_events, claim_relations, claims,
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
        )
        await conn.close()


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
