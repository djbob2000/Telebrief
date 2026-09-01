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
        await conn.execute(
            """
            INSERT INTO story_event_triage_decisions (
                run_id, story_id, latest_assignment_id, triage_version, scope_config_hash,
                decision, retention, enrichment, confidence, reason, created_at
            ) VALUES (%s, %s, %s, 'v2', 'hash-1', 'ANALYZE', 'KEEP', 'ANALYZE', 0.99, 'in city', %s)
            """,
            (trun_id, story_id, aid, _NOW),
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
        await conn.execute(
            """
            INSERT INTO story_event_triage_decisions (
                run_id, story_id, latest_assignment_id, triage_version, scope_config_hash,
                decision, retention, enrichment, confidence, reason, created_at
            ) VALUES
            (%s, %s, %s, 'v2', 'hash-abc', 'ANALYZE', 'KEEP', 'ANALYZE', 0.99, 'in city', %s),
            (%s, %s, %s, 'v2', 'hash-abc', 'ANALYZE', 'KEEP', 'ANALYZE', 0.95, 'impact', %s),
            (%s, %s, %s, 'v2', 'hash-abc', 'IGNORE', 'DROP', 'NONE', 0.99, 'external', %s)
            """,
            (
                trun_id,
                local_sid,
                aids[0],
                _NOW,
                trun_id,
                impact_sid,
                aids[1],
                _NOW,
                trun_id,
                oos_sid,
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

    async def test_snapshot_freezes_truthful_candidate_statistics(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Snapshot sealing captures exact distinct counts and records candidates cleanly."""
        from src.db.uow import DatabaseUnitOfWork
        from src.publication.repository import PublicationRepository
        from src.publication.snapshot import PublicationSnapshotService

        uow = DatabaseUnitOfWork(pool)
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        # Seed 2 stories with revisions
        s1, r1 = await _seed_story_with_revision(conn, edition.id)
        s2, r2 = await _seed_story_with_revision(conn, edition.id)

        service = PublicationSnapshotService(uow=uow, repo=repo)
        run = await service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            request_key="test-stats-run",
            policy_ids=policy_ids,
        )

        candidates = await service.seal_candidates(run.id)
        assert len(candidates) == 2
        cand_story_ids = {c.story_id for c in candidates}
        assert cand_story_ids == {s1, s2}

    async def test_snapshot_seals_single_source_community_event_candidates(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Single-source community kept stories with event_first payload are preserved in candidate snapshots."""
        import json

        from src.db.uow import DatabaseUnitOfWork
        from src.publication.repository import PublicationRepository
        from src.publication.snapshot import PublicationSnapshotService

        uow = DatabaseUnitOfWork(pool)
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        # Create single community source fragment
        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, external_id, url, name, role) VALUES ('telegram', 'channel', '-100222', 'https://t.me/c', 'Чат', 'community') RETURNING id"
        )
        src_id = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
            (src_id, edition.id),
        )
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', 'm-snap-comm', %s) RETURNING id",
            (src_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h-snap-1', 'Сообщение') RETURNING id",
            (item_id,),
        )
        sir_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'На Горе света нет', 'h-f-snap', 'v1', TRUE, %s) RETURNING id",
            (sir_id, _NOW),
        )
        frag_id = (await cur.fetchone())[0]

        # Story with event_payload
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at) VALUES (%s, 'active', 'event_first', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]

        event_payload = {
            "topic": "Отключение света",
            "category": "utilities",
            "urgency": "medium",
            "publishability": "brief",
            "headline": "Отключение света на Горе",
            "digest_summary": "Жители сообщают об отключении света.",
            "evidence_items": [
                {
                    "text": "На Горе света нет",
                    "kind": "community_report",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [frag_id],
                }
            ],
        }

        cur = await conn.execute(
            """
            INSERT INTO story_revisions (
                story_id, revision_no, current_state, semantic_text, content_hash,
                title, summary, event_payload, created_at
            ) VALUES (%s, 1, 'open', %s, 'h-rev-snap-comm', %s, %s, %s, %s)
            RETURNING id
            """,
            (
                story_id,
                event_payload["digest_summary"],
                event_payload["headline"],
                event_payload["digest_summary"],
                json.dumps(event_payload),
                _NOW,
            ),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )

        # Insert fragment embeddings
        cur = await conn.execute(
            """
            INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
            VALUES ('h-f-snap', '[1, 0]'::vector, 'm', 2)
            ON CONFLICT (normalized_hash, model, dimensions) DO UPDATE SET embedding = EXCLUDED.embedding
            RETURNING id
            """
        )
        vid = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
            VALUES (%s, %s)
            RETURNING id
            """,
            (frag_id, vid),
        )
        sfe_id = (await cur.fetchone())[0]

        # Assign fragment to story
        cur = await conn.execute(
            """
            INSERT INTO story_fragments (story_id, fragment_id, fragment_embedding_id, assignment_kind)
            VALUES (%s, %s, %s, 'new_story')
            RETURNING id
            """,
            (story_id, frag_id, sfe_id),
        )
        aid = (await cur.fetchone())[0]

        # Story cluster state
        await conn.execute(
            """
            INSERT INTO story_cluster_state (story_id, centroid, model, dimensions, fragment_count, unique_source_count, first_seen_at, last_seen_at, latest_assignment_id, analysis_dirty)
            VALUES (%s, '[1,0]'::vector, 'm', 2, 1, 1, %s, %s, %s, FALSE)
            """,
            (story_id, _NOW, _NOW, aid),
        )

        # Create triage run
        cur = await conn.execute(
            """
            INSERT INTO story_event_triage_runs (triage_version, provider, model, prompt_hash, story_count, input_chars, status)
            VALUES ('v1', 'p', 'm', 'h', 1, 100, 'succeeded')
            RETURNING id
            """
        )
        trun_id = (await cur.fetchone())[0]

        # Scope and Triage decisions
        await conn.execute(
            """
            INSERT INTO story_edition_scope_decisions (triage_run_id, story_id, edition_id, latest_assignment_id, scope_version, scope_config_hash, scope_class, confidence, reason, created_at)
            VALUES (%s, %s, %s, %s, 'v1', 'elig-hash-1', 'LOCAL', 0.99, 'in city', %s)
            """,
            (trun_id, story_id, edition.id, aid, _NOW),
        )
        await conn.execute(
            """
            INSERT INTO story_event_triage_decisions (
                run_id, story_id, latest_assignment_id, triage_version, scope_config_hash,
                decision, retention, enrichment, confidence, reason, created_at
            ) VALUES
            (%s, %s, %s, 'v2', 'elig-hash-1', 'ANALYZE', 'KEEP', 'BRIEF', 0.99, 'in city', %s)
            """,
            (trun_id, story_id, aid, _NOW),
        )

        service = PublicationSnapshotService(uow=uow, repo=repo)
        run = await service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            request_key="test-single-comm-snap",
            policy_ids=policy_ids,
        )

        candidates = await service.seal_candidates(run.id)
        assert len(candidates) == 1
        assert candidates[0].story_id == story_id
        assert candidates[0].story_revision_id == rev_id

    async def test_frozen_policy_versioning_and_defensive_seal_invariants(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        import json

        from src.db.uow import DatabaseUnitOfWork
        from src.publication.snapshot import PublicationSnapshotService

        uow = DatabaseUnitOfWork(pool)
        repo = PublicationRepository()
        policy_repo = PublicationPolicyRepository()

        # 1. Eligibility policy requiring frozen versions: triage_version='v9', scope_config_hash='hash-v9'
        elig = await policy_repo.get_or_create_eligibility_policy(
            conn,
            edition_id=edition.id,
            config_hash="cfg-v9-freeze",
            prompt_version="p-v9",
            config={
                "lookback_hours": 24,
                "triage_version": "v9",
                "scope_version": "v1",
                "scope_config_hash": "hash-v9",
            },
        )
        elig_id = elig.id
        sel = await policy_repo.get_or_create_selection_policy(
            conn, edition_id=edition.id, config_hash="sel-v9-freeze", prompt_version="p-v9"
        )
        wri = await policy_repo.get_or_create_writer_policy(
            conn, edition_id=edition.id, config_hash="wri-v9-freeze", prompt_version="p-v9"
        )
        policy_ids = (elig_id, sel.id, wri.id)

        # 2. Seed a story with stale triage_version='v8'
        sid_stale, rev_stale = await _seed_story_with_revision(conn, edition.id, _NOW)
        await conn.execute(
            "UPDATE stories SET knowledge_source = 'event_first' WHERE id = %s", (sid_stale,)
        )
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (1, 'message', 'ext-stale', %s) RETURNING id",
            (_NOW,),
        )
        si_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h-sir-stale', 'stale') RETURNING id",
            (si_id,),
        )
        sir_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'stale', 'h_f_stale', 'v1', TRUE, %s) RETURNING id",
            (sir_id, _NOW),
        )
        fid_stale = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions) VALUES ('h_stale_1', '[1, 0]'::vector, 'm', 2) RETURNING id"
        )
        fe_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_fragment_embeddings (fragment_id, vector_id) VALUES (%s, %s) RETURNING id",
            (fid_stale, fe_id),
        )
        sfe_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_fragments (story_id, fragment_id, fragment_embedding_id, assignment_kind) VALUES (%s, %s, %s, 'new_story') RETURNING id",
            (sid_stale, fid_stale, sfe_id),
        )
        aid_stale = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO story_cluster_state (story_id, centroid, model, dimensions, fragment_count, unique_source_count, first_seen_at, last_seen_at, latest_assignment_id, analysis_dirty) VALUES (%s, '[1, 0]'::vector, 'm', 2, 1, 1, %s, %s, %s, FALSE)",
            (sid_stale, _NOW, _NOW, aid_stale),
        )
        cur = await conn.execute(
            "INSERT INTO story_event_triage_runs (triage_version, provider, model, prompt_hash, story_count, input_chars, status) VALUES ('v1', 'p', 'm', 'h', 1, 100, 'succeeded') RETURNING id"
        )
        trun_id = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO story_edition_scope_decisions (triage_run_id, story_id, edition_id, latest_assignment_id, scope_version, scope_config_hash, scope_class, confidence, reason, created_at) VALUES (%s, %s, %s, %s, 'v1', 'hash-v9', 'LOCAL', 0.99, 'in city', %s)",
            (trun_id, sid_stale, edition.id, aid_stale, _NOW),
        )
        # triage_version='v8' is stale
        await conn.execute(
            "INSERT INTO story_event_triage_decisions (run_id, story_id, latest_assignment_id, triage_version, scope_config_hash, decision, retention, enrichment, confidence, reason, created_at) VALUES (%s, %s, %s, 'v8', 'hash-v9', 'ANALYZE', 'KEEP', 'BRIEF', 0.99, 'in city', %s)",
            (trun_id, sid_stale, aid_stale, _NOW),
        )

        # 3. Seed a story with triage_version='v9' (authoritative match) with >=1 PUBLISH evidence item
        sid_v9, rev_v9 = await _seed_story_with_revision(conn, edition.id, _NOW)
        await conn.execute(
            "UPDATE stories SET knowledge_source = 'event_first' WHERE id = %s", (sid_v9,)
        )
        payload = {
            "topic": "Водоснабжение",
            "publishability": "news",
            "evidence_items": [
                {
                    "text": "Воду дали в центре",
                    "kind": "service_access",
                    "publication_use": "PUBLISH",
                }
            ],
        }
        await conn.execute(
            "UPDATE story_revisions SET event_payload = %s::jsonb WHERE id = %s",
            (json.dumps(payload), rev_v9),
        )
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 2, 'h-sir-v9', 'v9 text') RETURNING id",
            (si_id,),
        )
        sir_id_v9 = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'v9 text', 'h_f_v9', 'v1', TRUE, %s) RETURNING id",
            (sir_id_v9, _NOW),
        )
        fid_v9 = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_fragment_embeddings (fragment_id, vector_id) VALUES (%s, %s) RETURNING id",
            (fid_v9, fe_id),
        )
        sfe_id_v9 = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_fragments (story_id, fragment_id, fragment_embedding_id, assignment_kind) VALUES (%s, %s, %s, 'new_story') RETURNING id",
            (sid_v9, fid_v9, sfe_id_v9),
        )
        aid_v9 = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO story_cluster_state (story_id, centroid, model, dimensions, fragment_count, unique_source_count, first_seen_at, last_seen_at, latest_assignment_id, analysis_dirty) VALUES (%s, '[1, 0]'::vector, 'm', 2, 1, 1, %s, %s, %s, FALSE)",
            (sid_v9, _NOW, _NOW, aid_v9),
        )
        await conn.execute(
            "INSERT INTO story_edition_scope_decisions (triage_run_id, story_id, edition_id, latest_assignment_id, scope_version, scope_config_hash, scope_class, confidence, reason, created_at) VALUES (%s, %s, %s, %s, 'v1', 'hash-v9', 'LOCAL', 0.99, 'in city', %s)",
            (trun_id, sid_v9, edition.id, aid_v9, _NOW),
        )
        await conn.execute(
            "INSERT INTO story_event_triage_decisions (run_id, story_id, latest_assignment_id, triage_version, scope_config_hash, decision, retention, enrichment, confidence, reason, created_at) VALUES (%s, %s, %s, 'v9', 'hash-v9', 'ANALYZE', 'KEEP', 'BRIEF', 0.99, 'in city', %s)",
            (trun_id, sid_v9, aid_v9, _NOW),
        )

        # 4. Verify eligible_story_revisions filters out stale sid_stale and includes sid_v9
        eligible = await repo.eligible_story_revisions(
            conn,
            edition_id=edition.id,
            snapshot_at=_NOW,
            eligibility_policy_id=elig_id,
        )
        eligible_sids = [e["story_id"] for e in eligible]
        assert sid_v9 in eligible_sids
        assert sid_stale not in eligible_sids

        # 5. Verify snapshot service seals successfully
        service = PublicationSnapshotService(uow=uow, repo=repo)
        run = await service.create_run(
            edition_id=edition.id,
            publication_type="daily_article",
            snapshot_at=_NOW,
            request_key="test-frozen-v9-run",
            policy_ids=policy_ids,
        )
        cands = await service.seal_candidates(run.id)
        assert len(cands) == 1
        assert cands[0].story_id == sid_v9

        # 6. Verify defensive seal invariant rejects candidate with 0 PUBLISH evidence items
        context_only_payload = {
            "topic": "Вопрос",
            "publishability": "news",
            "evidence_items": [
                {
                    "text": "Вопрос без ответа?",
                    "kind": "resident_question",
                    "publication_use": "CONTEXT",
                }
            ],
        }
        await conn.execute(
            "UPDATE story_revisions SET event_payload = %s::jsonb WHERE id = %s",
            (json.dumps(context_only_payload), rev_v9),
        )
        run_no_pub = await service.create_run(
            edition_id=edition.id,
            publication_type="daily_article",
            snapshot_at=_NOW,
            request_key="test-run-no-pub",
            policy_ids=policy_ids,
        )
        with pytest.raises(ValueError, match="0 PUBLISH evidence items"):
            await service.seal_candidates(run_no_pub.id)

        # Restore valid payload and verify non-KEEP retention is rejected
        await conn.execute(
            "UPDATE story_revisions SET event_payload = %s::jsonb WHERE id = %s",
            (json.dumps(payload), rev_v9),
        )
        await conn.execute(
            "UPDATE story_event_triage_decisions SET retention = 'DROP', enrichment = 'NONE' WHERE story_id = %s",
            (sid_v9,),
        )
        run_non_keep = await service.create_run(
            edition_id=edition.id,
            publication_type="daily_article",
            snapshot_at=_NOW,
            request_key="test-run-non-keep",
            policy_ids=policy_ids,
        )
        # Note: repository eligible_story_revisions filters setd.decision IN ('ANALYZE', 'KEEP')
        # If someone forced a candidate through, _seal_on rejects it
        # Let's test _seal_on defense-in-depth directly or via a mock row if query already drops it
        # In this case setd.decision was ANALYZE, but retention was changed to DROP:
        # The query lets it through because decision='ANALYZE', but _seal_on catches retention != KEEP!
        with pytest.raises(ValueError, match="non-KEEP retention"):
            await service.seal_candidates(run_non_keep.id)
