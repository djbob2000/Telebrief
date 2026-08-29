"""Fixtures for integration tests under tests/integration."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

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
def ensure_test_env() -> None:
    if "TELEBRIEF_TEST_DATABASE_URL" not in os.environ:
        return
    url = os.environ["TELEBRIEF_TEST_DATABASE_URL"]
    os.environ["DATABASE_URL"] = url


@pytest.fixture(scope="session", autouse=True)
async def ensure_schema_migrations() -> None:
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
             publication_input_fragments,
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
             story_revision_embeddings, story_revisions,
             story_event_analysis_runs, story_edition_scope_decisions,
             story_event_triage_decisions, story_event_triage_runs,
             story_cluster_state, story_fragments, stories,
             source_fragment_embeddings, fragment_embedding_vectors, event_embedding_batches,
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
             collector_artifacts, facebook_comment_collection_state,
             facebook_source_configs, facebook_auth_profiles,
             source_items, source_item_revisions, source_assets,
             source_item_state_events, collection_checkpoints,
             collection_runs, source_editions, sources, editions
    RESTART IDENTITY CASCADE;
"""


@pytest.fixture
async def conn(database_config: DatabaseConfig) -> AsyncIterator[psycopg.AsyncConnection]:
    from pgvector.psycopg import register_vector_async

    conn = await psycopg.AsyncConnection.connect(database_config.url, autocommit=True)
    await register_vector_async(conn)
    await conn.execute(_TRUNCATE_TABLES)
    try:
        yield conn
    finally:
        await conn.execute(_TRUNCATE_TABLES)
        await conn.close()


@pytest.fixture
async def pool(conn, database_config: DatabaseConfig) -> AsyncIterator[AsyncConnectionPool]:
    pool = await open_pool(database_config)
    try:
        yield pool
    finally:
        await close_pool(pool)


@pytest.fixture
def uow(pool: AsyncConnectionPool) -> DatabaseUnitOfWork:
    return DatabaseUnitOfWork(pool)


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
    await conn.commit()
    return SimpleNamespace(
        id=row[0],
        slug=row[1],
        name=row[2],
        timezone=row[3],
        language=row[4],
        enabled=row[5],
    )
