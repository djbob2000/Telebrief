"""Tests for StoryTriageService (batch triage of low-support stories)."""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock

import pytest

from src.processing.event_triage import StoryTriageService
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.stories import StoryRepository


@pytest.mark.postgres
async def test_story_triage_service_batch_flow(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    # Create 2 story shells
    sid1 = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    sid2 = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    # Insert fragments & cluster states
    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (8001, 'hash_tr1', '[1, 0]'::vector, 'test-model', 2),
        (8002, 'hash_tr2', '[0, 1]'::vector, 'test-model', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (9001, %s, 0, 'Real municipal repair on Lenin street', 'hash_tr1', 'v1', TRUE, NULL, %s),
        (9002, %s, 1, 'Selling refrigerator 5000 rub call now', 'hash_tr2', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now, revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (10001, 9001, 8001),
        (10002, 9002, 8002)
        """
    )

    aid1 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid1,
        fragment_id=9001,
        fragment_embedding_id=10001,
        assignment_kind="new_story",
    )
    aid2 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid2,
        fragment_id=9002,
        fragment_embedding_id=10002,
        assignment_kind="new_story",
    )

    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=sid1,
        centroid=[1.0, 0.0],
        model="test-model",
        dimensions=2,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid1,
        analysis_dirty=True,
    )
    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=sid2,
        centroid=[0.0, 1.0],
        model="test-model",
        dimensions=2,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid2,
        analysis_dirty=True,
    )

    s1 = await cluster_repo.get_cluster_state(conn, sid1)
    s2 = await cluster_repo.get_cluster_state(conn, sid2)
    assert s1 is not None and s2 is not None

    # Mock LLM: sid1 -> ANALYZE, sid2 -> IGNORE (commercial)
    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "mock_provider"
    mock_ai.model_name = "mock-model"
    triage_response = [
        {
            "story_id": sid1,
            "decision": "ANALYZE",
            "exclusion_reason": None,
            "confidence": 0.98,
            "reason": "Municipal news",
        },
        {
            "story_id": sid2,
            "decision": "IGNORE",
            "exclusion_reason": "commercial_classified",
            "confidence": 0.99,
            "reason": "Selling appliance",
        },
    ]
    mock_ai.generate_text.return_value = json.dumps(triage_response)

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    to_analyze = await service.triage_stories_batch(conn, [s1, s2])

    # sid1 should be approved for analysis, sid2 should be ignored
    assert to_analyze == [sid1]

    # Check that sid2 cluster is marked analyzed and no longer dirty
    s2_updated = await cluster_repo.get_cluster_state(conn, sid2)
    assert s2_updated is not None
    assert s2_updated.analysis_dirty is False
    assert s2_updated.last_analyzed_assignment_id == aid2

    # Check triage run and decision records in database
    cursor = await conn.execute("SELECT status, story_count FROM story_event_triage_runs")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "succeeded"
    assert row[1] == 2

    cursor = await conn.execute(
        "SELECT story_id, decision, exclusion_reason FROM story_event_triage_decisions ORDER BY story_id"
    )
    decisions = await cursor.fetchall()
    assert len(decisions) == 2
    assert decisions[0][0] == sid1 and decisions[0][1] == "ANALYZE"
    assert (
        decisions[1][0] == sid2
        and decisions[1][1] == "IGNORE"
        and decisions[1][2] == "commercial_classified"
    )
