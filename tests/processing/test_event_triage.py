"""Tests for StoryTriageService (Gate V2 batch scope, retention, enrichment, and brief synthesis)."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.config_loader import EditionScopeConfig
from src.processing.edition_scope import scope_config_hash
from src.processing.event_triage import (
    StoryTriageService,
)
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

    # Mock LLM response with LOCAL (KEEP+BRIEF), OUT_OF_SCOPE (DROP+NONE), DIRECT_IMPACT (KEEP+ANALYZE)
    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "mock_provider"
    mock_ai.model_name = "mock-model"
    triage_response = {
        "results": [
            {
                "story_id": local_sid,
                "scope": "LOCAL",
                "scope_basis_fragment_ids": [9001],
                "scope_confidence": 0.99,
                "scope_reason": "The event text explicitly names the target city.",
                "retention": "KEEP",
                "enrichment": "BRIEF",
                "exclusion_reason": None,
                "confidence": 0.98,
                "reason": "Legitimate local event.",
                "brief_payload": {
                    "enrichment_level": "brief",
                    "topic": "Ремонт на улице Ленина",
                    "tags": ["ремонт", "коммуналка"],
                    "urgency": "normal",
                    "publishability": "brief",
                    "headline": "Ремонтные работы в Бердянске",
                    "digest_summary": "На улице Ленина проводятся коммунальные работы.",
                    "evidence_items": [
                        {
                            "text": "Ремонтные работы на улице Ленина",
                            "kind": "service_access",
                            "publication_use": "PUBLISH",
                            "source_fragment_ids": [9001],
                            "service_state": {
                                "subject_key": "municipal_repair",
                                "subject_label": "Ремонтные работы",
                                "dimension": "status",
                                "state": "RESTRICTED",
                                "location": "Ленина",
                                "entity": "дорога",
                                "expected_now": True,
                                "basis": "explicit_restriction",
                            },
                        }
                    ],
                },
            },
            {
                "story_id": external_sid,
                "scope": "OUT_OF_SCOPE",
                "scope_basis_fragment_ids": [],
                "scope_confidence": 0.99,
                "scope_reason": "The event is explicitly in another city and no target impact is stated.",
                "retention": "DROP",
                "enrichment": "NONE",
                "exclusion_reason": None,
                "confidence": 0.99,
                "reason": "Important event, but outside edition scope.",
                "brief_payload": None,
            },
            {
                "story_id": impact_sid,
                "scope": "DIRECT_IMPACT",
                "scope_basis_fragment_ids": [9003],
                "scope_confidence": 0.96,
                "scope_reason": "The external grid failure explicitly caused an outage in the target city.",
                "retention": "KEEP",
                "enrichment": "ANALYZE",
                "exclusion_reason": None,
                "confidence": 0.96,
                "reason": "Direct local infrastructure consequence.",
                "brief_payload": {
                    "enrichment_level": "brief",
                    "topic": "Отключение электроэнергии",
                    "tags": ["электричество", "авария"],
                    "urgency": "high",
                    "publishability": "news",
                    "headline": "Блэкаут из-за аварии на внешней линии",
                    "digest_summary": "Авария на внешней линии привела к отключению света в Бердянске.",
                    "evidence_items": [
                        {
                            "text": "Авария на внешней линии привела к отключению света в Бердянске",
                            "kind": "service_access",
                            "publication_use": "PUBLISH",
                            "source_fragment_ids": [9003],
                            "service_state": {
                                "subject_key": "power_supply",
                                "subject_label": "Электроснабжение",
                                "dimension": "availability",
                                "state": "UNAVAILABLE",
                                "location": "Бердянск",
                                "entity": "ЛЭП",
                                "expected_now": True,
                                "basis": "direct_failure",
                            },
                        }
                    ],
                },
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

    # Check Gate V2 triage records in DB
    cursor = await conn.execute(
        """
        SELECT story_id, retention, enrichment, brief_payload IS NOT NULL
        FROM story_event_triage_decisions
        ORDER BY story_id
        """
    )
    triage_rows = await cursor.fetchall()
    assert len(triage_rows) == 3
    assert triage_rows[0] == (local_sid, "KEEP", "BRIEF", True)
    assert triage_rows[1] == (external_sid, "DROP", "NONE", False)
    assert triage_rows[2] == (impact_sid, "KEEP", "ANALYZE", True)


@pytest.mark.postgres
async def test_story_triage_service_cache_lookup_avoids_llm_call(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    sid = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES (8050, 'h50', '[1, 0]'::vector, 'm', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES (9050, %s, 0, 'Cached post', 'h50', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES (10050, 9050, 8050)
        """
    )
    aid = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid,
        fragment_id=9050,
        fragment_embedding_id=10050,
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

    scope_config = EditionScopeConfig(name="City", focus_places=("City",))
    scope_hash = scope_config_hash(scope_config)

    # 1. First run with AI
    mock_ai = AsyncMock()
    mock_ai.generate_text.return_value = json.dumps(
        {
            "results": [
                {
                    "story_id": sid,
                    "scope": "LOCAL",
                    "scope_basis_fragment_ids": [9050],
                    "scope_confidence": 0.99,
                    "scope_reason": "In city",
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "exclusion_reason": None,
                    "confidence": 0.98,
                    "reason": "OK",
                    "brief_payload": {
                        "topic": "Cached Topic",
                        "headline": "Cached Headline",
                        "digest_summary": "Cached Summary",
                        "evidence_items": [
                            {
                                "text": "Active",
                                "kind": "service_access",
                                "publication_use": "PUBLISH",
                                "source_fragment_ids": [9050],
                                "service_state": {
                                    "subject_key": "cached_service",
                                    "subject_label": "Service",
                                    "dimension": "availability",
                                    "location": "City",
                                    "entity": "serv",
                                    "state": "AVAILABLE",
                                    "expected_now": True,
                                    "basis": "normal_operation",
                                },
                            }
                        ],
                    },
                }
            ]
        }
    )

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    res1 = await service.triage_stories_batch(
        conn, [st], edition_id=edition.id, scope_config=scope_config, scope_hash=scope_hash
    )
    assert len(res1.results) == 1
    assert mock_ai.generate_text.call_count == 1

    # 2. Second run: cached! AI should NOT be called at all
    mock_ai_second = AsyncMock()
    service_second = StoryTriageService(ai_cascade=mock_ai_second, cluster_repo=cluster_repo)
    res2 = await service_second.triage_stories_batch(
        conn, [st], edition_id=edition.id, scope_config=scope_config, scope_hash=scope_hash
    )
    assert len(res2.results) == 1
    assert res2.results[0].story_id == sid
    assert res2.results[0].brief_payload is not None
    assert res2.results[0].brief_payload.headline == "Cached Headline"
    assert mock_ai_second.generate_text.call_count == 0


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
    for _ in range(5):
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
        (8024, 'h4', '[0, 0]'::vector, 'm', 2),
        (8025, 'h5', '[0.5, 0.5]'::vector, 'm', 2)
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
        (9024, %s, 3, 'Text 4', 'h4', 'v1', TRUE, NULL, %s),
        (9025, %s, 4, 'Text 5', 'h5', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now, revision.id, now, revision.id, now, revision.id, now, revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (10021, 9021, 8021),
        (10022, 9022, 8022),
        (10023, 9023, 8023),
        (10024, 9024, 8024),
        (10025, 9025, 8025)
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

    # sids[0]: valid KEEP+BRIEF
    # sids[1]: missing from response -> deferred
    # sids[2]: invalid scope ("REGIONAL") -> deferred
    # sids[3]: invalid scope_confidence (1.5) -> deferred
    # sids[4]: invalid fragment ref in brief_payload (ref 99999) -> deferred
    mock_ai = AsyncMock()
    triage_response = {
        "results": [
            {
                "story_id": sids[0],
                "scope": "LOCAL",
                "scope_basis_fragment_ids": [9021],
                "scope_confidence": 0.95,
                "scope_reason": "In city",
                "retention": "KEEP",
                "enrichment": "BRIEF",
                "exclusion_reason": None,
                "confidence": 0.9,
                "reason": "OK",
                "brief_payload": {
                    "topic": "Valid brief",
                    "headline": "Valid",
                    "digest_summary": "Valid summary",
                    "evidence_items": [
                        {
                            "text": "d",
                            "kind": "service_access",
                            "publication_use": "PUBLISH",
                            "source_fragment_ids": [9021],
                            "service_state": {
                                "subject_key": "serv",
                                "subject_label": "Serv",
                                "dimension": "av",
                                "location": "City",
                                "entity": "e",
                                "state": "AVAILABLE",
                                "expected_now": True,
                                "basis": "normal_operation",
                            },
                        }
                    ],
                },
            },
            {
                "story_id": sids[2],
                "scope": "REGIONAL",
                "scope_basis_fragment_ids": [],
                "scope_confidence": 0.95,
                "scope_reason": "In region",
                "retention": "KEEP",
                "enrichment": "BRIEF",
                "exclusion_reason": None,
                "confidence": 0.9,
                "reason": "OK",
            },
            {
                "story_id": sids[3],
                "scope": "LOCAL",
                "scope_basis_fragment_ids": [9024],
                "scope_confidence": 1.5,
                "scope_reason": "In city",
                "retention": "KEEP",
                "enrichment": "BRIEF",
                "exclusion_reason": None,
                "confidence": 0.9,
                "reason": "OK",
            },
            {
                "story_id": sids[4],
                "scope": "LOCAL",
                "scope_basis_fragment_ids": [9025],
                "scope_confidence": 0.95,
                "scope_reason": "In city",
                "retention": "KEEP",
                "enrichment": "BRIEF",
                "exclusion_reason": None,
                "confidence": 0.9,
                "reason": "OK",
                "brief_payload": {
                    "topic": "Invalid fragment ref",
                    "headline": "Invalid",
                    "digest_summary": "Invalid",
                    "evidence_items": [
                        {
                            "text": "d",
                            "kind": "service_access",
                            "publication_use": "PUBLISH",
                            "source_fragment_ids": [99999],  # invalid!
                        }
                    ],
                },
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
    assert set(result.deferred_story_ids) == {sids[1], sids[2], sids[3], sids[4]}


@pytest.mark.postgres
async def test_story_triage_service_drop_normalization_rules(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    sid_valid_drop = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    sid_low_conf_with_brief = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    sid_low_conf_without_brief = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (8031, 'h31', '[1, 0]'::vector, 'm', 2),
        (8032, 'h32', '[0, 1]'::vector, 'm', 2),
        (8033, 'h33', '[1, 1]'::vector, 'm', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (9031, %s, 0, 'Commercial haircut sale', 'h31', 'v1', TRUE, NULL, %s),
        (9032, %s, 1, 'Question about ATM cash', 'h32', 'v1', TRUE, NULL, %s),
        (9033, %s, 2, 'Unclear drop without brief', 'h33', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now, revision.id, now, revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (10031, 9031, 8031),
        (10032, 9032, 8032),
        (10033, 9033, 8033)
        """
    )

    aid1 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid_valid_drop,
        fragment_id=9031,
        fragment_embedding_id=10031,
        assignment_kind="new_story",
    )
    aid2 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid_low_conf_with_brief,
        fragment_id=9032,
        fragment_embedding_id=10032,
        assignment_kind="new_story",
    )
    aid3 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid_low_conf_without_brief,
        fragment_id=9033,
        fragment_embedding_id=10033,
        assignment_kind="new_story",
    )

    for sid, aid in [
        (sid_valid_drop, aid1),
        (sid_low_conf_with_brief, aid2),
        (sid_low_conf_without_brief, aid3),
    ]:
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

    s1 = await cluster_repo.get_cluster_state(conn, sid_valid_drop)
    s2 = await cluster_repo.get_cluster_state(conn, sid_low_conf_with_brief)
    s3 = await cluster_repo.get_cluster_state(conn, sid_low_conf_without_brief)

    scope_config = EditionScopeConfig(name="City", focus_places=("City",))
    scope_hash = scope_config_hash(scope_config)

    mock_ai = AsyncMock()
    triage_response = {
        "results": [
            {
                "story_id": sid_valid_drop,
                "scope": "LOCAL",
                "scope_basis_fragment_ids": [9031],
                "scope_confidence": 0.99,
                "scope_reason": "In city",
                "retention": "DROP",
                "enrichment": "NONE",
                "exclusion_reason": "commercial_classified",
                "confidence": 0.99,
                "reason": "Clear ad",
            },
            {
                # Low confidence drop but has valid brief -> normalized to KEEP + BRIEF
                "story_id": sid_low_conf_with_brief,
                "scope": "LOCAL",
                "scope_basis_fragment_ids": [9032],
                "scope_confidence": 0.99,
                "scope_reason": "In city",
                "retention": "DROP",
                "enrichment": "NONE",
                "exclusion_reason": "commercial_classified",
                "confidence": 0.70,  # lower than min_ignore_confidence (0.95)
                "reason": "Unsure if ad",
                "brief_payload": {
                    "topic": "ATM cash info",
                    "headline": "ATM is dispensing cash",
                    "digest_summary": "Residents report cash available at ATM",
                    "evidence_items": [
                        {
                            "text": "ATM working",
                            "kind": "service_access",
                            "publication_use": "PUBLISH",
                            "source_fragment_ids": [9032],
                            "service_state": {
                                "subject_key": "banking_cash",
                                "subject_label": "Банкоматы",
                                "dimension": "availability",
                                "location": "AKZ",
                                "entity": "ATM",
                                "state": "AVAILABLE",
                                "expected_now": True,
                                "basis": "normal_operation",
                            },
                        }
                    ],
                },
            },
            {
                # Low confidence drop without brief -> deferred
                "story_id": sid_low_conf_without_brief,
                "scope": "LOCAL",
                "scope_basis_fragment_ids": [9033],
                "scope_confidence": 0.99,
                "scope_reason": "In city",
                "retention": "DROP",
                "enrichment": "NONE",
                "exclusion_reason": "commercial_classified",
                "confidence": 0.70,  # low confidence
                "reason": "Unsure",
                "brief_payload": None,
            },
        ]
    }
    mock_ai.generate_text.return_value = json.dumps(triage_response)

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    result = await service.triage_stories_batch(
        conn,
        [s1, s2, s3],
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
        min_ignore_confidence=0.95,
    )

    assert len(result.results) == 2
    res_map = {r.story_id: r for r in result.results}
    assert res_map[sid_valid_drop].retention == "DROP"
    assert res_map[sid_valid_drop].enrichment == "NONE"

    # The low-confidence drop was normalized to KEEP+BRIEF
    assert res_map[sid_low_conf_with_brief].retention == "KEEP"
    assert res_map[sid_low_conf_with_brief].enrichment == "BRIEF"
    assert res_map[sid_low_conf_with_brief].brief_payload is not None

    # The low-confidence drop without brief was deferred
    assert result.deferred_story_ids == (sid_low_conf_without_brief,)


@pytest.mark.postgres
async def test_story_triage_utility_vs_commercial_prompt_contract(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    # 5 test cases: ATM, ONET, passport, haircut, generator
    cases_data = [
        (
            "atm",
            "ATM cash dispensing in Center",
            "service_access",
            "KEEP",
            "BRIEF",
            "PUBLISH",
            None,
        ),
        ("onet", "ONET backup power running", "service_access", "KEEP", "BRIEF", "PUBLISH", None),
        (
            "passport",
            "Passport fee terminal in MFC",
            "service_access",
            "KEEP",
            "BRIEF",
            "PUBLISH",
            None,
        ),
        (
            "haircut",
            "Haircut 20% discount sale",
            "commercial_offer",
            "DROP",
            "NONE",
            "EXCLUDE",
            "commercial_classified",
        ),
        (
            "generator",
            "Generator for sale 28000 rub",
            "commercial_offer",
            "DROP",
            "NONE",
            "EXCLUDE",
            "commercial_classified",
        ),
    ]

    states = []
    triage_results = []
    for idx, (name, text, kind, exp_ret, exp_enr, pub_use, ex_reason) in enumerate(
        cases_data, start=1
    ):
        sid = await story_repo.create_story_shell(
            conn, edition_id=edition.id, knowledge_source="event_first"
        )
        fid = 9100 + idx
        vid = 8100 + idx
        await conn.execute(
            """
            INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
            OVERRIDING SYSTEM VALUE VALUES (%s, %s, '[1, 0]'::vector, 'm', 2)
            """,
            (vid, f"hash_{name}"),
        )
        await conn.execute(
            """
            INSERT INTO source_fragments (
                id, source_item_revision_id, ordinal, text_content, normalized_hash,
                fragmenter_version, is_candidate, drop_reason, created_at
            ) OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, 'v1', TRUE, NULL, %s)
            """,
            (fid, revision.id, idx, text, f"hash_{name}", now),
        )
        await conn.execute(
            """
            INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
            OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s)
            """,
            (10100 + idx, fid, vid),
        )
        aid = await cluster_repo.assign_fragment_to_story(
            conn,
            story_id=sid,
            fragment_id=fid,
            fragment_embedding_id=10100 + idx,
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

        brief = None
        if exp_ret == "KEEP":
            item_data: dict[str, Any] = {
                "text": text,
                "kind": kind,
                "publication_use": pub_use,
                "source_fragment_ids": [fid],
            }
            if kind == "service_access":
                item_data["service_state"] = {
                    "subject_key": f"subj_{name}",
                    "subject_label": f"Label {name}",
                    "dimension": "status",
                    "location": "Center",
                    "entity": "service",
                    "state": "AVAILABLE",
                    "expected_now": True,
                    "basis": "normal_operation",
                }
            brief = {
                "topic": f"Topic for {name}",
                "headline": f"Headline for {name}",
                "digest_summary": f"Summary for {name}",
                "evidence_items": [item_data],
            }

        triage_results.append(
            {
                "story_id": sid,
                "scope": "LOCAL",
                "scope_basis_fragment_ids": [fid],
                "scope_confidence": 0.99,
                "scope_reason": "In target city",
                "retention": exp_ret,
                "enrichment": exp_enr,
                "exclusion_reason": ex_reason,
                "confidence": 0.98,
                "reason": f"Evaluated {name}",
                "brief_payload": brief,
            }
        )

    mock_ai = AsyncMock()
    mock_ai.generate_text.return_value = json.dumps({"results": triage_results})

    scope_config = EditionScopeConfig(name="City", focus_places=("City",))
    scope_hash = scope_config_hash(scope_config)

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    batch_result = await service.triage_stories_batch(
        conn,
        states,
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
    )

    assert len(batch_result.results) == 5
    assert batch_result.deferred_story_ids == ()

    by_id = {r.story_id: r for r in batch_result.results}
    for idx, (_name, _text, _kind, exp_ret, exp_enr, _pub_use, _ex_reason) in enumerate(cases_data):
        sid = states[idx].story_id
        r = by_id[sid]
        assert r.retention == exp_ret
        assert r.enrichment == exp_enr
        if exp_ret == "KEEP":
            assert r.brief_payload is not None
            assert len(r.brief_payload.evidence_items) == 1
            assert r.brief_payload.evidence_items[0].publication_use == "PUBLISH"


@pytest.mark.postgres
async def test_story_triage_80_singletons_one_call_no_rich_analysis(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    states = []
    triage_results = []
    for idx in range(1, 81):
        sid = await story_repo.create_story_shell(
            conn, edition_id=edition.id, knowledge_source="event_first"
        )
        fid = 19000 + idx
        vid = 18000 + idx
        await conn.execute(
            """
            INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
            OVERRIDING SYSTEM VALUE VALUES (%s, %s, '[1, 0]'::vector, 'm', 2)
            """,
            (vid, f"hash_80_{idx}"),
        )
        await conn.execute(
            """
            INSERT INTO source_fragments (
                id, source_item_revision_id, ordinal, text_content, normalized_hash,
                fragmenter_version, is_candidate, drop_reason, created_at
            ) OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, 'v1', TRUE, NULL, %s)
            """,
            (fid, revision.id, idx, f"Utility singleton event #{idx}", f"hash_80_{idx}", now),
        )
        await conn.execute(
            """
            INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
            OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s)
            """,
            (20000 + idx, fid, vid),
        )
        aid = await cluster_repo.assign_fragment_to_story(
            conn,
            story_id=sid,
            fragment_id=fid,
            fragment_embedding_id=20000 + idx,
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

        triage_results.append(
            {
                "story_id": sid,
                "scope": "LOCAL",
                "scope_basis_fragment_ids": [fid],
                "scope_confidence": 0.99,
                "scope_reason": "In target city",
                "retention": "KEEP",
                "enrichment": "BRIEF",
                "exclusion_reason": None,
                "confidence": 0.98,
                "reason": f"Utility #{idx}",
                "brief_payload": {
                    "topic": f"Utility #{idx}",
                    "headline": f"Utility headline #{idx}",
                    "digest_summary": f"Utility summary #{idx}",
                    "evidence_items": [
                        {
                            "text": f"Utility singleton event #{idx}",
                            "kind": "service_access",
                            "publication_use": "PUBLISH",
                            "source_fragment_ids": [fid],
                            "service_state": {
                                "subject_key": f"util_{idx}",
                                "subject_label": f"Utility {idx}",
                                "dimension": "status",
                                "location": "City",
                                "entity": "grid",
                                "state": "AVAILABLE",
                                "expected_now": True,
                                "basis": "normal_operation",
                            },
                        }
                    ],
                },
            }
        )

    mock_ai = AsyncMock()
    mock_ai.generate_text.return_value = json.dumps({"results": triage_results})

    scope_config = EditionScopeConfig(name="City", focus_places=("City",))
    scope_hash = scope_config_hash(scope_config)

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    result = await service.triage_stories_batch(
        conn,
        states,
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
    )

    assert len(result.results) == 80
    assert result.deferred_story_ids == ()
    assert mock_ai.generate_text.call_count == 1
    assert all(r.retention == "KEEP" for r in result.results)
    assert all(r.enrichment == "BRIEF" for r in result.results)


@pytest.mark.postgres
async def test_single_community_source_keep_internal_only_normalized_to_brief(
    conn, edition, revision
):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    # Create source with role 'community'
    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-1009999', 'https://t.me/community', 'Community Chat', 'community')
        RETURNING id
        """
    )
    community_source_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', '9999', now())
        RETURNING id
        """,
        (community_source_id,),
    )
    community_item_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'hash-comm-1', 'На Горе света нет')
        RETURNING id
        """,
        (community_item_id,),
    )
    comm_rev_id = (await cursor.fetchone())[0]

    sid = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES (8999, 'h_comm', '[1, 0]'::vector, 'm', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES (9999, %s, 0, 'На Горе света нет', 'h_comm', 'v1', TRUE, NULL, %s)
        """,
        (comm_rev_id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES (10999, 9999, 8999)
        """
    )
    aid = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid,
        fragment_id=9999,
        fragment_embedding_id=10999,
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

    scope_config = EditionScopeConfig(name="Бердянск", focus_places=("Бердянск",))
    scope_hash = scope_config_hash(scope_config)

    mock_ai = AsyncMock()
    mock_ai.generate_text.return_value = json.dumps(
        {
            "results": [
                {
                    "story_id": sid,
                    "scope": "LOCAL",
                    "scope_basis_fragment_ids": [9999],
                    "scope_confidence": 0.93,
                    "scope_reason": "In target city",
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "exclusion_reason": None,
                    "confidence": 0.74,
                    "reason": "Single community source outage",
                    "brief_payload": {
                        "topic": "Отключение света на Горе",
                        "publishability": "internal_only",
                        "headline": "Жители Горы сообщают об отсутствии света",
                        "digest_summary": "По сообщению жителя, на Горе нет электричества.",
                        "evidence_items": [
                            {
                                "text": "На Горе света нет",
                                "kind": "community_report",
                                "publication_use": "PUBLISH",
                                "source_fragment_ids": [9999],
                            }
                        ],
                    },
                }
            ]
        }
    )

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    batch = await service.triage_stories_batch(
        conn,
        [st],
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
    )

    assert len(batch.results) == 1
    result = batch.results[0]
    assert result.retention == "KEEP"
    assert result.enrichment == "BRIEF"
    assert result.brief_payload is not None
    assert result.brief_payload.publishability == "brief"
    assert result.brief_payload.evidence_items[0].kind == "community_report"
    assert result.brief_payload.evidence_items[0].publication_use == "PUBLISH"


@pytest.mark.postgres
async def test_gate_v2_normalizes_resident_question_brief_payload(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    sid = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES (8888, 'h_q', '[1, 0]'::vector, 'm', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES (9888, %s, 0, 'Работает ли пенсионный фонд?', 'h_q', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES (10888, 9888, 8888)
        """
    )
    aid = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid,
        fragment_id=9888,
        fragment_embedding_id=10888,
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

    scope_config = EditionScopeConfig(name="Бердянск", focus_places=("Бердянск",))
    scope_hash = scope_config_hash(scope_config)

    mock_ai = AsyncMock()
    mock_ai.generate_text.return_value = json.dumps(
        {
            "results": [
                {
                    "story_id": sid,
                    "scope": "LOCAL",
                    "scope_basis_fragment_ids": [9888],
                    "scope_confidence": 0.95,
                    "scope_reason": "Resident question in target city",
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "exclusion_reason": None,
                    "confidence": 0.85,
                    "reason": "Resident asking about pension fund",
                    "brief_payload": {
                        "topic": "Работа пенсионного фонда",
                        "publishability": "news",
                        "headline": "Жители уточняют работу пенсионного фонда",
                        "digest_summary": "В чатах спрашивают график работы учреждения.",
                        "evidence_items": [
                            {
                                "text": "Работает ли пенсионный фонд?",
                                "kind": "resident_question",
                                "publication_use": "PUBLISH",
                                "source_fragment_ids": [9888],
                            }
                        ],
                    },
                }
            ]
        }
    )

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    batch = await service.triage_stories_batch(
        conn,
        [st],
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
    )

    assert len(batch.results) == 1
    result = batch.results[0]
    assert result.retention == "KEEP"
    assert result.enrichment == "BRIEF"
    assert result.brief_payload is not None
    assert result.brief_payload.evidence_items[0].kind == "resident_question"
    assert result.brief_payload.evidence_items[0].publication_use == "CONTEXT"
    assert result.brief_payload.evidence_items[0].service_state is None


def test_gate_v7_uses_unified_service_state_contract() -> None:
    from src.processing.event_triage import _GATE_V2_SYSTEM_PROMPT, TRIAGE_VERSION

    assert TRIAGE_VERSION == "v7"
    prompt = _GATE_V2_SYSTEM_PROMPT

    assert '"service_state"' in prompt
    assert '"expected_now"' in prompt
    assert '"basis"' in prompt
    assert '"scope_basis_fragment_ids"' in prompt
    assert '"operational_observations"' not in prompt


@pytest.mark.postgres
async def test_gate_non_operational_coping_evidence_preserves_keep_without_observation(
    conn, edition, revision
):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    sid = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (8901, 'hash_coping_1', '[1, 0]'::vector, 'test-model', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (9901, %s, 0, 'Жильцы скинулись по 300 рублей на домовой генератор', 'hash_coping_1', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (10901, 9901, 8901)
        """
    )
    aid1 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid,
        fragment_id=9901,
        fragment_embedding_id=10901,
        assignment_kind="new_story",
    )
    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=sid,
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
    st = await cluster_repo.get_cluster_state(conn, sid)
    assert st is not None

    scope_config = EditionScopeConfig(name="Бердянск", focus_places=("Бердянск",))
    scope_hash = scope_config_hash(scope_config)

    mock_ai = AsyncMock()
    mock_ai.generate_text.return_value = json.dumps(
        {
            "results": [
                {
                    "story_id": sid,
                    "scope": "LOCAL",
                    "scope_basis_fragment_ids": [9901],
                    "scope_confidence": 0.95,
                    "scope_reason": "Resident workaround in target city",
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "exclusion_reason": None,
                    "confidence": 0.9,
                    "reason": "Residents sharing generator costs",
                    "brief_payload": {
                        "topic": "Использование генераторов жителями",
                        "publishability": "news",
                        "headline": "Жильцы дома установили общий генератор",
                        "digest_summary": "Жители собрали средства на работу насоса.",
                        "evidence_items": [
                            {
                                "text": "Жильцы скинулись по 300 рублей на домовой генератор",
                                "kind": "community_report",
                                "publication_use": "PUBLISH",
                                "source_fragment_ids": [9901],
                            }
                        ],
                    },
                }
            ]
        }
    )

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    batch = await service.triage_stories_batch(
        conn,
        [st],
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
    )

    assert len(batch.results) == 1
    res = batch.results[0]
    assert res.retention == "KEEP"
    assert res.enrichment == "BRIEF"
    assert res.brief_payload is not None
    assert len(res.brief_payload.evidence_items) == 1
    assert res.brief_payload.evidence_items[0].publication_use == "PUBLISH"


@pytest.mark.postgres
async def test_gate_v7_water_auto_projection_readiness(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    sid = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (8991, 'hash_water_gate', '[1, 0]'::vector, 'test-model', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (9991, %s, 0, 'Water is absent on upper floors in Berdyansk', 'hash_water_gate', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (10991, 9991, 8991)
        """
    )
    aid = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid,
        fragment_id=9991,
        fragment_embedding_id=10991,
        assignment_kind="new_story",
    )
    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=sid,
        centroid=[1.0, 0.0],
        model="test-model",
        dimensions=2,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid,
    )
    st = await cluster_repo.get_cluster_state(conn, sid)
    assert st is not None

    scope_config = EditionScopeConfig(name="Бердянск", focus_places=("Бердянск",))
    scope_hash = scope_config_hash(scope_config)

    mock_ai = AsyncMock()
    mock_ai.generate_text.return_value = json.dumps(
        {
            "results": [
                {
                    "story_id": sid,
                    "scope": "LOCAL",
                    "scope_basis_fragment_ids": [9991],
                    "scope_confidence": 0.98,
                    "scope_reason": "Water outage in target city",
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "exclusion_reason": None,
                    "confidence": 0.95,
                    "reason": "Water failure reported",
                    "brief_payload": {
                        "topic": "Отсутствие воды на верхних этажах",
                        "publishability": "news",
                        "headline": "В Бердянске на верхних этажах пропала вода",
                        "digest_summary": "Жители сообщают об отсутствии воды.",
                        "evidence_items": [
                            {
                                "kind": "service_access",
                                "publication_use": "PUBLISH",
                                "text": "Water is absent on upper floors in Berdyansk",
                                "source_fragment_ids": [9991],
                                "service_state": {
                                    "subject_key": "water_supply",
                                    "subject_label": "Water supply",
                                    "dimension": "availability",
                                    "state": "UNAVAILABLE",
                                    "location": "upper floors",
                                    "entity": "",
                                    "expected_now": True,
                                    "basis": "direct_failure",
                                },
                            }
                        ],
                    },
                }
            ]
        }
    )

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    batch = await service.triage_stories_batch(
        conn,
        [st],
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
    )

    assert len(batch.results) == 1
    res = batch.results[0]
    assert res.retention == "KEEP"
    assert res.brief_payload is not None
    item = res.brief_payload.evidence_items[0]
    assert item.service_state is not None
    assert item.service_state.subject_key == "water_supply"


@pytest.mark.postgres
async def test_gate_v7_coping_false_positive_demoted_to_community_report(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    sid = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (8992, 'hash_coping_gate', '[1, 0]'::vector, 'test-model', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (9992, %s, 0, 'Жители включают генератор на ночь для холодильников', 'hash_coping_gate', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (10992, 9992, 8992)
        """
    )
    aid = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid,
        fragment_id=9992,
        fragment_embedding_id=10992,
        assignment_kind="new_story",
    )
    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=sid,
        centroid=[1.0, 0.0],
        model="test-model",
        dimensions=2,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid,
    )

    st = await cluster_repo.get_cluster_state(conn, sid)
    assert st is not None

    scope_config = EditionScopeConfig(name="Бердянск", focus_places=("Бердянск",))
    scope_hash = scope_config_hash(scope_config)

    mock_ai = AsyncMock()
    mock_ai.generate_text.return_value = json.dumps(
        {
            "results": [
                {
                    "story_id": sid,
                    "scope": "LOCAL",
                    "scope_basis_fragment_ids": [9992],
                    "scope_confidence": 0.95,
                    "scope_reason": "Resident generator use in focus city",
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "exclusion_reason": None,
                    "confidence": 0.9,
                    "reason": "Residents running generator",
                    "brief_payload": {
                        "topic": "Использование генераторов жителями",
                        "publishability": "news",
                        "headline": "Жильцы дома используют генератор ночью",
                        "digest_summary": "Жители включают генератор для холодильников.",
                        "evidence_items": [
                            {
                                "text": "Жители включают генератор на ночь для холодильников",
                                "kind": "service_access",
                                "publication_use": "PUBLISH",
                                "source_fragment_ids": [9992],
                                "service_state": {
                                    "subject_key": "backup_power",
                                    "subject_label": "Генератор",
                                    "dimension": "availability",
                                    "location": "Бердянск",
                                    "entity": "жители",
                                    "state": "AVAILABLE",
                                    "expected_now": True,
                                    "basis": "normal_operation",
                                },
                            }
                        ],
                    },
                }
            ]
        }
    )

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    batch = await service.triage_stories_batch(
        conn,
        [st],
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
    )

    assert len(batch.results) == 1
    res = batch.results[0]
    assert res.retention == "KEEP"
    assert res.enrichment == "BRIEF"
    assert res.brief_payload is not None
    assert len(res.brief_payload.evidence_items) == 1
    assert res.brief_payload.evidence_items[0].kind == "community_report"
    assert res.brief_payload.evidence_items[0].service_state is None


@pytest.mark.postgres
async def test_gate_broad_regional_guard_drops_summary_without_local_consequence(
    conn, edition, revision
):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    # Story 1: broad regional summary without focus consequence
    sid1 = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    # Story 2: regional story with explicit focus consequence
    sid2 = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (8993, 'h_reg1', '[1, 0]'::vector, 'm', 2),
        (8994, 'h_reg2', '[0, 1]'::vector, 'm', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (9993, %s, 0, 'По всей Запорожской области зафиксировано 150 обстрелов', 'h_reg1', 'v1', TRUE, NULL, %s),
        (9994, %s, 1, 'По Запорожской области введены графики; в Бердянске отключен свет', 'h_reg2', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now, revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (10993, 9993, 8993),
        (10994, 9994, 8994)
        """
    )
    aid1 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid1,
        fragment_id=9993,
        fragment_embedding_id=10993,
        assignment_kind="new_story",
    )
    aid2 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid2,
        fragment_id=9994,
        fragment_embedding_id=10994,
        assignment_kind="new_story",
    )
    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=sid1,
        centroid=[1.0, 0.0],
        model="m",
        dimensions=2,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid1,
    )
    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=sid2,
        centroid=[0.0, 1.0],
        model="m",
        dimensions=2,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid2,
    )

    s1 = await cluster_repo.get_cluster_state(conn, sid1)
    s2 = await cluster_repo.get_cluster_state(conn, sid2)
    assert s1 is not None and s2 is not None

    scope_config = EditionScopeConfig(
        name="Бердянск",
        focus_places=("Бердянск", "Азовское"),
        direct_impact_only=True,
    )
    scope_hash = scope_config_hash(scope_config)

    mock_ai = AsyncMock()
    mock_ai.generate_text.return_value = json.dumps(
        {
            "results": [
                {
                    "story_id": sid1,
                    "scope": "LOCAL",
                    "scope_basis_fragment_ids": [9993],
                    "scope_confidence": 0.9,
                    "scope_reason": "Same region summary",
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "exclusion_reason": None,
                    "confidence": 0.9,
                    "reason": "Regional incidents",
                    "brief_payload": {
                        "topic": "Обстрелы по области",
                        "publishability": "news",
                        "headline": "Сводка по Запорожской области",
                        "digest_summary": "150 обстрелов в регионе.",
                        "evidence_items": [
                            {
                                "text": "150 обстрелов по области",
                                "kind": "established_fact",
                                "publication_use": "PUBLISH",
                                "source_fragment_ids": [9993],
                            }
                        ],
                    },
                },
                {
                    "story_id": sid2,
                    "scope": "DIRECT_IMPACT",
                    "scope_basis_fragment_ids": [9994],
                    "scope_confidence": 0.95,
                    "scope_reason": "Regional restriction explicitly hit Berdyansk",
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "exclusion_reason": None,
                    "confidence": 0.95,
                    "reason": "Direct blackout consequence",
                    "brief_payload": {
                        "topic": "Отключение света",
                        "publishability": "news",
                        "headline": "В Бердянске отключили свет",
                        "digest_summary": "Свет пропал из-за областных графиков.",
                        "evidence_items": [
                            {
                                "text": "В Бердянске отключен свет",
                                "kind": "service_access",
                                "publication_use": "PUBLISH",
                                "source_fragment_ids": [9994],
                                "service_state": {
                                    "subject_key": "power_supply",
                                    "subject_label": "Электроснабжение",
                                    "dimension": "availability",
                                    "state": "UNAVAILABLE",
                                    "expected_now": True,
                                    "basis": "direct_failure",
                                },
                            }
                        ],
                    },
                },
            ]
        }
    )

    service = StoryTriageService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    batch = await service.triage_stories_batch(
        conn,
        [s1, s2],
        edition_id=edition.id,
        scope_config=scope_config,
        scope_hash=scope_hash,
    )

    assert len(batch.results) == 2
    r1 = next(r for r in batch.results if r.story_id == sid1)
    r2 = next(r for r in batch.results if r.story_id == sid2)

    # sid1 was normalized to OUT_OF_SCOPE / DROP by deterministic guard
    assert r1.scope == "OUT_OF_SCOPE"
    assert r1.retention == "DROP"
    assert r1.enrichment == "NONE"
    assert r1.brief_payload is None

    # sid2 preserved DIRECT_IMPACT / KEEP because it explicitly mentioned Berdyansk
    assert r2.scope == "DIRECT_IMPACT"
    assert r2.retention == "KEEP"
    assert r2.brief_payload is not None
