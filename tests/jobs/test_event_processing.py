import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.config_loader import EditionScopeConfig
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
    sample_config.settings.edition_scopes = {
        "berdyansk-coalesce": EditionScopeConfig(
            name="Бердянск",
            focus_places=("Бердянск", "АКЗ"),
        )
    }

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
        fragment_count=2,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid,
        analysis_dirty=True,
    )

    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "mock_provider"
    mock_ai.model_name = "mock-model"

    triage_output = {
        "results": [
            {
                "story_id": sid,
                "scope": "LOCAL",
                "scope_confidence": 0.98,
                "scope_reason": "In AKZ",
                "decision": "ANALYZE",
                "exclusion_reason": None,
                "confidence": 0.95,
                "reason": "OK",
            }
        ]
    }
    analysis_output = {
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
    mock_ai.generate_text.side_effect = [
        json.dumps(triage_output),
        json.dumps(analysis_output),
    ]

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
    assert stats["gated"] >= 1
    assert stats["scope_local"] >= 1
    assert stats["analyzed"] >= 1

    # Check story state is now not dirty
    state = await cluster_repo.get_cluster_state(conn, sid)
    assert state is not None
    assert state.analysis_dirty is False


@pytest.mark.postgres
async def test_coalesce_dirty_stories_out_of_scope_marks_analyzed(conn, pool, uow, sample_config):
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    sample_config.settings.event_pipeline = sample_config.settings.event_pipeline.__class__(
        mode="event_first",
        analysis_quiet_seconds=60,
        analysis_min_interval_seconds=300,
    )
    sample_config.settings.edition_scopes = {
        "berdyansk-oos": EditionScopeConfig(
            name="Бердянск",
            focus_places=("Бердянск",),
        )
    }

    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk-oos', 'Berdyansk OOS') RETURNING id"
    )
    edition_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name)
        VALUES ('telegram', 'channel', '-1004244', 'https://t.me/example3', 'Example 3')
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
        VALUES (%s, 'message', 'msg-oos', now())
        RETURNING id
        """,
        (source_id,),
    )
    item_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'hash_rev_oos', 'В Киеве открылась новая станция метро')
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
        OVERRIDING SYSTEM VALUE VALUES (9911, 'hash_coos', '[1, 0, 0, 0]'::vector, 'model', 4)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (id, source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, drop_reason, created_at)
        OVERRIDING SYSTEM VALUE VALUES (9912, %s, 0, 'Text OOS', 'hash_coos', 'v1', TRUE, NULL, %s)
        """,
        (rev_id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES (9913, 9912, 9911)
        """
    )

    aid = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid,
        fragment_id=9912,
        fragment_embedding_id=9913,
        assignment_kind="new_story",
    )

    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=sid,
        centroid=[1.0, 0.0, 0.0, 0.0],
        model="model",
        dimensions=4,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid,
        analysis_dirty=True,
    )

    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "mock_provider"
    mock_ai.model_name = "mock-model"

    triage_output = {
        "results": [
            {
                "story_id": sid,
                "scope": "OUT_OF_SCOPE",
                "scope_confidence": 0.99,
                "scope_reason": "Kyiv metro is outside Berdyansk",
                "decision": "ANALYZE",
                "exclusion_reason": None,
                "confidence": 0.95,
                "reason": "OK",
            }
        ]
    }
    mock_ai.generate_text.return_value = json.dumps(triage_output)

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
    assert stats["gated"] >= 1
    assert stats["scope_out_of_scope"] == 1
    assert stats["analyzed"] == 0

    # Cluster state settled without rich analysis
    state = await cluster_repo.get_cluster_state(conn, sid)
    assert state is not None
    assert state.analysis_dirty is False
    assert state.last_analyzed_assignment_id == aid


@pytest.mark.postgres
async def test_coalesce_dirty_stories_uncertain_marks_analyzed(conn, pool, uow, sample_config):
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    sample_config.settings.event_pipeline = sample_config.settings.event_pipeline.__class__(
        mode="event_first",
        analysis_quiet_seconds=60,
        analysis_min_interval_seconds=300,
    )
    sample_config.settings.edition_scopes = {
        "berdyansk-unc": EditionScopeConfig(
            name="Бердянск",
            focus_places=("Бердянск",),
        )
    }

    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk-unc', 'Berdyansk UNC') RETURNING id"
    )
    edition_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name)
        VALUES ('telegram', 'channel', '-1004245', 'https://t.me/example4', 'Example 4')
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
        VALUES (%s, 'message', 'msg-unc', now())
        RETURNING id
        """,
        (source_id,),
    )
    item_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'hash_rev_unc', 'Где-то прошел сильный дождь')
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
        OVERRIDING SYSTEM VALUE VALUES (9921, 'hash_cunc', '[1, 0, 0, 0]'::vector, 'model', 4)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (id, source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, drop_reason, created_at)
        OVERRIDING SYSTEM VALUE VALUES (9922, %s, 0, 'Text UNC', 'hash_cunc', 'v1', TRUE, NULL, %s)
        """,
        (rev_id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES (9923, 9922, 9921)
        """
    )

    aid = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid,
        fragment_id=9922,
        fragment_embedding_id=9923,
        assignment_kind="new_story",
    )

    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=sid,
        centroid=[1.0, 0.0, 0.0, 0.0],
        model="model",
        dimensions=4,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid,
        analysis_dirty=True,
    )

    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "mock_provider"
    mock_ai.model_name = "mock-model"

    triage_output = {
        "results": [
            {
                "story_id": sid,
                "scope": "UNCERTAIN",
                "scope_confidence": 0.85,
                "scope_reason": "No geographic anchor",
                "decision": "ANALYZE",
                "exclusion_reason": None,
                "confidence": 0.95,
                "reason": "OK",
            }
        ]
    }
    mock_ai.generate_text.return_value = json.dumps(triage_output)

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
    assert stats["gated"] >= 1
    assert stats["scope_uncertain"] == 1
    assert stats["analyzed"] == 0

    state = await cluster_repo.get_cluster_state(conn, sid)
    assert state is not None
    assert state.analysis_dirty is False
    assert state.last_analyzed_assignment_id == aid


