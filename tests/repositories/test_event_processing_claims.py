"""Tests for fenced durable Event-First execution claims."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from src.repositories.event_processing_claims import EventProcessingClaimRepository


async def _seed_edition(conn, slug: str) -> int:
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES (%s, %s) RETURNING id",
        (slug, slug.replace("-", " ").title()),
    )
    return int((await cursor.fetchone())[0])


async def _seed_story_assignment(conn) -> tuple[int, int, int]:
    token = uuid4().hex
    edition_id = await _seed_edition(conn, f"claim-story-{token}")
    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, name)
        VALUES ('telegram', 'channel', %s, 'Claim Source')
        RETURNING id
        """,
        (f"claim-source-{token}",),
    )
    source_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', %s, now())
        RETURNING id
        """,
        (source_id, f"claim-message-{token}"),
    )
    item_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, %s, 'claim text')
        RETURNING id
        """,
        (item_id, f"claim-revision-{token}"),
    )
    revision_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_fragments
            (source_item_revision_id, ordinal, text_content, normalized_hash,
             fragmenter_version, is_candidate)
        VALUES (%s, 0, 'claim text', %s, 'v1', TRUE)
        RETURNING id
        """,
        (revision_id, f"claim-fragment-{token}"),
    )
    fragment_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
        VALUES (%s, '[1, 0]'::vector, 'test', 2)
        RETURNING id
        """,
        (f"claim-fragment-{token}",),
    )
    vector_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
        VALUES (%s, %s)
        RETURNING id
        """,
        (fragment_id, vector_id),
    )
    embedding_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source)
        VALUES (%s, 'active', 'event_first')
        RETURNING id
        """,
        (edition_id,),
    )
    story_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO story_fragments (story_id, fragment_id, fragment_embedding_id, assignment_kind)
        VALUES (%s, %s, %s, 'new_story')
        RETURNING id
        """,
        (story_id, fragment_id, embedding_id),
    )
    assignment_id = int((await cursor.fetchone())[0])
    return edition_id, story_id, assignment_id


@pytest.mark.postgres
async def test_cycle_claim_is_exclusive_until_expiry(repo_conn):
    repository = EventProcessingClaimRepository()
    edition_id = await _seed_edition(repo_conn, "cycle-exclusive")

    first = await repository.acquire_cycle(
        repo_conn, edition_id=edition_id, owner_id="worker-a", ttl_seconds=600
    )
    assert first is not None
    second = await repository.acquire_cycle(
        repo_conn, edition_id=edition_id, owner_id="worker-b", ttl_seconds=600
    )
    assert second is None


@pytest.mark.postgres
async def test_stale_cycle_token_cannot_spend_or_release_replacement(repo_conn):
    repository = EventProcessingClaimRepository()
    edition_id = await _seed_edition(repo_conn, "cycle-stale")
    claim = await repository.acquire_cycle(
        repo_conn, edition_id=edition_id, owner_id="worker-a", ttl_seconds=600
    )
    assert claim is not None
    stale = replace(claim, claim_token="stale-token")
    assert await repository.claim_rich_slot(repo_conn, stale, max_calls=1, ttl_seconds=600) is False
    assert await repository.release_cycle(repo_conn, stale) is False
    assert await repository.claim_rich_slot(repo_conn, claim, max_calls=1, ttl_seconds=600) is True


@pytest.mark.postgres
async def test_stage_claim_is_assignment_scoped(repo_conn):
    _, story_id, assignment_id = await _seed_story_assignment(repo_conn)
    repository = EventProcessingClaimRepository()

    first = await repository.try_claim_stage(
        repo_conn,
        story_id=story_id,
        latest_assignment_id=assignment_id,
        stage="triage",
        owner_id="worker-a",
        ttl_seconds=600,
    )
    assert first is not None
    duplicate = await repository.try_claim_stage(
        repo_conn,
        story_id=story_id,
        latest_assignment_id=assignment_id,
        stage="triage",
        owner_id="worker-b",
        ttl_seconds=600,
    )
    assert duplicate is None


@pytest.mark.postgres
async def test_stage_claim_fence_locks_only_live_exact_token(repo_conn):
    _, story_id, assignment_id = await _seed_story_assignment(repo_conn)
    repository = EventProcessingClaimRepository()
    claim = await repository.try_claim_stage(
        repo_conn,
        story_id=story_id,
        latest_assignment_id=assignment_id,
        stage="triage",
        owner_id="worker-a",
        ttl_seconds=600,
    )
    assert claim is not None
    assert await repository.lock_stage_claim_if_live(repo_conn, claim) is True
    assert (
        await repository.lock_stage_claim_if_live(
            repo_conn, replace(claim, claim_token="replaced-token")
        )
        is False
    )


@pytest.mark.postgres
async def test_cycle_call_slot_is_durable_and_bounded(repo_conn):
    repository = EventProcessingClaimRepository()
    edition_id = await _seed_edition(repo_conn, "cycle-budget")
    claim = await repository.acquire_cycle(
        repo_conn, edition_id=edition_id, owner_id="worker-a", ttl_seconds=600
    )
    assert claim is not None
    assert await repository.claim_rich_slot(repo_conn, claim, max_calls=2, ttl_seconds=600)
    assert await repository.claim_rich_slot(repo_conn, claim, max_calls=2, ttl_seconds=600)
    assert not await repository.claim_rich_slot(repo_conn, claim, max_calls=2, ttl_seconds=600)

    cursor = await repo_conn.execute(
        "SELECT rich_calls_started FROM event_processing_cycle_leases WHERE edition_id = %s",
        (edition_id,),
    )
    assert (await cursor.fetchone())[0] == 2
