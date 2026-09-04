"""Fixtures for PostgreSQL-backed publication tests in tests/publication.

Gated on TELEBRIEF_TEST_DATABASE_URL.
"""

from __future__ import annotations

import datetime as dt
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
            if item.get_closest_marker("unit") is not None:
                continue
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
             story_event_analysis_runs, story_edition_scope_decisions, story_event_triage_decisions, story_event_triage_runs,
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
    """One registered edition; exposes ``id`` and ``name`` for test inserts."""
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk', 'Berdyansk') RETURNING id, name"
    )
    row = await cursor.fetchone()
    return SimpleNamespace(id=row[0], name=row[1])


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


async def seed_claim_for_story(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    story_id: int,
    created_at: dt.datetime | None = None,
    platform: str = "telegram",
) -> int:
    now = created_at or dt.datetime.now(dt.timezone.utc)
    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES (%s, 'channel', %s, %s, 'Chan', 'official')
        RETURNING id
        """,
        (platform, f"ext-{story_id}-{now.timestamp()}", f"https://t.me/c{story_id}"),
    )
    source_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_items (source_id, kind, external_id, first_collected_at, published_at) VALUES (%s, 'msg', %s, %s, %s) RETURNING id",
        (source_id, f"item-{story_id}-{now.timestamp()}", now, now),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h', 'txt', %s) RETURNING id",
        (item_id, now),
    )
    rev_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version)
        VALUES (%s, 1, 'h-rel', 'v-rel')
        ON CONFLICT (edition_id, version) DO UPDATE SET config_hash = EXCLUDED.config_hash
        RETURNING id
        """,
        (edition_id,),
    )
    rel_pol_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason)
        VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id
        """,
        (rev_id, edition_id, rel_pol_id),
    )
    rel_dec_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version)
        VALUES (%s, 1, 'h', 'v')
        ON CONFLICT (edition_id, version) DO UPDATE SET config_hash = EXCLUDED.config_hash
        RETURNING id
        """,
        (edition_id,),
    )
    extr_pol_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status)
        VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id
        """,
        (rev_id, edition_id, extr_pol_id, rel_dec_id),
    )
    extr_run_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at)
        VALUES (%s, %s, %s, 'Утверждение новости', 'утверждение новости', %s)
        RETURNING id
        """,
        (extr_run_id, rev_id, edition_id, now),
    )
    claim_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (story_id, claim_id, now),
    )
    return claim_id
