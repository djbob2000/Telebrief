"""Fixtures for PostgreSQL-backed script tests in tests/scripts."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from pgvector.psycopg import register_vector_async
from psycopg_pool import AsyncConnectionPool

from src.config_loader import DatabaseConfig
from src.db.pool import close_pool, open_pool
from src.db.uow import DatabaseUnitOfWork

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
    conn: psycopg.AsyncConnection = await psycopg.AsyncConnection.connect(
        database_config.url, autocommit=True
    )
    await register_vector_async(conn)
    try:
        await _clear_slice(conn)
        yield conn
    finally:
        await _clear_slice(conn)
        await conn.close()


async def _clear_slice(conn: psycopg.AsyncConnection) -> None:
    await conn.execute(_TRUNCATE_TABLES)
    await conn.execute("SET search_path TO public, procrastinate")
    await conn.execute("DELETE FROM procrastinate.procrastinate_jobs")


@pytest.fixture
async def pool(database_config: DatabaseConfig) -> AsyncIterator[AsyncConnectionPool]:
    p = await open_pool(database_config)
    try:
        yield p
    finally:
        await close_pool(p)


@pytest.fixture
def uow(pool: AsyncConnectionPool) -> DatabaseUnitOfWork:
    return DatabaseUnitOfWork(pool)


@pytest.fixture
async def edition(conn: psycopg.AsyncConnection) -> SimpleNamespace:
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk', 'Berdyansk') RETURNING id, slug, name"
    )
    row = await cursor.fetchone()
    return SimpleNamespace(id=row[0], slug=row[1], name=row[2])
