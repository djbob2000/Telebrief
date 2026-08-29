import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.jobs.event_processing import coalesce_dirty_stories_task, process_event_revisions_task
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.stories import StoryRepository
from src.runtime import install_runtime


@pytest.mark.postgres
async def test_process_event_revisions_task_end_to_end(conn, pool, uow, sample_config):
    # Configure event pipeline
    sample_config.settings.event_pipeline = sample_config.settings.event_pipeline.__class__(
        mode="event_first",
        fragment_max_chars=500,
        active_window_hours=72,
        join_similarity=0.84,
        max_cluster_candidates=20,
        embedding_batch_size=32,
    )
    sample_config.embedding.dimensions = 4

    # Setup edition, source, item, revision
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk-ev', 'Berdyansk Ev') RETURNING id"
    )
    edition_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name)
        VALUES ('telegram', 'channel', '-1004242', 'https://t.me/example', 'Example')
        RETURNING id
        """
    )
    source_id = (await cursor.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (source_id, edition_id),
    )

    cursor = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', 'msg-1', now())
        RETURNING id
        """,
        (source_id,),
    )
    item_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'hash_rev1', 'На АКЗ порыв водопровода, ведутся работы.')
        RETURNING id
        """,
        (item_id,),
    )
    rev_id = (await cursor.fetchone())[0]

    from types import SimpleNamespace

    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "mock_provider"
    mock_ai.model_name = "mock-model"
    runtime = SimpleNamespace(
        config=sample_config,
        pool=pool,
        uow=uow,
        provider_cascade=mock_ai,
        image_generator=None,
    )
    install_runtime(runtime)

    # Patch create_embedding_provider to return dummy embeddings
    class MockEmbProvider:
        async def embed_many(self, texts, *, purpose, model, dimensions):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    with patch(
        "src.jobs.event_processing.create_embedding_provider", return_value=MockEmbProvider()
    ):
        stats = await process_event_revisions_task.func(revision_ids=[rev_id])

    assert stats["revisions"] == 1
    assert stats["fragments"] >= 1
    assert stats["candidates"] >= 1
    assert stats["assignments"] >= 1

    # Verify story created and cluster state set
    cursor = await conn.execute(
        "SELECT id, knowledge_source FROM stories WHERE edition_id = %s", (edition_id,)
    )
    story_row = await cursor.fetchone()
    assert story_row is not None
    assert story_row[1] == "event_first"

    cluster_repo = EventClusterRepository()
    state = await cluster_repo.get_cluster_state(conn, story_row[0])
    assert state is not None
    assert state.fragment_count >= 1
    assert state.analysis_dirty is True


@pytest.mark.postgres
async def test_coalesce_dirty_stories_task_end_to_end(conn, pool, uow, sample_config):
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    sample_config.settings.event_pipeline = sample_config.settings.event_pipeline.__class__(
        mode="event_first",
        analysis_quiet_seconds=60,
        analysis_min_interval_seconds=300,
        direct_analysis_min_fragments=2,
    )

    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk-coalesce', 'Berdyansk Coalesce') RETURNING id"
    )
    edition_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name)
        VALUES ('telegram', 'channel', '-1004243', 'https://t.me/example2', 'Example 2')
        RETURNING id
        """
    )
    source_id = (await cursor.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (source_id, edition_id),
    )

    cursor = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', 'msg-1', now())
        RETURNING id
        """,
        (source_id,),
    )
    item_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'hash_rev1', 'На АКЗ порыв водопровода')
        RETURNING id
        """,
        (item_id,),
    )
    rev_id = (await cursor.fetchone())[0]

    story_repo = StoryRepository()
    cluster_repo = EventClusterRepository()
    sid = await story_repo.create_story_shell(
        conn, edition_id=edition_id, knowledge_source="event_first"
    )

    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES (9901, 'hash_c1', '[1, 0, 0, 0]'::vector, 'model', 4)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (id, source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, drop_reason, created_at)
        OVERRIDING SYSTEM VALUE VALUES (9902, %s, 0, 'Text 1', 'hash_c1', 'v1', TRUE, NULL, %s)
        """,
        (rev_id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES (9903, 9902, 9901)
        """
    )

    aid = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid,
        fragment_id=9902,
        fragment_embedding_id=9903,
        assignment_kind="new_story",
    )

    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=sid,
        centroid=[1.0, 0.0, 0.0, 0.0],
        model="model",
        dimensions=4,
        fragment_count=2,  # Direct analysis eligible
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid,
        analysis_dirty=True,
    )

    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "mock_provider"
    mock_ai.model_name = "mock-model"
    llm_output = {
        "topic": "Авария на водоводе в АКЗ",
        "category": "utilities",
        "urgency": "high",
        "publishability": "news",
        "headline": "Восстановление водоснабжения на АКЗ",
        "digest_summary": "Водоканал ликвидирует аварию.",
        "key_facts": ["Авария ликвидируется"],
        "official_positions": [],
        "community_observations": [],
        "conflicts_or_uncertainties": [],
        "affected_areas": ["АКЗ"],
        "timeline_summary": "",
        "confidence_score": 0.95,
    }
    mock_ai.generate_text.return_value = json.dumps(llm_output)

    runtime = SimpleNamespace(
        config=sample_config,
        pool=pool,
        uow=uow,
        provider_cascade=mock_ai,
        image_generator=None,
    )
    install_runtime(runtime)

    stats = await coalesce_dirty_stories_task.func(edition_id=edition_id)
    assert stats["scanned"] >= 1
    assert stats["settled"] >= 1
    assert stats["analyzed"] >= 1

    # Check story state is now not dirty
    state = await cluster_repo.get_cluster_state(conn, sid)
    assert state is not None
    assert state.analysis_dirty is False
