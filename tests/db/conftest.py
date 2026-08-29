"""Fixtures for database-level invariant tests in tests/db."""

from __future__ import annotations

import asyncio
import os
import pathlib

import psycopg
import pytest
from pgvector.psycopg import register_vector_async

from src.config_loader import DatabaseConfig

_TRUNCATE_TABLES = """
    TRUNCATE verification_assessments, verification_policy_versions,
             evidence_cluster_members, evidence_clusters,
             evidence_assessment_run_claims, evidence_assessment_runs,
             evidence_assessment_policy_versions,
             story_relation_proposals, story_match_decisions,
             story_matching_candidates, story_matching_runs,
             story_matching_policy_versions,
             story_relations, story_state_events, story_claims,
             story_revision_embeddings, story_revisions,
             story_event_analysis_runs, story_edition_scope_decisions, story_event_triage_decisions, story_event_triage_runs,
             story_cluster_state, story_fragments, stories,
             source_fragment_embeddings, fragment_embedding_vectors, event_embedding_batches,
             publication_input_fragments,
             source_fragments,
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
    gate = pathlib.Path(__file__).parent.resolve()
    for item in items:
        if item.path.resolve().is_relative_to(gate):
            item.add_marker(
                pytest.mark.skipif(
                    "TELEBRIEF_TEST_DATABASE_URL" not in os.environ,
                    reason="TELEBRIEF_TEST_DATABASE_URL is not set",
                )
            )


@pytest.fixture(scope="session")
def procrastinate_schema_ready() -> None:
    """Ensure the official Procrastinate tables exist in the test database."""
    if "TELEBRIEF_TEST_DATABASE_URL" not in os.environ:
        return
    from src.jobs.admin import ensure_official_tables

    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    asyncio.run(ensure_official_tables(url, "procrastinate"))


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


@pytest.fixture
async def pg_conn(database_config: DatabaseConfig):
    """Autocommit connection ensuring standard migrations are applied."""
    conn = await psycopg.AsyncConnection.connect(database_config.url, autocommit=True)
    await conn.execute("DELETE FROM telebrief_schema_migrations WHERE version >= 900000")
    from src.db.migrations import migrate

    await migrate(conn, MIGRATIONS_DIR)
    try:
        yield conn
    finally:
        await conn.execute("DELETE FROM telebrief_schema_migrations WHERE version >= 900000")
        await migrate(conn, MIGRATIONS_DIR)
        await conn.close()


@pytest.fixture
async def isolated_pg_conn(database_config: DatabaseConfig):
    """Isolated autocommit connection for individual test probes."""
    conn = await psycopg.AsyncConnection.connect(database_config.url, autocommit=True)
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector CASCADE")
    await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm CASCADE")
    await register_vector_async(conn)
    try:
        await conn.execute(
            "DROP TABLE IF EXISTS rollback_probe_ok, nontransactional_probe, order_probe_900003, order_probe_900004 CASCADE"
        )
        await conn.execute("TRUNCATE telebrief_schema_migrations")
        yield conn
    finally:
        await conn.execute(
            "DROP TABLE IF EXISTS rollback_probe_ok, nontransactional_probe, order_probe_900003, order_probe_900004 CASCADE"
        )
        await conn.execute("TRUNCATE telebrief_schema_migrations")
        from src.db.migrations import migrate

        await migrate(conn, MIGRATIONS_DIR)
        await conn.close()


@pytest.fixture
async def conn(database_config: DatabaseConfig):
    """Autocommit connection to a clean slice of the test database."""
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        database_config.url, autocommit=True
    )
    await register_vector_async(conn)
    try:
        await conn.execute(_TRUNCATE_TABLES)
        yield conn
    finally:
        await conn.execute(_TRUNCATE_TABLES)
        await conn.close()
