"""Tests for EventAnalysisService and rich event analysis."""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock

import pytest

from src.domain.event_payload import (
    EventPayload,
    EvidenceItemPayload,
    ensure_keep_publishability,
    normalize_question_evidence,
)
from src.processing.event_analysis import EventAnalysisPayload, EventAnalysisService
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.stories import StoryRepository


@pytest.mark.unit
def test_resident_question_is_valid_context_evidence() -> None:
    item = EvidenceItemPayload.from_dict(
        {
            "text": "Работает ли пенсионный фонд?",
            "kind": "resident_question",
            "publication_use": "CONTEXT",
            "source_fragment_ids": [101],
        }
    )
    assert item.kind == "resident_question"
    assert item.publication_use == "CONTEXT"


@pytest.mark.unit
def test_resident_question_publish_is_normalized_to_context() -> None:
    item = EvidenceItemPayload.from_dict(
        {
            "text": "Где купить уголь?",
            "kind": "resident_question",
            "publication_use": "PUBLISH",
            "source_fragment_ids": [102],
        }
    )
    assert item.publication_use == "CONTEXT"


@pytest.mark.unit
def test_normalize_question_evidence_strips_question_only_observations():
    payload = EventPayload.from_dict(
        {
            "headline": "Работа пенсионного фонда",
            "digest_summary": "Жители спрашивают, работает ли пенсионный фонд.",
            "evidence_items": [
                {
                    "text": "Работает пенсионный фонд?",
                    "kind": "resident_question",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [11],
                }
            ],
            "operational_observations": [
                {
                    "subject_key": "pension_fund",
                    "subject_label": "Пенсионный фонд",
                    "dimension": "availability",
                    "location": "Бердянск",
                    "entity": "пенсионный фонд",
                    "state": "UNKNOWN",
                    "detail": "Жители спрашивают, работает ли учреждение",
                    "source_fragment_ids": [11],
                }
            ],
        }
    )
    normalized = normalize_question_evidence(payload)
    assert normalized.evidence_items[0].publication_use == "CONTEXT"
    assert normalized.operational_observations == ()


@pytest.mark.unit
def test_normalize_question_evidence_preserves_answered_observations():
    payload = EventPayload.from_dict(
        {
            "headline": "Работа нотариуса",
            "digest_summary": "Жители уточняли график, нотариус работает от генератора.",
            "evidence_items": [
                {
                    "text": "Работает ли нотариус?",
                    "kind": "resident_question",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [11],
                },
                {
                    "text": "Нотариус принимает по записи и работает от генератора.",
                    "kind": "service_access",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [12],
                },
            ],
            "operational_observations": [
                {
                    "subject_key": "notary",
                    "subject_label": "Нотариус",
                    "dimension": "availability",
                    "location": "Бердянск",
                    "entity": "нотариус",
                    "state": "AVAILABLE",
                    "detail": "Работает от генератора",
                    "source_fragment_ids": [12],
                }
            ],
        }
    )
    normalized = normalize_question_evidence(payload)
    assert normalized.evidence_items[0].publication_use == "CONTEXT"
    assert normalized.evidence_items[1].publication_use == "PUBLISH"
    assert len(normalized.operational_observations) == 1
    assert normalized.operational_observations[0].state == "AVAILABLE"


@pytest.mark.unit
def test_ensure_keep_publishability_preserves_evidence_and_normalizes_internal_only():
    payload = EventPayload.from_dict(
        {
            "topic": "Свет на Горе",
            "publishability": "internal_only",
            "headline": "На Горе нет света",
            "digest_summary": "Житель сообщает об отсутствии света.",
            "evidence_items": [
                {
                    "text": "На Горе света нет",
                    "kind": "community_report",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [101],
                }
            ],
        },
        allowed_fragment_ids={101},
    )

    normalized = ensure_keep_publishability(payload)

    assert normalized.publishability == "brief"
    assert normalized.evidence_items == payload.evidence_items
    assert normalized.evidence_items[0].kind == "community_report"
    assert normalized.evidence_items[0].publication_use == "PUBLISH"


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
    assert restored.representative_fragment_ids == (101, 102)


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
    assert payload.tags == ("море", "медузы", "пляж", "ограничение доступа")


@pytest.mark.unit
def test_event_payload_reads_legacy_category_as_tag():
    payload = EventAnalysisPayload.from_dict(
        {
            "category": "utilities",
            "headline": "...",
            "digest_summary": "...",
        }
    )
    assert payload.tags == ("utilities",)


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


