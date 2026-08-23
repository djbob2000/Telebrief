"""Fixtures for PostgreSQL-backed publication tests in tests/publication.

Gated on TELEBRIEF_TEST_DATABASE_URL.
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
from src.db.migrations import migrate
from src.db.pool import close_pool, open_pool
from src.db.uow import DatabaseUnitOfWork

REPO_ROOT = Path(__file__).parent.parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"


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


@pytest.fixture(scope="session", autouse=True)
async def ensure_schema_migrations() -> None:
    """Ensure all migrations (including 0008_publications.sql) are applied."""
    if "TELEBRIEF_TEST_DATABASE_URL" not in os.environ:
        return
    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await migrate(conn, MIGRATIONS_DIR)


_TRUNCATE_TABLES = """
    TRUNCATE publication_delivery_attempts, publication_deliveries,
             publication_delivery_payloads, delivery_destinations,
             publications, publication_generation_attempts,
             publication_input_evidence_clusters, publication_input_claims,
             publication_inputs, publication_selection_decisions,
             publication_candidates, publication_runs,
             publication_policy_versions, writer_policy_versions,
             editorial_selection_policy_versions, eligibility_policy_versions,
             verification_assessments, verification_policy_versions,
             evidence_cluster_members, evidence_clusters,
             evidence_assessment_run_claims, evidence_assessment_runs,
             evidence_assessment_policy_versions,
             story_relation_proposals, story_match_decisions,
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


async def _clear_slice(conn: psycopg.AsyncConnection) -> None:
    await conn.execute(_TRUNCATE_TABLES)
    await conn.execute("SET search_path TO public, procrastinate")
    await conn.execute("DELETE FROM procrastinate.procrastinate_jobs")


@pytest.fixture
async def conn(database_config: DatabaseConfig) -> AsyncIterator[psycopg.AsyncConnection]:
    """Autocommit connection to a clean slice of the test database."""
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        database_config.url, autocommit=True
    )
    try:
        await _clear_slice(conn)
        yield conn
    finally:
        await _clear_slice(conn)
        await conn.close()


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
async def edition(conn: psycopg.AsyncConnection) -> SimpleNamespace:
    """One registered edition; exposes ``id`` for test inserts."""
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk', 'Berdyansk') RETURNING id"
    )
    row = await cursor.fetchone()
    return SimpleNamespace(id=row[0])


@pytest.fixture(scope="session")
def procrastinate_schema_ready() -> None:
    """Ensure the official Procrastinate tables exist in the test database."""
    import asyncio

    from src.jobs.admin import ensure_official_tables

    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    asyncio.run(ensure_official_tables(url, "procrastinate"))


@pytest.fixture
def jobs_import_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
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
    from src.jobs.app import procrastinate_app

    async with procrastinate_app.open_async():
        yield procrastinate_app
