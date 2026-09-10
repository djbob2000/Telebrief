"""Exact-assignment Event-First authority repository tests."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from src.domain.event_authority import AuthorityTarget
from src.publication.repository import PublicationPolicyRepository, PublicationRepository
from src.repositories.event_authority import EventAuthorityRepository

UTC = dt.timezone.utc
SNAPSHOT = dt.datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
SOURCE_CUTOFF = dt.datetime(2026, 9, 10, 8, 55, tzinfo=UTC)


@pytest.mark.postgres
async def test_authority_targets_use_assignment_at_cutoff_and_temporal_decisions(repo_conn):
    conn = repo_conn
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('authority-edition', 'Authority Edition') RETURNING id"
    )
    edition_id = int((await cursor.fetchone())[0])
    # Seed the story separately so the assignment helper can use its id.
    cursor = await conn.execute(
        """
        INSERT INTO stories (edition_id, knowledge_source, lifecycle_state, created_at)
        VALUES (%s, 'event_first', 'active', %s)
        RETURNING id
        """,
        (edition_id, SOURCE_CUTOFF - dt.timedelta(minutes=10)),
    )
    story_id = int((await cursor.fetchone())[0])

    # The helper's INSERT is intentionally replaced below with explicit setup.
    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, name)
        VALUES ('telegram', 'channel', 'authority-source-main', 'Authority Source Main')
        RETURNING id
        """
    )
    source_id = int((await cursor.fetchone())[0])
    assignment_ids: list[int] = []
    for suffix, observed_at in (
        ("old", SOURCE_CUTOFF - dt.timedelta(minutes=5)),
        ("new", SNAPSHOT),
    ):
        cursor = await conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
            VALUES (%s, 'message', %s, %s) RETURNING id
            """,
            (source_id, f"authority-main-{suffix}", observed_at),
        )
        item_id = int((await cursor.fetchone())[0])
        cursor = await conn.execute(
            """
            INSERT INTO source_item_revisions (
                source_item_id, revision_no, collected_at, content_hash, text_content
            ) VALUES (%s, 1, %s, %s, %s) RETURNING id
            """,
            (item_id, observed_at, f"authority-main-hash-{suffix}", suffix),
        )
        revision_id = int((await cursor.fetchone())[0])
        cursor = await conn.execute(
            """
            INSERT INTO source_fragments (
                source_item_revision_id, ordinal, text_content, normalized_hash,
                fragmenter_version, is_candidate, created_at
            ) VALUES (%s, 0, %s, %s, 'v1', TRUE, %s) RETURNING id
            """,
            (revision_id, suffix, f"authority-main-fragment-{suffix}", observed_at),
        )
        fragment_id = int((await cursor.fetchone())[0])
        cursor = await conn.execute(
            """
            INSERT INTO fragment_embedding_vectors (
                normalized_hash, embedding, model, dimensions
            ) VALUES (%s, '[1, 0]'::vector, 'test', 2) RETURNING id
            """,
            (f"authority-main-vector-{suffix}",),
        )
        vector_id = int((await cursor.fetchone())[0])
        cursor = await conn.execute(
            """
            INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
            VALUES (%s, %s) RETURNING id
            """,
            (fragment_id, vector_id),
        )
        fragment_embedding_id = int((await cursor.fetchone())[0])
        cursor = await conn.execute(
            """
            INSERT INTO story_fragments (
                story_id, fragment_id, fragment_embedding_id, assignment_kind, assigned_at
            ) VALUES (%s, %s, %s, 'new_story', %s) RETURNING id
            """,
            (story_id, fragment_id, fragment_embedding_id, observed_at),
        )
        assignment_ids.append(int((await cursor.fetchone())[0]))

        cursor = await conn.execute(
            """
            INSERT INTO story_revisions (
                story_id, revision_no, event_assignment_id, current_state,
                semantic_text, content_hash, event_payload, created_at
            ) VALUES (%s, %s, %s, 'open', %s, %s, %s::jsonb, %s)
            RETURNING id
            """,
            (
                story_id,
                len(assignment_ids),
                assignment_ids[-1],
                suffix,
                f"authority-main-story-{suffix}",
                json.dumps({"publishability": "news"}),
                observed_at,
            ),
        )
        revision_row = await cursor.fetchone()
        if suffix == "old":
            await conn.execute(
                "UPDATE stories SET current_revision_id = %s WHERE id = %s",
                (revision_row[0], story_id),
            )
    old_assignment, new_assignment = assignment_ids
    await conn.execute(
        """
        INSERT INTO story_cluster_state (
            story_id, centroid, model, dimensions, fragment_count,
            unique_source_count, first_seen_at, last_seen_at,
            latest_assignment_id, analysis_dirty
        ) VALUES (%s, '[1, 0]'::vector, 'test', 2, 2, 1, %s, %s, %s, TRUE)
        """,
        (story_id, SOURCE_CUTOFF - dt.timedelta(minutes=5), SNAPSHOT, new_assignment),
    )
    policy = await PublicationPolicyRepository().get_or_create_eligibility_policy(
        conn,
        edition_id=edition_id,
        config_hash="authority-policy",
        prompt_version="v1",
        config={
            "lookback_hours": 24,
            "excluded_platforms": [],
            "triage_version": "v10",
            "scope_version": "v1",
            "scope_config_hash": "scope-hash",
        },
    )
    repo = PublicationRepository()
    required = await repo.list_required_authority_targets(
        conn,
        edition_id=edition_id,
        snapshot_at=SNAPSHOT,
        source_cutoff_at=SOURCE_CUTOFF,
        eligibility_policy_id=policy.id,
    )
    assert [(target.story_id, target.assignment_id) for target in required] == [
        (story_id, old_assignment)
    ]

    cursor = await conn.execute(
        """
        INSERT INTO story_event_triage_runs (
            triage_version, provider, model, prompt_hash, story_count, input_chars, status
        ) VALUES ('v10', 'test', 'test', 'authority', 1, 1, 'succeeded')
        RETURNING id
        """
    )
    triage_run_id = int((await cursor.fetchone())[0])
    await conn.execute(
        """
        INSERT INTO story_edition_scope_decisions (
            triage_run_id, story_id, edition_id, latest_assignment_id,
            scope_version, scope_config_hash, scope_class, confidence, reason
        ) VALUES (%s, %s, %s, %s, 'v1', 'scope-hash', 'LOCAL', 1, 'test')
        """,
        (triage_run_id, story_id, edition_id, new_assignment),
    )
    await conn.execute(
        """
        INSERT INTO story_event_triage_decisions (
            run_id, story_id, latest_assignment_id, triage_version, decision,
            confidence, reason, scope_config_hash, retention, enrichment, brief_payload
        ) VALUES (%s, %s, %s, 'v10', 'ANALYZE', 1, 'test', 'scope-hash', 'KEEP', 'BRIEF', %s::jsonb)
        """,
        (triage_run_id, story_id, new_assignment, json.dumps({"publishability": "news"})),
    )
    gaps = await repo.find_authority_gap_targets(
        conn,
        edition_id=edition_id,
        snapshot_at=SNAPSHOT,
        source_cutoff_at=SOURCE_CUTOFF,
        eligibility_policy_id=policy.id,
    )
    assert [(target.story_id, target.assignment_id) for target in gaps] == [
        (story_id, old_assignment)
    ]


@pytest.mark.postgres
async def test_filter_unsatisfied_targets_requires_exact_temporal_authority(repo_conn):
    conn = repo_conn
    repository = EventAuthorityRepository()
    target = AuthorityTarget(
        story_id=1,
        assignment_id=2,
        edition_id=3,
        triage_version="v10",
        scope_version="v1",
        scope_config_hash="hash",
    )
    assert await repository.filter_unsatisfied_targets(
        conn, targets=[target], snapshot_at=SNAPSHOT
    ) == [target]
