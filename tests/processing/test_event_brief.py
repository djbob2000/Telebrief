"""Tests for EventBriefService and cluster processed status."""

from __future__ import annotations

import datetime as dt

import pytest

from src.domain.event_payload import EventPayload, OperationalObservationPayload
from src.processing.event_brief import EventBriefService
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.stories import StoryRepository


@pytest.mark.postgres
async def test_event_brief_service_persists_brief_revision(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    story_id = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES (7001, 'hb1', '[1, 0]'::vector, 'm', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (7011, %s, 0, 'Brief frag text', 'hb1', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES (7021, 7011, 7001)
        """
    )
    aid = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=story_id,
        fragment_id=7011,
        fragment_embedding_id=7021,
        assignment_kind="new_story",
    )

    payload = EventPayload(
        topic="ATM on AKZ",
        tags=("atm", "cash"),
        urgency="normal",
        publishability="brief",
        headline="ATM dispensing cash on AKZ",
        digest_summary="Del Mar ATM is working.",
        enrichment_level="brief",
        operational_observations=(
            OperationalObservationPayload(
                subject_key="banking_cash",
                subject_label="Банкоматы",
                dimension="availability",
                location="AKZ",
                entity="Del Mar ATM",
                state="AVAILABLE",
                detail="ATM active",
                source_fragment_ids=(7011,),
            ),
        ),
    )

    service = EventBriefService(story_repo=story_repo, cluster_repo=cluster_repo)
    rev1 = await service.persist_brief(conn, story_id=story_id, assignment_id=aid, payload=payload)

    assert rev1 is not None
    assert rev1.event_payload["enrichment_level"] == "brief"
    assert rev1.event_payload["publishability"] == "brief"
    assert rev1.title == "ATM dispensing cash on AKZ"
    assert rev1.summary == "Del Mar ATM is working."

    # Idempotent: second call creates no duplicate revision
    rev2 = await service.persist_brief(conn, story_id=story_id, assignment_id=aid, payload=payload)
    assert rev2 is None

    # Check database revisions count
    cursor = await conn.execute(
        "SELECT count(*) FROM story_revisions WHERE story_id = %s", (story_id,)
    )
    assert (await cursor.fetchone())[0] == 1


@pytest.mark.postgres
async def test_event_brief_service_merges_into_rich_revision_without_downgrading(
    conn, edition, revision
):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    story_id = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    from src.domain.event_payload import EvidenceItemPayload
    from src.domain.service_state import ServiceStatePayload

    # 1. Existing rich analysis revision
    rich_payload = EventPayload(
        topic="Water Outage in Koloniya",
        tags=("water", "outage"),
        urgency="high",
        publishability="news",
        headline="Major Water Pipeline Burst",
        digest_summary="Koloniya is without water due to large burst.",
        enrichment_level="analysis",
        key_facts=("Burst at 8am", "Excavators on site"),
        timeline_summary="Morning burst",
        confidence_score=0.95,
        evidence_items=(
            EvidenceItemPayload(
                text="Morning outage in Koloniya",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(7011,),
                service_state=ServiceStatePayload(
                    subject_key="water_supply",
                    subject_label="Водоснабжение",
                    dimension="availability",
                    location="Koloniya",
                    entity="pipe",
                    state="UNAVAILABLE",
                    expected_now=True,
                    basis="direct_failure",
                ),
            ),
        ),
    )

    import hashlib
    import json

    from src.domain.stories import NewStoryRevision

    rich_dict = rich_payload.to_dict()
    content_hash = hashlib.sha256(json.dumps(rich_dict, sort_keys=True).encode("utf-8")).hexdigest()
    await story_repo.create_revision_if_semantic_change(
        conn,
        story_id=story_id,
        semantic_changed=True,
        revision=NewStoryRevision(
            current_state="active",
            semantic_text=rich_payload.digest_summary,
            content_hash=content_hash,
            created_at=now,
            title=rich_payload.headline,
            summary=rich_payload.digest_summary,
            reason="event_analysis_v1",
            event_payload=rich_dict,
        ),
    )

    # 2. Gate V2 brief with new observation (e.g. restoration at 2pm)
    new_brief = EventPayload(
        topic="Water restored",
        tags=("water", "restoration", "repair"),
        headline="Water restored in Koloniya",
        digest_summary="Water has returned at 2pm.",
        enrichment_level="brief",
        evidence_items=(
            EvidenceItemPayload(
                text="Afternoon restoration in Koloniya",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(7012,),
                service_state=ServiceStatePayload(
                    subject_key="water_supply",
                    subject_label="Водоснабжение",
                    dimension="availability",
                    location="Koloniya",
                    entity="pipe",
                    state="AVAILABLE",
                    expected_now=True,
                    basis="normal_operation",
                ),
            ),
        ),
    )

    service = EventBriefService(story_repo=story_repo, cluster_repo=cluster_repo)
    merged_rev = await service.persist_brief(
        conn, story_id=story_id, assignment_id=1, payload=new_brief
    )

    assert merged_rev is not None
    assert merged_rev.event_payload["enrichment_level"] == "analysis"  # preserved!
    assert merged_rev.event_payload["key_facts"] == [
        "Burst at 8am",
        "Excavators on site",
    ]  # preserved!
    assert len(merged_rev.event_payload["evidence_items"]) == 2


@pytest.mark.postgres
async def test_mark_cluster_processed_without_analysis_semantics(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()

    story_id = await story_repo.create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )

    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES (7031, 'hb31', '[1, 0]'::vector, 'm', 2)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (7041, %s, 0, 'Frag 1', 'hb31', 'v1', TRUE, NULL, %s),
        (7042, %s, 1, 'Frag 2', 'hb32', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now, revision.id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES (7051, 7041, 7031), (7052, 7042, 7031)
        """
    )
    aid1 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=story_id,
        fragment_id=7041,
        fragment_embedding_id=7051,
        assignment_kind="new_story",
    )

    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=story_id,
        centroid=[1.0, 0.0],
        model="m",
        dimensions=2,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid1,
        analysis_dirty=True,
    )

    # Call mark_cluster_processed_without_analysis
    await cluster_repo.mark_cluster_processed_without_analysis(
        conn, story_id=story_id, assignment_id=aid1
    )

    st = await cluster_repo.get_cluster_state(conn, story_id)
    assert st is not None
    assert st.analysis_dirty is False
    assert st.last_analyzed_assignment_id is None
    assert st.last_analyzed_at is None

    # Now simulate a new fragment assignment arriving (new aid2)
    aid2 = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=story_id,
        fragment_id=7042,
        fragment_embedding_id=7052,
        assignment_kind="vector_join",
    )
    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=story_id,
        centroid=[1.0, 0.0],
        model="m",
        dimensions=2,
        fragment_count=2,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid2,
        analysis_dirty=True,
    )

    # Calling mark_cluster_processed_without_analysis with old aid1 should NOT clear dirty state of aid2
    await cluster_repo.mark_cluster_processed_without_analysis(
        conn, story_id=story_id, assignment_id=aid1
    )
    st2 = await cluster_repo.get_cluster_state(conn, story_id)
    assert st2 is not None
    assert st2.analysis_dirty is True
