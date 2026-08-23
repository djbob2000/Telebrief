"""Tests for temporal publication snapshot semantics (Plan 4 Task 2)."""

import datetime as dt

import psycopg
import pytest

from src.db.uow import DatabaseUnitOfWork
from src.publication.policies import PublicationPolicyService
from src.publication.snapshot import PublicationSnapshotService

_T_19_40 = dt.datetime(2026, 8, 22, 19, 40, tzinfo=dt.timezone.utc)
_T_19_50 = dt.datetime(2026, 8, 22, 19, 50, tzinfo=dt.timezone.utc)
_T_19_55 = dt.datetime(2026, 8, 22, 19, 55, tzinfo=dt.timezone.utc)
_T_19_58 = dt.datetime(2026, 8, 22, 19, 58, tzinfo=dt.timezone.utc)
_T_20_01 = dt.datetime(2026, 8, 22, 20, 1, tzinfo=dt.timezone.utc)
_T_20_03 = dt.datetime(2026, 8, 22, 20, 3, tzinfo=dt.timezone.utc)


@pytest.mark.postgres
class TestTemporalPublicationSnapshots:
    """Ensures publication candidate snapshots strictly adhere to snapshot_at cutoff."""

    async def test_policy_service_reuses_identical_hashes_and_creates_new_on_change(
        self, conn: psycopg.AsyncConnection, edition
    ):
        policy_service = PublicationPolicyService()

        # Initial ensure_current
        policies1 = await policy_service.ensure_current(
            conn,
            edition_id=edition.id,
            publication_type="article",
            eligibility_config_hash="elig-1",
            selection_config_hash="sel-1",
            writer_config_hash="wri-1",
        )
        assert policies1.eligibility.version == 1
        assert policies1.selection.version == 1
        assert policies1.writer.version == 1

        # Second ensure_current with identical hashes reuses version 1
        policies2 = await policy_service.ensure_current(
            conn,
            edition_id=edition.id,
            publication_type="article",
            eligibility_config_hash="elig-1",
            selection_config_hash="sel-1",
            writer_config_hash="wri-1",
        )
        assert policies2.eligibility.id == policies1.eligibility.id
        assert policies2.selection.id == policies1.selection.id
        assert policies2.writer.id == policies1.writer.id

        # Third call with changed writer hash creates version 2 for writer only
        policies3 = await policy_service.ensure_current(
            conn,
            edition_id=edition.id,
            publication_type="article",
            eligibility_config_hash="elig-1",
            selection_config_hash="sel-1",
            writer_config_hash="wri-2-modified",
        )
        assert policies3.eligibility.id == policies1.eligibility.id
        assert policies3.selection.id == policies1.selection.id
        assert policies3.writer.version == 2
        assert policies3.writer.id != policies1.writer.id

    async def test_candidate_freezes_latest_revision_before_snapshot_at(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        service = PublicationSnapshotService(uow=uow)

        # 1. Create a story
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _T_19_40),
        )
        story_id = (await cur.fetchone())[0]

        # 2. Insert Revision #1 at 19:55
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Водоканал начал ремонт на АКЗ', 'hash-rev-1', %s)
            RETURNING id
            """,
            (story_id, _T_19_55),
        )
        rev1_id = (await cur.fetchone())[0]

        # 3. Insert Revision #2 at 20:03 (after snapshot cutoff of 19:58)
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 2, 'open', 'Ремонт завершен, вода подана', 'hash-rev-2', %s)
            RETURNING id
            """,
            (story_id, _T_20_03),
        )
        rev2_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev2_id, story_id)
        )

        # 4. Create publication run with cutoff snapshot_at = 19:58
        run = await service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_T_19_58,
            request_key="test-temporal-story-rev",
        )

        # 5. Seal candidates at present wall-clock (which is past 20:03)
        candidates = await service.seal_candidates(run.id)

        # 6. Assert candidate references Revision #1 (19:55), NOT Revision #2 (20:03)
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.story_id == story_id
        assert cand.story_revision_id == rev1_id
        assert cand.story_revision_id != rev2_id

    async def test_claims_after_snapshot_at_are_excluded_from_snapshot_features(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        service = PublicationSnapshotService(uow=uow)

        # Create source item
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name)
            VALUES ('telegram', 'channel', '-10099', 'https://t.me/ch', 'Chan') RETURNING id
            """
        )
        source_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', '1', %s) RETURNING id",
            (source_id, _T_19_40),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h', 'txt', %s) RETURNING id",
            (item_id, _T_19_40),
        )
        item_rev_id = (await cur.fetchone())[0]

        # Seed relevance decision
        cur = await conn.execute(
            """
            INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version)
            VALUES (%s, 1, 'h-rel', 'v-rel') RETURNING id
            """,
            (edition.id,),
        )
        rel_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason)
            VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id
            """,
            (item_rev_id, edition.id, rel_pol_id),
        )
        rel_dec_id = (await cur.fetchone())[0]

        # Seed claim extraction run
        cur = await conn.execute(
            """
            INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version)
            VALUES (%s, 1, 'h', 'v') RETURNING id
            """,
            (edition.id,),
        )
        extr_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status)
            VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id
            """,
            (item_rev_id, edition.id, extr_pol_id, rel_dec_id),
        )
        extr_run_id = (await cur.fetchone())[0]

        # Story created 19:50 with rev 1
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _T_19_50),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Новость о дорогах', 'hash-1', %s) RETURNING id
            """,
            (story_id, _T_19_50),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )

        # Claim 1 created & attached at 19:50 (before snapshot)
        cur = await conn.execute(
            """
            INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at)
            VALUES (%s, %s, %s, 'claim 1', 'claim 1', %s) RETURNING id
            """,
            (extr_run_id, item_rev_id, edition.id, _T_19_50),
        )
        claim1_id = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, claim1_id, _T_19_50),
        )

        # Claim 2 created & attached at 20:01 (after snapshot cutoff of 19:58)
        cur = await conn.execute(
            """
            INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at)
            VALUES (%s, %s, %s, 'claim 2', 'claim 2', %s) RETURNING id
            """,
            (extr_run_id, item_rev_id, edition.id, _T_20_01),
        )
        claim2_id = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, claim2_id, _T_20_01),
        )

        # Create run at snapshot 19:58
        run = await service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_T_19_58,
            request_key="test-temporal-claim-counts",
        )
        candidates = await service.seal_candidates(run.id)
        assert len(candidates) == 1
        # Snapshot features must count only claim 1 (count = 1), not claim 2
        assert candidates[0].snapshot_features["claim_count"] == 1