@pytest.mark.postgres
async def test_coalesce_dirty_stories_scope_rescreen_preserves_enrichment(
    conn, pool, uow, sample_config
):
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    sample_config.settings.event_pipeline = sample_config.settings.event_pipeline.__class__(
        mode="event_first",
        analysis_quiet_seconds=60,
        analysis_min_interval_seconds=300,
    )
    sample_config.settings.edition_scopes = {
        "berdyansk-rescreen": EditionScopeConfig(
            name="Бердянск",
            focus_places=("Бердянск",),
        )
    }

    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk-rescreen', 'Berdyansk Rescreen') RETURNING id"
    )
    edition_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name)
        VALUES ('telegram', 'channel', '-1004246', 'https://t.me/example5', 'Example 5')
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
        VALUES (%s, 'message', 'msg-rs', now())
        RETURNING id
        """,
        (source_id,),
    )
    item_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'hash_rev_rs', 'Ремонт в Бердянске')
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
        OVERRIDING SYSTEM VALUE VALUES (9931, 'hash_crs', '[1, 0, 0, 0]'::vector, 'model', 4)
        """
    )
    await conn.execute(
        """
        INSERT INTO source_fragments (id, source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, drop_reason, created_at)
        OVERRIDING SYSTEM VALUE VALUES (9932, %s, 0, 'Text RS', 'hash_crs', 'v1', TRUE, NULL, %s)
        """,
        (rev_id, now),
    )
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES (9933, 9932, 9931)
        """
    )

    aid = await cluster_repo.assign_fragment_to_story(
        conn,
        story_id=sid,
        fragment_id=9932,
        fragment_embedding_id=9933,
        assignment_kind="new_story",
    )

    # Pre-create an enriched revision
    cur = await conn.execute(
        """
        INSERT INTO story_revisions (story_id, revision_no, title, summary, current_state, semantic_text, content_hash)
        VALUES (%s, 1, 'Existing Headline', 'Existing Summary', 'active', 'Semantic text', 'hash_rs1')
        RETURNING id
        """,
        (sid,),
    )
    rev_row = await cur.fetchone()
    assert rev_row is not None
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_row[0], sid)
    )

    # Cluster state has last_analyzed_assignment_id == aid, but analysis_dirty == True (e.g. forced re-screen)
    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=sid,
        centroid=[1.0, 0.0, 0.0, 0.0],
        model="model",
        dimensions=4,
        fragment_count=1,
        unique_source_count=1,
        first_seen_at=now,
        last_seen_at=now,
        latest_assignment_id=aid,
        analysis_dirty=True,
    )
    await cluster_repo.update_cluster_analysis_analyzed(
        conn,
        story_id=sid,
        assignment_id=aid,
        analyzed_at=now,
    )
    await conn.execute(
        "UPDATE story_cluster_state SET analysis_dirty = TRUE WHERE story_id = %s", (sid,)
    )

    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "mock_provider"
    mock_ai.model_name = "mock-model"

    triage_output = {
        "results": [
            {
                "story_id": sid,
                "scope": "LOCAL",
                "scope_confidence": 0.99,
                "scope_reason": "In Berdyansk",
                "decision": "ANALYZE",
                "exclusion_reason": None,
                "confidence": 0.95,
                "reason": "OK",
            }
        ]
    }
    # generate_text only called for triage, NOT for rich analysis!
    mock_ai.generate_text.return_value = json.dumps(triage_output)

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
    assert stats["gated"] >= 1
    assert stats["scope_local"] == 1
    assert stats["analyzed"] == 0  # Skipped re-analysis!

    state = await cluster_repo.get_cluster_state(conn, sid)
    assert state is not None
    assert state.analysis_dirty is False
