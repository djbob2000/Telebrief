"""Tests for Publication snapshot, candidate constraints, and idempotency (Plan 4 Task 1)."""

import datetime as dt

import psycopg
import pytest

from src.publication.repository import (
    PublicationPolicyRepository,
    PublicationRepository,
)

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


async def _seed_policies(conn: psycopg.AsyncConnection, edition_id: int) -> tuple[int, int, int]:
    policy_repo = PublicationPolicyRepository()
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition_id, config_hash="elig-hash-1", prompt_version="elig-v1"
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition_id, config_hash="sel-hash-1", prompt_version="sel-v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition_id, config_hash="wri-hash-1", prompt_version="wri-v1"
    )
    return (elig.id, sel.id, wri.id)


async def _seed_story_with_revision(
    conn: psycopg.AsyncConnection, edition_id: int, created_at: dt.datetime = _NOW
) -> tuple[int, int]:
    from tests.publication.conftest import seed_claim_for_story

    cur = await conn.execute(
        "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
        (edition_id, created_at),
    )
    story_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
        VALUES (%s, 1, 'open', 'Бердянск новость', 'hash-1', %s)
        RETURNING id
        """,
        (story_id, created_at),
    )
    rev_id = (await cur.fetchone())[0]
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
    )
    await seed_claim_for_story(conn, edition_id, story_id, created_at)
    return story_id, rev_id


@pytest.mark.postgres
class TestPublicationSnapshotConstraints:
    """Tests DB constraints and idempotency for publication runs and candidates."""

    async def test_scheduled_request_key_idempotency(self, conn: psycopg.AsyncConnection, edition):
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        req_key = "scheduled:berdyansk:article:2026-08-22T20:00:00+03:00"

        run1 = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key=req_key,
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        assert run1.id is not None
        assert run1.status == "created"

        # Calling again with identical request_key resolves existing run without creating duplicate
        run2 = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key=req_key,
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        assert run2.id == run1.id

    async def test_candidate_duplicate_story_in_same_run_is_rejected(
        self, conn: psycopg.AsyncConnection, edition
    ):
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)
        run = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-key-candidates",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        story_id, rev_id = await _seed_story_with_revision(conn, edition.id)

        cand1 = await repo.insert_candidate(
            conn,
            run.id,
            story_id=story_id,
            story_revision_id=rev_id,
            deterministic_rank=1,
        )
        assert cand1.id is not None

        # Inserting duplicate story for same run violates uniqueness
        with pytest.raises(psycopg.errors.UniqueViolation):
            await repo.insert_candidate(
                conn,
                run.id,
                story_id=story_id,
                story_revision_id=rev_id,
                deterministic_rank=2,
            )

    async def test_unverified_and_single_source_stories_are_eligible_candidates(
        self, conn: psycopg.AsyncConnection, edition
    ):
        from src.publication.repository import PublicationRepository

        repo = PublicationRepository()
        story_id, rev_id = await _seed_story_with_revision(conn, edition.id)

        # Neither evidence clusters nor verification assessments exist for this story
        eligible = await repo.eligible_story_revisions(
            conn,
            edition_id=edition.id,
            snapshot_at=_NOW,
        )

        assert len(eligible) == 1
        assert eligible[0]["story_id"] == story_id
        assert eligible[0]["story_revision_id"] == rev_id

    async def test_event_first_story_eligibility_and_fragment_freezing(
        self, conn: psycopg.AsyncConnection, edition
    ):
        from src.publication.models import PublicationSelectionDecision
        from src.publication.repository import PublicationRepository

        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        # 1. Create event_first story with event_payload
        cur = await conn.execute(
            """
            INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
            VALUES (%s, 'active', 'event_first', %s)
            RETURNING id
            """,
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]

        import json

        event_payload = {
            "topic": "Ремонт дорог",
            "headline": "Ремонт на Восточном проспекте",
            "digest_summary": "Дорожники укладывают асфальт.",
            "publishability": "news",
            "confidence_score": 0.95,
        }
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (
                story_id, revision_no, current_state, semantic_text, content_hash,
                event_payload, created_at
            ) VALUES (%s, 1, 'open', 'Ремонт на Восточном проспекте', 'h-ev-1', %s, %s)
            RETURNING id
            """,
            (story_id, json.dumps(event_payload), _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )

        # Setup source item, fragment, embedding, assignment, and cluster state
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name)
            VALUES ('telegram', 'channel', '-10099', 'https://t.me/test', 'Test')
            RETURNING id
            """
        )
        src_id = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
            (src_id, edition.id),
        )
        cur = await conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
            VALUES (%s, 'message', 'msg-ev-1', %s)
            RETURNING id
            """,
            (src_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
            VALUES (%s, 1, 'h-sir-1', 'Текст фрагмента')
            RETURNING id
            """,
            (item_id,),
        )
        sir_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
            VALUES (%s, 0, 'Текст фрагмента', 'h-frag-1', 'v1', TRUE, %s)
            RETURNING id
            """,
            (sir_id, _NOW),
        )
        frag_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
            VALUES ('h-frag-1', '[1, 0]'::vector, 'm', 2)
            ON CONFLICT (normalized_hash, model, dimensions) DO UPDATE SET embedding = EXCLUDED.embedding
            RETURNING id

            """
        )
        vec_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
            VALUES (%s, %s)
            RETURNING id
            """,
            (frag_id, vec_id),
        )
        sfe_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO story_fragments (story_id, fragment_id, fragment_embedding_id, assignment_kind)
            VALUES (%s, %s, %s, 'new_story')
            RETURNING id
            """,
            (story_id, frag_id, sfe_id),
        )
        aid = (await cur.fetchone())[0]

        await conn.execute(
            """
            INSERT INTO story_cluster_state (story_id, centroid, model, dimensions, fragment_count, unique_source_count, first_seen_at, last_seen_at, latest_assignment_id, analysis_dirty)
            VALUES (%s, '[1, 0]'::vector, 'm', 2, 1, 1, %s, %s, %s, FALSE)
            """,
            (story_id, _NOW, _NOW, aid),
        )

        cur = await conn.execute(
            """
            INSERT INTO story_event_triage_runs (triage_version, provider, model, prompt_hash, story_count, input_chars, status)
            VALUES ('v1', 'p', 'm', 'h', 1, 100, 'succeeded')
            RETURNING id
            """
        )
        trun_id = (await cur.fetchone())[0]

        await conn.execute(
            """
            INSERT INTO story_edition_scope_decisions (triage_run_id, story_id, edition_id, latest_assignment_id, scope_version, scope_config_hash, scope_class, confidence, reason, created_at)
            VALUES (%s, %s, %s, %s, 'v1', 'hash-1', 'LOCAL', 0.99, 'in city', %s)
            """,
            (trun_id, story_id, edition.id, aid, _NOW),
        )

        # 2. Check eligible_story_revisions finds this event_first story
        eligible = await repo.eligible_story_revisions(
            conn,
            edition_id=edition.id,
            snapshot_at=_NOW,
        )
        assert any(e["story_id"] == story_id for e in eligible)

        # 3. Create run and candidate
        run = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="digest_grouped",
            request_key="test-key-event-freeze",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        cand = await repo.insert_candidate(
            conn,
            run.id,
            story_id=story_id,
            story_revision_id=rev_id,
            deterministic_rank=1,
        )

        dec = await repo.insert_selection_decision(
            conn,
            run.id,
            PublicationSelectionDecision(
                id=0,
                publication_run_id=run.id,
                candidate_id=cand.id,
                decision="INCLUDE",
                presentation_intent="lead",
                confidence=0.95,
                reason="Important news",
                rank=1,
                metadata={},
                created_at=_NOW,
            ),
        )

        # 4. Freeze selected input with fragment_ids
        pub_input = await repo.freeze_selected_input(
            conn,
            run.id,
            story_id=story_id,
            story_revision_id=rev_id,
            selection_decision_id=dec.id,
            presentation_intent="lead",
            rank=1,
            fragment_ids=[frag_id],
        )
        assert pub_input.fragment_ids == [frag_id]

        # 5. Load sealed inputs
        sealed = await repo.load_sealed_inputs(conn, run.id)
        assert len(sealed) == 1
        assert sealed[0].fragment_ids == [frag_id]

    async def test_event_first_scope_gate_eligibility(self, conn: psycopg.AsyncConnection, edition):
        repo = PublicationRepository()
        from psycopg.types.json import Jsonb

        # 1. Create 4 event-first stories
        cur = await conn.execute(
            """
            INSERT INTO stories (edition_id, lifecycle_state, created_at, knowledge_source)
            VALUES (%s, 'active', %s, 'event_first')
            RETURNING id
            """,
            (edition.id, _NOW),
        )
        local_sid = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO stories (edition_id, lifecycle_state, created_at, knowledge_source)
            VALUES (%s, 'active', %s, 'event_first')
            RETURNING id
            """,
            (edition.id, _NOW),
        )
        impact_sid = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO stories (edition_id, lifecycle_state, created_at, knowledge_source)
            VALUES (%s, 'active', %s, 'event_first')
            RETURNING id
            """,
            (edition.id, _NOW),
        )
        oos_sid = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO stories (edition_id, lifecycle_state, created_at, knowledge_source)
            VALUES (%s, 'active', %s, 'event_first')
            RETURNING id
            """,
            (edition.id, _NOW),
        )
        nodup_sid = (await cur.fetchone())[0]

        # Add rich revisions for all 4
        payload = Jsonb({"publishability": "news", "headline": "Test", "confidence_score": 0.95})
        for sid in (local_sid, impact_sid, oos_sid, nodup_sid):
            cur = await conn.execute(
                """
                INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at, event_payload)
                VALUES (%s, 1, 'active', 'Text', 'h-1', %s, %s)
                RETURNING id
                """,
                (sid, _NOW, payload),
            )
            rid = (await cur.fetchone())[0]
            await conn.execute(
                "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rid, sid)
            )

        # Setup source, item, revision, and source_fragments for assignments
        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-10099', 'https://t.me/e', 'E') RETURNING id"
        )
        src_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', 'm-1', %s) RETURNING id",
            (src_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h-1', 'T') RETURNING id",
            (item_id,),
        )
        sir_id = (await cur.fetchone())[0]

        # Insert 4 fragments
        cur = await conn.execute(
            """
            INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
            VALUES
            (%s, 0, 'F1', 'h-f1', 'v1', TRUE, %s),
            (%s, 1, 'F2', 'h-f2', 'v1', TRUE, %s),
            (%s, 2, 'F3', 'h-f3', 'v1', TRUE, %s),
            (%s, 3, 'F4', 'h-f4', 'v1', TRUE, %s)
            RETURNING id
            """,
            (sir_id, _NOW, sir_id, _NOW, sir_id, _NOW, sir_id, _NOW),
        )
        frag_ids = [r[0] for r in await cur.fetchall()]

        # Insert fragment embeddings
        sfe_ids = []
        for idx, fid in enumerate(frag_ids):
            cur = await conn.execute(
                """
                INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
                VALUES (%s, '[1, 0]'::vector, 'm', 2)
                ON CONFLICT (normalized_hash, model, dimensions) DO UPDATE SET embedding = EXCLUDED.embedding
                RETURNING id

                """,
                (f"h-f{idx + 1}",),
            )
            vid = (await cur.fetchone())[0]
            cur = await conn.execute(
                """
                INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
                VALUES (%s, %s)
                RETURNING id
                """,
                (fid, vid),
            )
            sfe_ids.append((await cur.fetchone())[0])

        # Assign fragments to stories
        aids = []
        for sfe_id, (sid, fid) in zip(
            sfe_ids,
            zip((local_sid, impact_sid, oos_sid, nodup_sid), frag_ids, strict=False),
            strict=False,
        ):
            cur = await conn.execute(
                """
                INSERT INTO story_fragments (story_id, fragment_id, fragment_embedding_id, assignment_kind)
                VALUES (%s, %s, %s, 'new_story')
                RETURNING id
                """,
                (sid, fid, sfe_id),
            )
            aids.append((await cur.fetchone())[0])

        # Setup story_cluster_state for each
        for sid, aid in zip((local_sid, impact_sid, oos_sid, nodup_sid), aids, strict=False):
            await conn.execute(
                """
                INSERT INTO story_cluster_state (story_id, centroid, model, dimensions, fragment_count, unique_source_count, first_seen_at, last_seen_at, latest_assignment_id, analysis_dirty)
                VALUES (%s, '[1,0]'::vector, 'm', 2, 1, 1, %s, %s, %s, FALSE)
                """,
                (sid, _NOW, _NOW, aid),
            )

        # Create triage run
        cur = await conn.execute(
            """
            INSERT INTO story_event_triage_runs (triage_version, provider, model, prompt_hash, story_count, input_chars, status)
            VALUES ('v1', 'p', 'm', 'h', 4, 100, 'succeeded')
            RETURNING id
            """
        )
        trun_id = (await cur.fetchone())[0]

        # Insert scope decisions:
        # local_sid -> LOCAL (hash "hash-abc")
        # impact_sid -> DIRECT_IMPACT (hash "hash-abc")
        # oos_sid -> OUT_OF_SCOPE (hash "hash-abc")
        # nodup_sid -> has NO scope decision
        await conn.execute(
            """
            INSERT INTO story_edition_scope_decisions (triage_run_id, story_id, edition_id, latest_assignment_id, scope_version, scope_config_hash, scope_class, confidence, reason, created_at)
            VALUES
            (%s, %s, %s, %s, 'v1', 'hash-abc', 'LOCAL', 0.99, 'in city', %s),
            (%s, %s, %s, %s, 'v1', 'hash-abc', 'DIRECT_IMPACT', 0.95, 'impact', %s),
            (%s, %s, %s, %s, 'v1', 'hash-abc', 'OUT_OF_SCOPE', 0.99, 'external', %s)
            """,
            (
                trun_id,
                local_sid,
                edition.id,
                aids[0],
                _NOW,
                trun_id,
                impact_sid,
                edition.id,
                aids[1],
                _NOW,
                trun_id,
                oos_sid,
                edition.id,
                aids[2],
                _NOW,
            ),
        )

        # 1. Query eligible with scope_config_hash = "hash-abc"
        eligible = await repo.eligible_story_revisions(
            conn,
            edition_id=edition.id,
            snapshot_at=_NOW,
            scope_config_hash="hash-abc",
        )
        eligible_sids = {r["story_id"] for r in eligible}
        assert local_sid in eligible_sids
        assert impact_sid in eligible_sids
        assert oos_sid not in eligible_sids
        assert nodup_sid not in eligible_sids

        # 2. Query eligible with different scope_config_hash -> neither is eligible
        eligible_other = await repo.eligible_story_revisions(
            conn,
            edition_id=edition.id,
            snapshot_at=_NOW,
            scope_config_hash="hash-different",
        )
        assert len(eligible_other) == 0

        # 3. Query without hash -> matches any valid v1 decision (local & impact)
        eligible_any = await repo.eligible_story_revisions(
            conn,
            edition_id=edition.id,
            snapshot_at=_NOW,
            scope_config_hash=None,
        )
        eligible_any_sids = {r["story_id"] for r in eligible_any}
        assert local_sid in eligible_any_sids
        assert impact_sid in eligible_any_sids
        assert oos_sid not in eligible_any_sids
        assert nodup_sid not in eligible_any_sids
