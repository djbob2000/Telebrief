"""Tests for EventAnalysisService and rich event analysis."""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock

import pytest

from src.processing.event_analysis import EventAnalysisPayload, EventAnalysisService
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.stories import StoryRepository


@pytest.mark.unit
def test_event_analysis_payload_serialization():
    payload = EventAnalysisPayload(
        analysis_version="v1",
        topic="Water outage on AKZ",
        category="utilities",
        urgency="high",
        publishability="news",
        headline="Авария на водоводе в Бердянске",
        digest_summary="Водоканал устраняет порыв на АКЗ.",
        key_facts=["Порыв трубы", "Работы до 17:00"],
        official_positions=[{"source": "Vodokanal", "statement": "Ремонт идет"}],
        community_observations=["Воды нет с 8 утра"],
        conflicts_or_uncertainties=[],
        affected_areas=["АКЗ", "РТС"],
        timeline_summary="Утром порыв, днем ремонт",
        confidence_score=0.95,
        representative_fragment_ids=[101, 102],
    )

    data = payload.to_dict()
    restored = EventAnalysisPayload.from_dict(data)
    assert restored.topic == payload.topic
    assert restored.headline == payload.headline
    assert restored.representative_fragment_ids == [101, 102]


@pytest.mark.unit
def test_event_payload_accepts_open_tags():
    payload = EventAnalysisPayload.from_dict(
        {
            "topic": "Массовое появление медуз у городского пляжа",
            "tags": ["море", "медузы", "пляж", "ограничение доступа"],
            "headline": "...",
            "digest_summary": "...",
        }
    )
    assert payload.tags == ["море", "медузы", "пляж", "ограничение доступа"]


@pytest.mark.unit
def test_event_payload_reads_legacy_category_as_tag():
    payload = EventAnalysisPayload.from_dict(
        {
            "category": "utilities",
            "headline": "...",
            "digest_summary": "...",
        }
    )
    assert payload.tags == ["utilities"]


@pytest.mark.postgres
async def test_event_analysis_service_workflow(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    # 1. Create story shell
    story_id = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    # 2. Insert vector & fragment
    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (5001, 'hash_ea', '[1, 0, 0, 0]'::vector, 'test-model', 4)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (6001, %s, 0, 'Outage on AKZ district', 'hash_ea', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (7001, 6001, 5001)
        """
    )

    assignment_id = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=story_id,
        fragment_id=6001,
        fragment_embedding_id=7001,
        assignment_kind="new_story",
    )
    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=story_id,
        centroid=[1.0, 0.0, 0.0, 0.0],
        model="test-model",
        dimensions=4,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=assignment_id,
        analysis_dirty=True,
    )

    # Mock AI Cascade
    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "mock_provider"
    mock_ai.model_name = "mock-model"
    llm_output = {
        "topic": "Авария на водоводе в АКЗ",
        "category": "utilities",
        "urgency": "high",
        "publishability": "news",
        "headline": "В микрорайоне АКЗ устраняют аварию на водопроводе",
        "digest_summary": "Коммунальщики ведут восстановительные работы на Мелитопольском шоссе.",
        "key_facts": ["Порыв на водоводе", "Ограничено водоснабжение"],
        "official_positions": [{"source": "Бердянскводоканал", "statement": "Работы до вечера"}],
        "community_observations": ["На дороге вода"],
        "conflicts_or_uncertainties": [],
        "affected_areas": ["АКЗ", "Мелитопольское шоссе"],
        "timeline_summary": "Утром произошла утечка",
        "confidence_score": 0.96,
    }
    mock_ai.generate_text.return_value = json.dumps(llm_output)

    service = EventAnalysisService(ai_cascade=mock_ai)

    rev = await service.analyze_story(conn, story_id)
    assert rev is not None
    assert rev.story_id == story_id
    assert rev.title == llm_output["headline"]
    assert rev.event_payload["topic"] == llm_output["topic"]
    assert rev.event_payload["affected_areas"] == ["АКЗ", "Мелитопольское шоссе"]

    # Check cluster state is no longer dirty
    state = await cluster_repo.get_cluster_state(conn, story_id)
    assert state is not None
    assert state.analysis_dirty is False
    assert state.last_analyzed_assignment_id == assignment_id

    # Check audit run
    cursor = await conn.execute(
        "SELECT status, story_id, input_fragment_count FROM story_event_analysis_runs"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "succeeded"
    assert row[1] == story_id
    assert row[2] == 1
