"""Tests for KnowledgeEditorialAdapter and generation observers (Plan 4 Task 4)."""

import datetime as dt

import psycopg
import pytest

from src.db.uow import DatabaseUnitOfWork
from src.publication.editorial_adapter import (
    DatabaseGenerationAttemptObserver,
    KnowledgeEditorialAdapter,
)
from src.publication.selection import (
    EditorialSelectionService,
    HeuristicSelectionModel,
)
from src.publication.snapshot import PublicationSnapshotService

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


@pytest.mark.postgres
class TestKnowledgeEditorialAdapter:
    """Tests building Story Cards from persistent sealed knowledge."""

    async def test_build_creates_cards_and_bundle_from_sealed_inputs(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)
        sel_service = EditorialSelectionService(uow=uow, model=HeuristicSelectionModel())
        adapter = KnowledgeEditorialAdapter(uow=uow)

        # 1. Seed source, source_item, revision
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name)
            VALUES ('telegram', 'channel', '-1001234', 'https://t.me/b_adm', 'Бердянск Официально')
            RETURNING id
            """
        )
        source_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', '101', %s) RETURNING id",
            (source_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h-101', 'С 1 сентября вводятся новые тарифы на проезд', %s) RETURNING id",
            (item_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]

        # 2. Seed relevance + claim extraction
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'rh', 'rv') RETURNING id",
            (edition.id,),
        )
        rel_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id",
            (rev_id, edition.id, rel_pol_id),
        )
        rel_dec_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'ch', 'cv') RETURNING id",
            (edition.id,),
        )
        extr_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status) VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id",
            (rev_id, edition.id, extr_pol_id, rel_dec_id),
        )
        extr_run_id = (await cur.fetchone())[0]

        # 3. Seed claim
        cur = await conn.execute(
            """
            INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at)
            VALUES (%s, %s, %s, 'Тариф на проезд составит 30 рублей', 'тариф на проезд составит 30 рублей', %s)
            RETURNING id
            """,
            (extr_run_id, rev_id, edition.id, _NOW),
        )
        claim_id = (await cur.fetchone())[0]

        # 4. Seed story
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Изменение стоимости проезда в общественном транспорте', 'h-s1', %s)
            RETURNING id
            """,
            (story_id, _NOW),
        )
        s_rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (s_rev_id, story_id)
        )

        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, claim_id, _NOW),
        )

        # 5. Snapshot & Select
        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-adapter-build",
        )
        await snap_service.seal_candidates(run.id)
        inputs = await sel_service.select(run.id)
        assert len(inputs) == 1

        # 6. Adapter build
        frozen = await adapter.build(run.id)
        assert len(frozen.analysis.cards) == 1
        card = frozen.analysis.cards[0]
        assert card.id == f"story-{story_id}"
        assert card.summary == "Изменение стоимости проезда в общественном транспорте"
        assert len(card.all_source_refs()) == 1
        assert "telegram:101" in card.all_source_refs()
        assert len(frozen.writer_bundle.records) == 1
        assert "telegram:101" in frozen.writer_bundle.records

    async def test_database_attempt_observer_records_attempts(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-obs-record",
        )

        observer = DatabaseGenerationAttemptObserver(uow=uow, run_id=run.id)

        att1_id = await observer.attempt_started("writer", provider="gemini", model="gemini-2.5")
        assert att1_id > 0
        await observer.attempt_finished(att1_id, "failed", error_kind="TimeoutError")

        att2_id = await observer.attempt_started("story_renderer_fallback")
        assert att2_id > att1_id
        await observer.attempt_finished(att2_id, "succeeded")

        assert len(observer.attempts) == 2
        assert observer.attempts[0].kind == "writer"
        assert observer.attempts[1].kind == "story_renderer_fallback"
        assert observer.last_successful_content_attempt is not None
        assert observer.last_successful_content_attempt.id == att2_id
