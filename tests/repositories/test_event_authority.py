"""Exact-assignment Event-First authority repository tests."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from src.domain.event_authority import AuthorityTarget
from src.publication.repository import PublicationPolicyRepository, PublicationRepository
from src.repositories.event_authority import EventAuthorityRepository
from src.repositories.event_clusters import EventClusterRepository

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
    assert gaps[0].source_cutoff_at == SOURCE_CUTOFF
    assert gaps[0].snapshot_at == SNAPSHOT

    metrics = await EventClusterRepository().get_assignment_snapshot_metrics(
        conn,
        story_id=story_id,
        assignment_id=new_assignment,
        source_cutoff_at=SOURCE_CUTOFF,
    )
    assert metrics == (1, 1, SOURCE_CUTOFF - dt.timedelta(minutes=5))


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


@pytest.mark.unit
async def test_due_enrichment_query_orders_distinct_rows_by_last_seen_at():
    class Cursor:
        async def fetchall(self):
            return [(11, 101)]

    class Connection:
        def __init__(self):
            self.query = ""

        async def execute(self, query, params):
            self.query = query
            assert params[-1] == 10
            return Cursor()

    conn = Connection()
    result = await EventAuthorityRepository().list_due_enrichment_assignments(
        conn,
        now=dt.datetime.now(dt.timezone.utc),
        limit=10,
    )

    assert result == [(11, 101)]
    assert "SELECT DISTINCT sc.story_id, sc.latest_assignment_id, sc.last_seen_at" in conn.query
    assert "ORDER BY due.last_seen_at ASC, due.story_id ASC" in conn.query


@pytest.mark.postgres
async def test_gap_includes_event_story_without_revision(repo_conn):
    conn = repo_conn
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('gap-edition', 'Gap Edition') RETURNING id"
    )
    edition_id = int((await cursor.fetchone())[0])

    cursor = await conn.execute(
        """
        INSERT INTO stories (edition_id, knowledge_source, lifecycle_state, created_at)
        VALUES (%s, 'event_first', 'active', %s)
        RETURNING id
        """,
        (edition_id, SOURCE_CUTOFF - dt.timedelta(minutes=10)),
    )
    story_id = int((await cursor.fetchone())[0])

    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, name)
        VALUES ('telegram', 'channel', 'gap-source', 'Gap Source')
        RETURNING id
        """
    )
    source_id = int((await cursor.fetchone())[0])

    observed_at = SOURCE_CUTOFF - dt.timedelta(minutes=5)
    cursor = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', 'gap-item-1', %s) RETURNING id
        """,
        (source_id, observed_at),
    )
    item_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (
            source_item_id, revision_no, collected_at, content_hash, text_content
        ) VALUES (%s, 1, %s, 'gap-hash-1', 'gap text') RETURNING id
        """,
        (item_id, observed_at),
    )
    revision_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_fragments (
            source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, created_at
        ) VALUES (%s, 0, 'gap fragment', 'gap-frag-hash', 'v1', TRUE, %s) RETURNING id
        """,
        (revision_id, observed_at),
    )
    fragment_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (
            normalized_hash, embedding, model, dimensions
        ) VALUES ('gap-vec-hash', '[1, 0]'::vector, 'test', 2) RETURNING id
        """
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
    assignment_id = int((await cursor.fetchone())[0])

    policy = await PublicationPolicyRepository().get_or_create_eligibility_policy(
        conn,
        edition_id=edition_id,
        config_hash="authority-policy-gap",
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
    targets = await repo.find_authority_gap_targets(
        conn,
        edition_id=edition_id,
        snapshot_at=SNAPSHOT,
        source_cutoff_at=SOURCE_CUTOFF,
        eligibility_policy_id=policy.id,
    )
    assert [(t.story_id, t.assignment_id) for t in targets] == [(story_id, assignment_id)]


@pytest.mark.postgres
async def test_event_authority_gap_regressions(repo_conn):
    conn = repo_conn
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('reg-edition', 'Regression Edition') RETURNING id"
    )
    edition_id = int((await cursor.fetchone())[0])

    policy = await PublicationPolicyRepository().get_or_create_eligibility_policy(
        conn,
        edition_id=edition_id,
        config_hash="authority-reg-policy",
        prompt_version="v1",
        config={
            "lookback_hours": 24,
            "excluded_platforms": ["excluded_plat"],
            "triage_version": "v10",
            "scope_version": "v1",
            "scope_config_hash": "scope-hash",
        },
    )
    repo = PublicationRepository()

    # Helper to seed a story with fragment
    async def _seed_story(plat: str, obs_at: dt.datetime, suffix: str) -> tuple[int, int]:
        cursor = await conn.execute(
            """
            INSERT INTO stories (edition_id, knowledge_source, lifecycle_state, created_at)
            VALUES (%s, 'event_first', 'active', %s)
            RETURNING id
            """,
            (edition_id, obs_at),
        )
        s_id = int((await cursor.fetchone())[0])

        cursor = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, name)
            VALUES (%s, 'channel', %s, %s)
            RETURNING id
            """,
            (plat, f"src-{suffix}", f"Source {suffix}"),
        )
        src_id = int((await cursor.fetchone())[0])

        cursor = await conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
            VALUES (%s, 'message', %s, %s) RETURNING id
            """,
            (src_id, f"item-{suffix}", obs_at),
        )
        it_id = int((await cursor.fetchone())[0])

        cursor = await conn.execute(
            """
            INSERT INTO source_item_revisions (
                source_item_id, revision_no, collected_at, content_hash, text_content
            ) VALUES (%s, 1, %s, %s, %s) RETURNING id
            """,
            (it_id, obs_at, f"hash-{suffix}", suffix),
        )
        rev_id = int((await cursor.fetchone())[0])

        cursor = await conn.execute(
            """
            INSERT INTO source_fragments (
                source_item_revision_id, ordinal, text_content, normalized_hash,
                fragmenter_version, is_candidate, created_at
            ) VALUES (%s, 0, %s, %s, 'v1', TRUE, %s) RETURNING id
            """,
            (rev_id, suffix, f"frag-hash-{suffix}", obs_at),
        )
        frag_id = int((await cursor.fetchone())[0])

        cursor = await conn.execute(
            """
            INSERT INTO fragment_embedding_vectors (
                normalized_hash, embedding, model, dimensions
            ) VALUES (%s, '[1, 0]'::vector, 'test', 2) RETURNING id
            """,
            (f"vec-hash-{suffix}",),
        )
        vec_id = int((await cursor.fetchone())[0])

        cursor = await conn.execute(
            """
            INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
            VALUES (%s, %s) RETURNING id
            """,
            (frag_id, vec_id),
        )
        fe_id = int((await cursor.fetchone())[0])

        cursor = await conn.execute(
            """
            INSERT INTO story_fragments (
                story_id, fragment_id, fragment_embedding_id, assignment_kind, assigned_at
            ) VALUES (%s, %s, %s, 'new_story', %s) RETURNING id
            """,
            (s_id, frag_id, fe_id, obs_at),
        )
        as_id = int((await cursor.fetchone())[0])
        return s_id, as_id

    # 1. Valid KEEP + matching revision/payload -> not a gap
    s_keep, a_keep = await _seed_story("telegram", SOURCE_CUTOFF - dt.timedelta(hours=2), "keep")
    # Decision
    cursor = await conn.execute(
        """
        INSERT INTO story_event_triage_runs (
            triage_version, provider, model, prompt_hash, story_count, input_chars, status
        ) VALUES ('v10', 'test', 'test', 'auth-reg', 1, 1, 'succeeded') RETURNING id
        """
    )
    t_run_id = int((await cursor.fetchone())[0])
    await conn.execute(
        """
        INSERT INTO story_edition_scope_decisions (
            triage_run_id, story_id, edition_id, latest_assignment_id,
            scope_version, scope_config_hash, scope_class, confidence, reason, created_at
        ) VALUES (%s, %s, %s, %s, 'v1', 'scope-hash', 'LOCAL', 1, 'test', %s)
        """,
        (t_run_id, s_keep, edition_id, a_keep, SNAPSHOT),
    )
    await conn.execute(
        """
        INSERT INTO story_event_triage_decisions (
            run_id, story_id, latest_assignment_id, triage_version, decision,
            confidence, reason, scope_config_hash, retention, enrichment, brief_payload, created_at
        ) VALUES (%s, %s, %s, 'v10', 'ANALYZE', 1, 'test', 'scope-hash', 'KEEP', 'BRIEF', %s::jsonb, %s)
        """,
        (t_run_id, s_keep, a_keep, json.dumps({"publishability": "news"}), SNAPSHOT),
    )
    await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, event_assignment_id, current_state,
            semantic_text, content_hash, event_payload, created_at
        ) VALUES (%s, 1, %s, 'open', 'keep', 'keep-hash', %s::jsonb, %s)
        """,
        (s_keep, a_keep, json.dumps({"publishability": "news"}), SNAPSHOT),
    )

    # 2. Valid DROP decision -> not a gap
    s_drop, a_drop = await _seed_story("telegram", SOURCE_CUTOFF - dt.timedelta(hours=2), "drop")
    await conn.execute(
        """
        INSERT INTO story_edition_scope_decisions (
            triage_run_id, story_id, edition_id, latest_assignment_id,
            scope_version, scope_config_hash, scope_class, confidence, reason, created_at
        ) VALUES (%s, %s, %s, %s, 'v1', 'scope-hash', 'OUT_OF_SCOPE', 1, 'test', %s)
        """,
        (t_run_id, s_drop, edition_id, a_drop, SNAPSHOT),
    )
    await conn.execute(
        """
        INSERT INTO story_event_triage_decisions (
            run_id, story_id, latest_assignment_id, triage_version, decision,
            confidence, reason, scope_config_hash, retention, enrichment, brief_payload, created_at
        ) VALUES (%s, %s, %s, 'v10', 'IGNORE', 1, 'test', 'scope-hash', 'DROP', 'NONE', NULL, %s)
        """,
        (t_run_id, s_drop, a_drop, SNAPSHOT),
    )

    # 3. Decision exists for previous assignment -> new assignment remains a gap
    s_reassign, a_old = await _seed_story(
        "telegram", SOURCE_CUTOFF - dt.timedelta(hours=3), "reassign-old"
    )
    await conn.execute(
        """
        INSERT INTO story_edition_scope_decisions (
            triage_run_id, story_id, edition_id, latest_assignment_id,
            scope_version, scope_config_hash, scope_class, confidence, reason, created_at
        ) VALUES (%s, %s, %s, %s, 'v1', 'scope-hash', 'LOCAL', 1, 'test', %s)
        """,
        (t_run_id, s_reassign, edition_id, a_old, SNAPSHOT),
    )
    await conn.execute(
        """
        INSERT INTO story_event_triage_decisions (
            run_id, story_id, latest_assignment_id, triage_version, decision,
            confidence, reason, scope_config_hash, retention, enrichment, brief_payload, created_at
        ) VALUES (%s, %s, %s, 'v10', 'IGNORE', 1, 'test', 'scope-hash', 'DROP', 'NONE', NULL, %s)
        """,
        (t_run_id, s_reassign, a_old, SNAPSHOT),
    )
    # Add a second fragment/assignment on cutoff for s_reassign
    cursor = await conn.execute(
        """
        INSERT INTO source_fragments (
            source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, created_at
        ) VALUES (1, 1, 'reassign-second-frag', 'frag-hash-reassign-2', 'v1', TRUE, %s) RETURNING id
        """,
        (SOURCE_CUTOFF - dt.timedelta(minutes=10),),
    )
    frag_id_2 = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (
            normalized_hash, embedding, model, dimensions
        ) VALUES ('vec-hash-reassign-2', '[1, 0]'::vector, 'test', 2) RETURNING id
        """
    )
    vec_id_2 = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
        VALUES (%s, %s) RETURNING id
        """,
        (frag_id_2, vec_id_2),
    )
    fe_id_2 = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO story_fragments (
            story_id, fragment_id, fragment_embedding_id, assignment_kind, assigned_at
        ) VALUES (%s, %s, %s, 'vector_join', %s) RETURNING id
        """,
        (s_reassign, frag_id_2, fe_id_2, SOURCE_CUTOFF - dt.timedelta(minutes=10)),
    )
    a_new = int((await cursor.fetchone())[0])

    # 4. Fragment outside lookback -> not target
    s_old_window, _ = await _seed_story(
        "telegram", SOURCE_CUTOFF - dt.timedelta(hours=25), "outside-lookback"
    )

    # 5. Excluded platform -> not target
    s_excluded, _ = await _seed_story(
        "excluded_plat", SOURCE_CUTOFF - dt.timedelta(hours=1), "excluded"
    )

    # Query targets
    required = await repo.list_required_authority_targets(
        conn,
        edition_id=edition_id,
        snapshot_at=SNAPSHOT,
        source_cutoff_at=SOURCE_CUTOFF,
        eligibility_policy_id=policy.id,
    )
    required_sids = {t.story_id for t in required}
    assert s_old_window not in required_sids, "Story outside lookback should not be target"
    assert s_excluded not in required_sids, "Story on excluded platform should not be target"
    assert s_keep in required_sids
    assert s_drop in required_sids
    assert s_reassign in required_sids

    gaps = await repo.find_authority_gap_targets(
        conn,
        edition_id=edition_id,
        snapshot_at=SNAPSHOT,
        source_cutoff_at=SOURCE_CUTOFF,
        eligibility_policy_id=policy.id,
    )
    gap_sids = {t.story_id for t in gaps}
    assert s_keep not in gap_sids, "Valid KEEP story should not be gap"
    assert s_drop not in gap_sids, "Valid DROP story should not be gap"
    assert s_reassign in gap_sids, "Story with new assignment should remain gap"
    assert [(t.story_id, t.assignment_id) for t in gaps if t.story_id == s_reassign] == [
        (s_reassign, a_new)
    ]
