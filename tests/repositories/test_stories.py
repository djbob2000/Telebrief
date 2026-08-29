"""Tests for Story repository and domain models."""

from __future__ import annotations

import datetime as dt

import pytest

from src.domain.stories import NewStoryRevision, Story, StoryRevision
from src.repositories.stories import StoryRepository


@pytest.mark.unit
def test_story_domain_model_from_row_with_knowledge_source():
    now = dt.datetime.now(dt.timezone.utc)
    # Legacy row (5 columns)
    legacy_row = (1, 10, 100, "active", now)
    story_legacy = Story.from_row(legacy_row)
    assert story_legacy.knowledge_source == "legacy_claims"

    # Event-first row (6 columns)
    event_row = (2, 10, 101, "active", now, "event_first")
    story_event = Story.from_row(event_row)
    assert story_event.knowledge_source == "event_first"


@pytest.mark.unit
def test_story_revision_domain_model_from_row_with_event_payload():
    now = dt.datetime.now(dt.timezone.utc)
    # Legacy row (10 columns)
    legacy_row = (100, 1, 1, "Title", "Summary", "active", "semantic", "hash1", "reason1", now)
    rev_legacy = StoryRevision.from_row(legacy_row)
    assert rev_legacy.event_payload == {}

    # Event-first row with payload (11 columns)
    payload = {"topic": "Water outage", "publishability": "news"}
    event_row = (
        101,
        1,
        2,
        "Title 2",
        "Summary 2",
        "active",
        "semantic 2",
        "hash2",
        "reason2",
        now,
        payload,
    )
    rev_event = StoryRevision.from_row(event_row)
    assert rev_event.event_payload == payload


@pytest.mark.postgres
async def test_story_repository_event_first_shell_and_payload(repo_conn):
    repo = StoryRepository()
    now = dt.datetime.now(dt.timezone.utc)

    # 1. Create event-first story shell
    # Ensure test edition exists
    cursor = await repo_conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('test-ef-ed', 'Test EF Edition') RETURNING id"
    )
    edition_id = (await cursor.fetchone())[0]

    story_id = await repo.create_story_shell(
        repo_conn, edition_id=edition_id, knowledge_source="event_first"
    )
    story = await repo.get(repo_conn, story_id)
    assert story is not None
    assert story.knowledge_source == "event_first"
    assert story.current_revision_id is None
    assert story.lifecycle_state == "candidate"

    # 2. Append revision with event_payload
    payload = {
        "analysis_version": "v1",
        "topic": "Power outage in Koloniya",
        "category": "utilities",
        "digest_summary": "Fallen branch caused power outage in Koloniya",
        "publishability": "news",
    }
    new_rev = NewStoryRevision(
        current_state="active",
        semantic_text="Power outage in Koloniya",
        content_hash="abc123hash",
        created_at=now,
        title="Power outage in Koloniya",
        summary="Fallen branch caused power outage in Koloniya",
        reason="event_analysis",
        event_payload=payload,
    )
    created_rev = await repo.create_revision_if_semantic_change(
        repo_conn,
        story_id=story_id,
        semantic_changed=True,
        revision=new_rev,
    )
    assert created_rev is not None
    assert created_rev.event_payload == payload

    fetched_rev = await repo.get_revision(repo_conn, created_rev.id)
    assert fetched_rev is not None
    assert fetched_rev.event_payload == payload

    batch_revs = await repo.get_revisions(repo_conn, [created_rev.id])
    assert len(batch_revs) == 1
    assert batch_revs[0].event_payload == payload
