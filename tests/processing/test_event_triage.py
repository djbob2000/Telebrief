"""Tests for StoryTriageService (batch triage and scope classification)."""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock

import pytest

from src.config_loader import EditionScopeConfig
from src.processing.edition_scope import scope_config_hash
from src.processing.event_triage import StoryTriageService
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.stories import StoryRepository


@pytest.mark.postgres
async def test_story_triage_service_batch_flow(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    # Create 3 story shells
    local_sid = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    external_sid = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    impact_sid = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    # Insert fragments & cluster states
    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (8001, 'hash_tr1', '[1, 0]'::vector, 'test-model', 2),
        (8002, 'hash_tr2', '[0, 1]'::vector, 'test-model', 2),
        (8003, 'hash_tr3', '[1, 1]'::vector, 'test-model', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (9001, %s, 0, 'Real municipal repair in Berdyansk on Lenin street', 'hash_tr1', 'v1', TRUE, NULL, %s),
        (9002, %s, 1, 'Major warehouse explosion in Bucha near Kyiv', 'hash_tr2', 'v1', TRUE, NULL, %s),
        (9003, %s, 2, 'External line failure caused blackout in Berdyansk', 'hash_tr3', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now, revision.id, now, revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (10001, 9001, 8001),
        (10002, 9002, 8002),
        (10003, 9003, 8003)
        """
    )

    aid1 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=local_sid,
        fragment_id=9001,
        fragment_embedding_id=10001,
        assignment_kind="new_story",
    )
    aid2 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=external_sid,
        fragment_id=9002,
        fragment_embedding_id=10002,
        assignment_kind="new_story",
    )
    aid3 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=impact_sid,
        fragment_id=9003,
        fragment_embedding_id=10003,
        assignment_kind="new_story",
    )

    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=local_sid,
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
        story_id=external_sid,
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
    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=impact_sid,
        centroid=[1.0, 1.0],
        model="test-model",
        dimensions=2,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid3,
        analysis_dirty=True,
    )

    s1 = await cluster_repo.get_cluster_state(conn, local_sid)
    s2 = await cluster_repo.get_cluster_state(conn, external_sid)
    s3 = await cluster_repo.get_cluster_state(conn, impact_sid)
    assert s1 is not None and s2 is not None and s3 is not None

    scope_config = EditionScopeConfig(
        name="Бердянск",
        focus_places=("Бердянск", "Осипенко", "Азовское"),
        direct_impact_only=True,
    )
    scope_hash = scope_config_hash(scope_config)

    # Mock LLM response with LOCAL, OUT_OF_SCOPE, DIRECT_IMPACT
    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "mock_provider"
    mock_ai.model_name = "mock-model"
    triage_response = {
        "results": [
            {
                "story_id": local_sid,
                "scope": "LOCAL",
                "scope_confidence": 0.99,
                "scope_reason": "The event text explicitly names the target city.",
                "decision": "ANALYZE",
                "exclusion_reason": None,
                "confidence": 0.98,
                "reason": "Legitimate local event.",
            },
            {
                "story_id": external_sid,
                "scope": "OUT_OF_SCOPE",
                "scope_confidence": 0.99,
                "scope_reason": "The event is explicitly in another city and no target impact is stated.",
                "decision": "ANALYZE",
                "exclusion_reason": None,
                "confidence": 0.99,
                "reason": "Important event, but outside edition scope.",
            },
            {
                "story_id": impact_sid,
                "scope": "DIRECT_IMPACT",
                "scope_confidence": 0.96,
                "scope_reason": "The external grid failure explicitly caused an outage in the target city.",
                "decision": "ANALYZE",
                "exclusion_reason": None,
                "confidence": 0.96,
                "reason": "Direct local infrastructure consequence.",
            },
        ]
    }
    mock_ai.generate_text.return_value = json.dumps(triage_response)

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    batch_res = await service.triage_stories_batch(
        conn,
        [s1, s2, s3],
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
    )

    assert len(batch_res.results) == 3
    assert batch_res.deferred_story_ids == ()

    # Check scope decisions in DB
    cursor = await conn.execute(
        """
        SELECT story_id, scope_class, confidence, scope_config_hash
        FROM story_edition_scope_decisions
        ORDER BY story_id
        """
    )
    scope_rows = await cursor.fetchall()
    assert len(scope_rows) == 3
    assert scope_rows[0][0] == local_sid and scope_rows[0][1] == "LOCAL"
    assert scope_rows[1][0] == external_sid and scope_rows[1][1] == "OUT_OF_SCOPE"
    assert scope_rows[2][0] == impact_sid and scope_rows[2][1] == "DIRECT_IMPACT"
    assert all(r[3] == scope_hash for r in scope_rows)

    # Check triage run and decision records in database
    cursor = await conn.execute("SELECT status, story_count FROM story_event_triage_runs")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "succeeded"
    assert row[1] == 3


@pytest.mark.postgres
async def test_story_triage_service_provider_failure_defers_all(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    sid1 = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    sid2 = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (8011, 'hash_f1', '[1, 0]'::vector, 'test-model', 2),
        (8012, 'hash_f2', '[0, 1]'::vector, 'test-model', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (9011, %s, 0, 'Post 1', 'hash_f1', 'v1', TRUE, NULL, %s),
        (9012, %s, 1, 'Post 2', 'hash_f2', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now, revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (10011, 9011, 8011),
        (10012, 9012, 8012)
        """
    )

    aid1 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid1,
        fragment_id=9011,
        fragment_embedding_id=10011,
        assignment_kind="new_story",
    )
    aid2 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid2,
        fragment_id=9012,
        fragment_embedding_id=10012,
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

    scope_config = EditionScopeConfig(name="City", focus_places=("City",))
    scope_hash = scope_config_hash(scope_config)

    mock_ai = AsyncMock()
    mock_ai.generate_text.side_effect = RuntimeError("provider down")

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    result = await service.triage_stories_batch(
        conn,
        [s1, s2],
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
    )

    assert result.results == ()
    assert set(result.deferred_story_ids) == {sid1, sid2}


@pytest.mark.postgres
async def test_story_triage_service_missing_and_invalid_results_deferred(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    sids = []
    for _ in range(4):
        sids.append(
            await story_repo.create_story_shell(
                conn, edition_id=edition.id, knowledge_source="event_first"
            )
        )

    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (8021, 'h1', '[1, 0]'::vector, 'm', 2),
        (8022, 'h2', '[0, 1]'::vector, 'm', 2),
        (8023, 'h3', '[1, 1]'::vector, 'm', 2),
        (8024, 'h4', '[0, 0]'::vector, 'm', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (9021, %s, 0, 'Text 1', 'h1', 'v1', TRUE, NULL, %s),
        (9022, %s, 1, 'Text 2', 'h2', 'v1', TRUE, NULL, %s),
        (9023, %s, 2, 'Text 3', 'h3', 'v1', TRUE, NULL, %s),
        (9024, %s, 3, 'Text 4', 'h4', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now, revision.id, now, revision.id, now, revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (10021, 9021, 8021),
        (10022, 9022, 8022),
        (10023, 9023, 8023),
        (10024, 9024, 8024)
        """
    )

    states = []
    for idx, sid in enumerate(sids):
        aid = await cluster_repo.assign_fragment_to_story(
            conn,
            story_id=sid,
            fragment_id=9021 + idx,
            fragment_embedding_id=10021 + idx,
            assignment_kind="new_story",
        )
        await cluster_repo.upsert_cluster_state(
            conn,
            story_id=sid,
            centroid=[1.0, 0.0],
            model="m",
            dimensions=2,
            fragment_count=1,
            unique_source_count=1,
            first_seen_at=now,
            last_seen_at=now,
            latest_assignment_id=aid,
            analysis_dirty=True,
        )
        st = await cluster_repo.get_cluster_state(conn, sid)
        assert st is not None
        states.append(st)

    scope_config = EditionScopeConfig(name="City", focus_places=("City",))
    scope_hash = scope_config_hash(scope_config)

    # sids[0]: valid
    # sids[1]: missing from response
    # sids[2]: invalid scope ("REGIONAL")
    # sids[3]: invalid scope_confidence (1.5)
    mock_ai = AsyncMock()
    triage_response = {
        "results": [
            {
                "story_id": sids[0],
                "scope": "LOCAL",
                "scope_confidence": 0.95,
                "scope_reason": "In city",
                "decision": "ANALYZE",
                "exclusion_reason": None,
                "confidence": 0.9,
                "reason": "OK",
            },
            {
                "story_id": sids[2],
                "scope": "REGIONAL",
                "scope_confidence": 0.95,
                "scope_reason": "In region",
                "decision": "ANALYZE",
                "exclusion_reason": None,
                "confidence": 0.9,
                "reason": "OK",
            },
            {
                "story_id": sids[3],
                "scope": "LOCAL",
                "scope_confidence": 1.5,
                "scope_reason": "In city",
                "decision": "ANALYZE",
                "exclusion_reason": None,
                "confidence": 0.9,
                "reason": "OK",
            },
        ]
    }
    mock_ai.generate_text.return_value = json.dumps(triage_response)

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    result = await service.triage_stories_batch(
        conn,
        states,
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
    )

    assert len(result.results) == 1
    assert result.results[0].story_id == sids[0]
    assert set(result.deferred_story_ids) == {sids[1], sids[2], sids[3]}
