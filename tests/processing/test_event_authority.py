"""Gate authority regressions for exact assignment boundaries."""

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
async def test_gate_target_excludes_post_assignment_evidence(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    old_at = now - dt.timedelta(minutes=5)
    new_at = now - dt.timedelta(minutes=1)
    story_id = await StoryRepository().create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    cluster_repo = EventClusterRepository()
    assignments: list[tuple[int, str]] = []
    fragment_ids: list[int] = []
    for suffix, assigned_at in (("old", old_at), ("new", new_at)):
        cursor = await conn.execute(
            """
            INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
            VALUES (%s, '[1, 0]'::vector, 'authority-test', 2) RETURNING id
            """,
            (f"authority-vector-{suffix}",),
        )
        vector_id = int((await cursor.fetchone())[0])
        cursor = await conn.execute(
            """
            INSERT INTO source_fragments (
                source_item_revision_id, ordinal, text_content, normalized_hash,
                fragmenter_version, is_candidate, created_at
            ) VALUES (%s, %s, %s, %s, 'v1', TRUE, %s) RETURNING id
            """,
            (
                revision.id,
                len(assignments),
                f"fragment {suffix}",
                f"authority-{suffix}",
                assigned_at,
            ),
        )
        fragment_id = int((await cursor.fetchone())[0])
        cursor = await conn.execute(
            """
            INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
            VALUES (%s, %s) RETURNING id
            """,
            (fragment_id, vector_id),
        )
        embedding_id = int((await cursor.fetchone())[0])
        assignment_id = await cluster_repo.assign_fragment_to_story(
            conn,
            story_id=story_id,
            fragment_id=fragment_id,
            fragment_embedding_id=embedding_id,
            assignment_kind="new_story",
        )
        await conn.execute(
            "UPDATE story_fragments SET assigned_at = %s WHERE id = %s",
            (assigned_at, assignment_id),
        )
        assignments.append((assignment_id, suffix))
        fragment_ids.append(fragment_id)

    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=story_id,
        centroid=[1.0, 0.0],
        model="authority-test",
        dimensions=2,
        fragment_count=2,
        unique_source_count=1,
        first_seen_at=old_at,
        last_seen_at=new_at,
        latest_assignment_id=assignments[-1][0],
        analysis_dirty=True,
    )
    state = await cluster_repo.get_cluster_state(conn, story_id)
    assert state is not None

    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "test"
    mock_ai.model_name = "test"
    mock_ai.generate_text.return_value = json.dumps(
        {
            "results": [
                {
                    "story_id": story_id,
                    "scope": "LOCAL",
                    "scope_basis_fragment_ids": [fragment_ids[0]],
                    "scope_confidence": 0.99,
                    "scope_reason": "The older evidence is local.",
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "confidence": 0.99,
                    "reason": "Useful local report.",
                    "brief_payload": {
                        "publishability": "brief",
                        "topic": "Local report",
                        "headline": "Local report",
                        "digest_summary": "The older evidence is retained.",
                        "evidence_items": [
                            {
                                "text": "fragment old",
                                "kind": "community_report",
                                "publication_use": "PUBLISH",
                                "source_fragment_ids": [fragment_ids[0]],
                            }
                        ],
                    },
                }
            ]
        }
    )
    scope = EditionScopeConfig(
        name="Berdyansk", focus_places=("Berdyansk",), direct_impact_only=True
    )
    result = await StoryTriageService(mock_ai, cluster_repo=cluster_repo).triage_stories_batch(
        conn,
        [state],
        edition_id=edition.id,
        scope_config=scope,
        scope_hash=scope_config_hash(scope),
        assignment_id_by_story={story_id: assignments[0][0]},
    )

    assert result.deferred_story_ids == ()
    prompt = mock_ai.generate_text.await_args.kwargs["prompt"]
    assert "fragment old" in prompt
    assert "fragment new" not in prompt
    rows = await (
        await conn.execute(
            """
            SELECT latest_assignment_id
            FROM story_event_triage_decisions
            WHERE story_id = %s
            """,
            (story_id,),
        )
    ).fetchall()
    assert [int(row[0]) for row in rows] == [assignments[0][0]]
