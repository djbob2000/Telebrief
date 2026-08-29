"""Tests for Publication generation attempts and publication constraints (Plan 4 Task 1)."""

import datetime as dt

import psycopg
import pytest

from src.db.uow import DatabaseUnitOfWork
from src.publication.generation import PublicationGenerationService
from src.publication.repository import (
    PublicationPolicyRepository,
    PublicationRepository,
)
from src.publication.selection import EditorialSelectionService
from src.publication.snapshot import PublicationSnapshotService

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


@pytest.mark.postgres
class TestPublicationGenerationConstraints:
    """Tests DB constraints for generation attempts and publications."""

    async def test_winning_attempt_belongs_to_same_publication_run(
        self, conn: psycopg.AsyncConnection, edition
    ):
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        run1 = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-key-run1",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        run2 = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-key-run2",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )

        attempt_in_run2 = await repo.insert_generation_attempt(
            conn,
            run_id=run2.id,
            attempt_no=1,
            kind="writer",
            provider="mock",
        )

        # Attempting to create Publication in run1 with an attempt from run2 must fail FK
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await repo.create_publication(
                conn,
                run_id=run1.id,
                winning_attempt_id=attempt_in_run2.id,
                publication_type="article",
                title="Заголовок",
                lead="Лид",
                body="Текст статьи",
            )

    async def test_publication_body_cannot_be_null(self, conn: psycopg.AsyncConnection, edition):
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        run = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-key-not-null-body",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        attempt = await repo.insert_generation_attempt(
            conn,
            run_id=run.id,
            attempt_no=1,
            kind="writer",
            provider="mock",
        )

        with pytest.raises(psycopg.errors.NotNullViolation):
            await conn.execute(
                """
                INSERT INTO publications (
                    publication_run_id, winning_generation_attempt_id,
                    publication_type, title, lead, body
                ) VALUES (%s, %s, %s, %s, %s, NULL)
                """,
                (run.id, attempt.id, "article", "Title", "Lead"),
            )