@pytest.mark.postgres
async def test_event_analysis_preserves_kept_community_story_publishability(
    conn, edition, revision
):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    story_id = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (5002, 'hash_comm', '[1, 0, 0, 0]'::vector, 'test-model', 4)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (6002, %s, 0, 'На Горе света нет', 'hash_comm', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (7002, 6002, 5002)
        """
    )

    assignment_id = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=story_id,
        fragment_id=6002,
        fragment_embedding_id=7002,
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

    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "mock_provider"
    mock_ai.model_name = "mock-model"
    llm_output = {
        "topic": "Свет на Горе",
        "urgency": "normal",
        "publishability": "internal_only",
        "headline": "Жители Горы сообщают об отсутствии света",
        "digest_summary": "По сообщению жителя, на Горе нет света.",
        "evidence_items": [
            {
                "text": "На Горе света нет",
                "kind": "community_report",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [6002],
            }
        ],
        "community_observations": ["На Горе света нет"],
        "confidence_score": 0.62,
    }
    mock_ai.generate_text.return_value = json.dumps(llm_output)

    service = EventAnalysisService(ai_cascade=mock_ai)

    rev = await service.analyze_story(conn, story_id)
    assert rev is not None
    assert rev.event_payload["publishability"] == "brief"
    assert rev.event_payload["evidence_items"][0]["kind"] == "community_report"
    assert rev.event_payload["evidence_items"][0]["publication_use"] == "PUBLISH"
    assert rev.event_payload["confidence_score"] == 0.62


@pytest.mark.unit
def test_event_analysis_v5_operational_observation_contract_is_service_state_only() -> None:
    from src.processing import event_analysis

    prompt = event_analysis._EVENT_ANALYSIS_SYSTEM_PROMPT.lower()
    assert event_analysis.ANALYSIS_VERSION == "v5"
    assert "service_access" in prompt
    assert "every publish service_access" in prompt
    assert "seasonal" in prompt
    assert "coping" in prompt
    assert "resident-facing" in prompt
    assert "do not create an operational observation" in prompt
    assert "regional" in prompt
    assert "safety advice" in prompt


@pytest.mark.unit
def test_resident_question_vs_community_report_operational_boundary() -> None:
    from src.domain.event_payload import (
        EventPayload,
        normalize_question_evidence,
    )

    # 1. Pure resident question must NOT produce operational observation
    question_payload = EventPayload.from_dict(
        {
            "headline": "Вопрос о свете в Центре",
            "digest_summary": "Жители интересуются, есть ли свет в Центре.",
            "evidence_items": [
                {
                    "text": "Подскажите, есть ли свет в Центре?",
                    "kind": "resident_question",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [1],
                }
            ],
            "operational_observations": [
                {
                    "subject_key": "power_supply",
                    "subject_label": "Электроснабжение",
                    "dimension": "availability",
                    "location": "Центр",
                    "entity": "электросеть",
                    "state": "UNKNOWN",
                    "detail": "Жители интересуются наличием света",
                    "source_fragment_ids": [1],
                }
            ],
        }
    )
    normalized_q = normalize_question_evidence(question_payload)
    assert normalized_q.evidence_items[0].kind == "resident_question"
    assert normalized_q.evidence_items[0].publication_use == "CONTEXT"
    assert len(normalized_q.operational_observations) == 0

    # 2. Community report with observed state is PUBLISH and retains operational observation
    report_payload = EventPayload.from_dict(
        {
            "headline": "Отсутствие света на Горе",
            "digest_summary": "По сообщениям жителей, на Горе нет света.",
            "evidence_items": [
                {
                    "text": "На Горе света нет",
                    "kind": "community_report",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [2],
                }
            ],
            "operational_observations": [
                {
                    "subject_key": "power_supply",
                    "subject_label": "Электроснабжение",
                    "dimension": "availability",
                    "location": "Гора",
                    "entity": "электросеть",
                    "state": "UNAVAILABLE",
                    "detail": "Гора: нет света",
                    "source_fragment_ids": [2],
                }
            ],
        }
    )
    normalized_r = normalize_question_evidence(report_payload)
    assert normalized_r.evidence_items[0].kind == "community_report"
    assert normalized_r.evidence_items[0].publication_use == "PUBLISH"
    assert len(normalized_r.operational_observations) == 1
    assert normalized_r.operational_observations[0].state == "UNAVAILABLE"


@pytest.mark.postgres
async def test_event_analysis_v5_drops_invalid_coping_operational_observation(
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
        (8993, 'hash_coping_analysis', '[1, 0]'::vector, 'test-model', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (9993, %s, 0, 'Жители включают генератор на ночь для холодильников', 'hash_coping_analysis', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (10993, 9993, 8993)
        """
    )
    aid = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid,
        fragment_id=9993,
        fragment_embedding_id=10993,
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
        analysis_dirty=True,
    )

    mock_ai = AsyncMock()
    mock_ai.generate_text.return_value = json.dumps(
        {
            "topic": "Использование генераторов жителями",
            "tags": ["генераторы", "быт"],
            "urgency": "normal",
            "publishability": "news",
            "headline": "Жильцы дома используют генератор ночью",
            "digest_summary": "Жители включают генератор для холодильников.",
            "key_facts": ["Генератор запускают ночью"],
            "evidence_items": [
                {
                    "text": "Жители включают генератор на ночь для холодильников",
                    "kind": "community_report",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [9993],
                }
            ],
            "operational_observations": [
                {
                    "subject_key": "household_generator",
                    "subject_label": "Генератор",
                    "dimension": "availability",
                    "location": "Бердянск",
                    "entity": "жители",
                    "state": "AVAILABLE",
                    "detail": "Генератор доступен",
                    "source_fragment_ids": [9993],
                }
            ],
            "official_positions": [],
            "community_observations": ["Генератор шумит"],
            "conflicts_or_uncertainties": [],
            "affected_areas": ["Бердянск"],
            "timeline_summary": "Жители используют генератор ночью",
            "confidence_score": 0.9,
        }
    )

    service = EventAnalysisService(ai_cascade=mock_ai, cluster_repo=cluster_repo)
    rev = await service.analyze_story(conn, sid)

    assert rev is not None
    assert rev.event_payload is not None
    assert len(rev.event_payload["evidence_items"]) == 1
    assert rev.event_payload["operational_observations"] == []
