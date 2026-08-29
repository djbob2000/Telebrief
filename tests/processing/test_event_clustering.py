"""Tests for streaming deterministic event clustering."""

from __future__ import annotations

import datetime as dt

import pytest

from src.domain.event_pipeline import SourceFragment
from src.processing.event_clustering import (
    EventClusteringService,
    combine_centroids,
    normalize_vector,
)
from src.repositories.event_clusters import EventClusterRepository


@pytest.mark.unit
def test_vector_math_helpers():
    v = [3.0, 4.0]
    norm = normalize_vector(v)
    assert pytest.approx(norm[0], 0.001) == 0.6
    assert pytest.approx(norm[1], 0.001) == 0.8

    # Combine 2 vectors
    c = combine_centroids([1.0, 0.0], 1, [0.0, 1.0])
    assert pytest.approx(c[0], 0.001) == 0.7071
    assert pytest.approx(c[1], 0.001) == 0.7071


@pytest.mark.postgres
async def test_event_clustering_workflow(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    dim = 4

    # Insert 2 vector records in DB
    await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (id, normalized_hash, embedding, model, dimensions)
        OVERRIDING SYSTEM VALUE VALUES
        (2001, 'hash_a', '[1, 0, 0, 0]'::vector, 'test-model', 4),
        (2002, 'hash_b', '[0.95, 0.05, 0, 0]'::vector, 'test-model', 4),
        (2003, 'hash_c', '[0, 0, 1, 0]'::vector, 'test-model', 4)
        """
    )

    # Insert 3 fragments in DB
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (3001, %s, 0, 'Outage message 1', 'hash_a', 'v1', TRUE, NULL, %s),
        (3002, %s, 1, 'Outage message 2 similar', 'hash_b', 'v1', TRUE, NULL, %s),
        (3003, %s, 2, 'Unrelated topic message', 'hash_c', 'v1', TRUE, NULL, %s)
        """,
        (revision.id, now, revision.id, now, revision.id, now),
    )

    # Insert source_fragment_embeddings
    await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (id, fragment_id, vector_id)
        OVERRIDING SYSTEM VALUE VALUES
        (4001, 3001, 2001),
        (4002, 3002, 2002),
        (4003, 3003, 2003)
        """
    )

    frag1 = SourceFragment(
        3001, revision.id, 0, "Outage message 1", "hash_a", "v1", True, None, now
    )
    frag2 = SourceFragment(
        3002, revision.id, 1, "Outage message 2 similar", "hash_b", "v1", True, None, now
    )
    frag3 = SourceFragment(
        3003, revision.id, 2, "Unrelated topic message", "hash_c", "v1", True, None, now
    )

    service = EventClusteringService()
    cluster_repo = EventClusterRepository()

    # 1. Process frag1 -> Should create new story shell
    res1 = await service.process_fragment(
        conn,
        frag1,
        edition_id=edition.id,
        fragment_embedding_id=4001,
        vector=[1.0, 0.0, 0.0, 0.0],
        model="test-model",
        dimensions=dim,
        item_timestamp=now,
        join_similarity=0.84,
    )
    assert res1.assignment_kind == "new_story"
    assert res1.story_id > 0

    state1 = await cluster_repo.get_cluster_state(conn, res1.story_id)
    assert state1 is not None
    assert state1.fragment_count == 1
    assert state1.analysis_dirty is True

    # 2. Process frag2 (cosine sim ~ 0.99 with frag1) -> Should join frag1's story
    res2 = await service.process_fragment(
        conn,
        frag2,
        edition_id=edition.id,
        fragment_embedding_id=4002,
        vector=[0.95, 0.05, 0.0, 0.0],
        model="test-model",
        dimensions=dim,
        item_timestamp=now + dt.timedelta(minutes=10),
        join_similarity=0.84,
    )
    assert res2.assignment_kind == "vector_join"
    assert res2.story_id == res1.story_id
    assert res2.similarity is not None
    assert res2.similarity >= 0.84

    state2 = await cluster_repo.get_cluster_state(conn, res1.story_id)
    assert state2 is not None
    assert state2.fragment_count == 2
    assert state2.analysis_dirty is True

    # 3. Process frag3 (orthogonal vector [0, 0, 1, 0]) -> Should create new separate story
    res3 = await service.process_fragment(
        conn,
        frag3,
        edition_id=edition.id,
        fragment_embedding_id=4003,
        vector=[0.0, 0.0, 1.0, 0.0],
        model="test-model",
        dimensions=dim,
        item_timestamp=now + dt.timedelta(minutes=20),
        join_similarity=0.84,
    )
    assert res3.assignment_kind == "new_story"
    assert res3.story_id != res1.story_id