@pytest.mark.postgres
class TestPublicationGenerationService:
    """Tests PublicationGenerationService with real database, adapters, and mock generators."""

    async def test_generation_service_produces_publication_and_records_winning_attempt(
        self, conn: psycopg.AsyncConnection, pool, edition, monkeypatch
    ):
        from src.config_loader import Config
        from src.db.uow import DatabaseUnitOfWork
        from src.publication.generation import PublicationGenerationService
        from src.publication.selection import EditorialSelectionService, HeuristicSelectionModel
        from src.publication.snapshot import PublicationSnapshotService

        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)
        sel_service = EditorialSelectionService(uow=uow, model=HeuristicSelectionModel())

        # 1. Seed source, source_item, revision
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name)
            VALUES ('telegram', 'channel', '-100555', 'https://t.me/b_news', 'Бердянск Новости') RETURNING id
            """
        )
        source_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', '501', %s) RETURNING id",
            (source_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h-501', 'Реконструкция набережной завершена', %s) RETURNING id",
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

        cur = await conn.execute(
            """
            INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at)
            VALUES (%s, %s, %s, 'На набережной открыли новую пешеходную зону', 'на набережной открыли новую пешеходную зону', %s)
            RETURNING id
            """,
            (extr_run_id, rev_id, edition.id, _NOW),
        )
        claim_id = (await cur.fetchone())[0]

        # 3. Seed story
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Открытие набережной после ремонта', 'h-s501', %s)
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

        # 4. Snapshot & Select
        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-gen-winning-attempt",
        )
        await snap_service.seal_candidates(run.id)
        await sel_service.select(run.id)

        # 5. Mock generator
        class MockArticleGenerator:
            async def generate_from_frozen_input(self, frozen, attempt_observer=None):
                if attempt_observer is not None:
                    att_id = await attempt_observer.attempt_started(
                        "writer", provider="mock", model="mock-1"
                    )
                    await attempt_observer.attempt_finished(att_id, "succeeded")
                return (
                    "В Бердянске завершили реконструкцию набережной",
                    "Лид новости",
                    "Полный текст статьи об открытии пешеходной зоны.",
                )

        from src.config_loader import Settings

        settings = Settings(
            schedule_time="09:00",
            timezone="UTC",
            lookback_hours=24,
            openai_model="gpt-4",
            openai_temperature=0.7,
            ai_provider="mock",
        )
        config = Config(
            channels=[],
            settings=settings,
            telegram_api_id=1,
            telegram_api_hash="hash",
            telegram_bot_token="token",
            openai_api_key="key",
            log_level="INFO",
        )

        service = PublicationGenerationService(
            uow=uow,
            config=config,
            generator=MockArticleGenerator(),
        )

        pub = await service.generate(run.id)
        assert pub.publication_run_id == run.id
        assert pub.title == "В Бердянске завершили реконструкцию набережной"
        assert pub.body == "Полный текст статьи об открытии пешеходной зоны."
        assert pub.winning_generation_attempt_id > 0

        # Check run status transitioned to succeeded
        run_after = await PublicationRepository().get_run_by_id(conn, run.id)
        assert run_after.status == "succeeded"
        assert run_after.completed_at is not None

        # 6. Idempotent re-run returns same publication
        pub_again = await service.generate(run.id)
        assert pub_again.id == pub.id

    async def test_digest_generation_runs_editorializer_and_renders_custom_topics(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)
        sel_service = EditorialSelectionService(uow=uow)
        from tests.publication.conftest import seed_claim_for_story

        # 1. Seed story with claim
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Сюжет о воде', 'h-s1', %s) RETURNING id
            """,
            (story_id, _NOW),
        )
        story_rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s",
            (story_rev_id, story_id),
        )
        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        # 2. Snapshot & Select
        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            request_key="test-digest-editorializer",
        )
        await snap_service.seal_candidates(run.id)
        await sel_service.select(run.id)

        # 3. Mock DigestEditorializer
        from dataclasses import replace

        class MockEditorializer:
            async def editorialize(self, *, cards, bundle, attempt_observer=None):
                if attempt_observer is not None:
                    att_id = await attempt_observer.attempt_started(
                        "writer",
                        provider="mock",
                        model="mock-1",
                        metadata={"subkind": "digest_editorializer"},
                    )
                    await attempt_observer.attempt_finished(att_id, "succeeded")
                return [
                    replace(
                        c,
                        topic="Ремонт водовода на Восточном",
                        category="utilities",
                        summary="Водоснабжение временно приостановлено.",
                    )
                    for c in cards
                ]

        from src.config_loader import Config, Settings

        settings = Settings(
            schedule_time="09:00",
            timezone="UTC",
            lookback_hours=24,
            openai_model="gpt-4",
            openai_temperature=0.7,
            ai_provider="mock",
        )
        config = Config(
            channels=[],
            settings=settings,
            telegram_api_id=1,
            telegram_api_hash="hash",
            telegram_bot_token="token",
            openai_api_key="key",
            log_level="INFO",
        )

        class MockGenerator:
            async def generate_from_frozen_input(self, frozen, attempt_observer=None):
                return ("Title", "Lead", "Body")

        service = PublicationGenerationService(
            uow=uow,
            config=config,
            generator=MockGenerator(),
            editorializer=MockEditorializer(),
        )

        pub = await service.generate(run.id)
        assert pub.publication_run_id == run.id
        assert "Ремонт водовода на Восточном" in pub.body
        assert "Водоснабжение временно приостановлено." in pub.body

    async def test_digest_generation_falls_back_gracefully_when_editorializer_fails(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)
        sel_service = EditorialSelectionService(uow=uow)
        from tests.publication.conftest import seed_claim_for_story

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Сюжет о воде', 'h-s2', %s) RETURNING id
            """,
            (story_id, _NOW),
        )
        story_rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s",
            (story_rev_id, story_id),
        )
        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            request_key="test-digest-editorializer-fail",
        )
        await snap_service.seal_candidates(run.id)
        await sel_service.select(run.id)

        from src.publication.editorializer import EditorializationError

        class FailingEditorializer:
            async def editorialize(self, *, cards, bundle, attempt_observer=None):
                raise EditorializationError("Simulated LLM outage")

        from src.config_loader import Config, Settings

        settings = Settings(
            schedule_time="09:00",
            timezone="UTC",
            lookback_hours=24,
            openai_model="gpt-4",
            openai_temperature=0.7,
            ai_provider="mock",
        )
        config = Config(
            channels=[],
            settings=settings,
            telegram_api_id=1,
            telegram_api_hash="hash",
            telegram_bot_token="token",
            openai_api_key="key",
            log_level="INFO",
        )

        class MockGenerator:
            async def generate_from_frozen_input(self, frozen, attempt_observer=None):
                return ("Title", "Lead", "Body")

        service = PublicationGenerationService(
            uow=uow,
            config=config,
            generator=MockGenerator(),
            editorializer=FailingEditorializer(),
        )

        # Must not raise: falls back to canonical cards and renders successfully
        pub = await service.generate(run.id)
        assert pub.publication_run_id == run.id
        assert pub.body is not None
        assert len(pub.body) > 0
