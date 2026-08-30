"""Tests for Publication generation attempts and publication constraints (Plan 4 Task 1)."""

import datetime as dt
import json

import psycopg
import pytest

from src.db.uow import DatabaseUnitOfWork
from src.publication.generation import PublicationGenerationService
from src.publication.models import PublicationSelectionDecision
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
        conn,
        edition_id=edition_id,
        config_hash="elig-hash-1",
        prompt_version="elig-v1",
        config={"lookback_hours": 24},
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
        from src.publication.selection import HeuristicSelectionModel

        sel_service = EditorialSelectionService(uow=uow, model=HeuristicSelectionModel())

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

    async def test_event_first_digest_generation(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        import json

        from src.publication.models import PublicationSelectionDecision
        from src.publication.repository import PublicationPolicyRepository, PublicationRepository

        uow = DatabaseUnitOfWork(pool)
        repo = PublicationRepository()
        policy_repo = PublicationPolicyRepository()

        elig = await policy_repo.get_or_create_eligibility_policy(
            conn, edition_id=edition.id, config_hash="h-e", prompt_version="v1"
        )
        sel = await policy_repo.get_or_create_selection_policy(
            conn, edition_id=edition.id, config_hash="h-s", prompt_version="v1"
        )
        wri = await policy_repo.get_or_create_writer_policy(
            conn, edition_id=edition.id, config_hash="h-w", prompt_version="v1"
        )

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at) VALUES (%s, 'active', 'event_first', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]

        event_payload = {
            "topic": "Городской транспорт",
            "category": "transport",
            "urgency": "medium",
            "publishability": "news",
            "headline": "Новые автобусы на маршрутах города",
            "digest_summary": "На линии вышли 5 новых автобусов среднего класса.",
            "key_facts": ["Маршрут №4 и №10 получили пополнение"],
            "official_positions": [],
            "community_observations": [],
            "conflicts_or_uncertainties": [],
            "affected_areas": ["Центр", "Колония"],
            "timeline_summary": "",
            "confidence_score": 0.98,
            "representative_fragment_ids": [],
        }

        cur = await conn.execute(
            """
            INSERT INTO story_revisions (
                story_id, revision_no, current_state, semantic_text, content_hash,
                title, summary, event_payload, created_at
            ) VALUES (%s, 1, 'open', %s, 'h-ev-t1', %s, %s, %s, %s)
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

        run = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="digest_grouped",
            request_key="test-event-gen-grouped",
            snapshot_at=_NOW,
            policy_ids=(elig.id, sel.id, wri.id),
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
                confidence=0.98,
                reason="Good news",
                rank=1,
                metadata={},
                created_at=_NOW,
            ),
        )

        await repo.freeze_selected_input(
            conn,
            run.id,
            story_id=story_id,
            story_revision_id=rev_id,
            selection_decision_id=dec.id,
            presentation_intent="lead",
            rank=1,
            fragment_ids=[],
        )
        await repo.transition_run(conn, run.id, "selected_inputs_sealed")

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
            uow=uow, config=config, repo=repo, generator=MockGenerator()
        )
        pub = await service.generate(run.id, defer_delivery=False)
        assert pub.publication_run_id == run.id
        assert "Новые автобусы" in (pub.body or "") or "Новые автобусы" in (pub.title or "")

    async def test_event_first_digest_generation_bounded_ai_call_budget(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Event-first digest generation is deterministic and consumes <= 1 chat completion call."""
        uow = DatabaseUnitOfWork(pool)
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        # 1. Create story & source & fragment
        cur = await conn.execute(
            """
            INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
            VALUES (%s, 'active', 'event_first', %s)
            RETURNING id
            """,
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-10099', 'https://t.me/e', 'E') RETURNING id"
        )
        src_id = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
            (src_id, edition.id),
        )
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', 'm-bud', %s) RETURNING id",
            (src_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h-bud', 'Текст') RETURNING id",
            (item_id,),
        )
        sir_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'Текст', 'h-fbud', 'v1', TRUE, %s) RETURNING id",
            (sir_id, _NOW),
        )
        frag_id = (await cur.fetchone())[0]

        event_payload = {
            "topic": "Новые автобусы",
            "headline": "Новые автобусы вышли на маршруты",
            "digest_summary": "Парк пополнился 10 новыми автобусами.",
            "category": "transport",
            "evidence_items": [
                {
                    "text": "10 новых автобусов вышли на маршруты",
                    "kind": "established_fact",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [frag_id],
                }
            ],
            "tags": ["транспорт", "автобусы"],
        }
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (
                story_id, revision_no, current_state, semantic_text, content_hash,
                title, summary, event_payload, created_at
            ) VALUES (%s, 1, 'open', %s, 'h-ev-gen-bud', %s, %s, %s, %s)
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

        run = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="digest_grouped",
            request_key="test-event-gen-budget",
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
                confidence=0.98,
                reason="Good news",
                rank=1,
                metadata={},
                created_at=_NOW,
            ),
        )
        await repo.freeze_selected_input(
            conn,
            run.id,
            story_id=story_id,
            story_revision_id=rev_id,
            selection_decision_id=dec.id,
            presentation_intent="lead",
            rank=1,
            fragment_ids=[frag_id],
        )
        await repo.transition_run(conn, run.id, "selected_inputs_sealed")

        from unittest.mock import AsyncMock

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

        mock_generator = AsyncMock()
        mock_editorializer = AsyncMock()

        service = PublicationGenerationService(
            uow=uow,
            config=config,
            repo=repo,
            generator=mock_generator,
            editorializer=mock_editorializer,
        )

        pub = await service.generate(run.id, defer_delivery=False)
        assert pub.publication_run_id == run.id
        assert pub.body is not None
        assert "Новые автобусы" in pub.body

        # Assert zero extra chat calls were spent on editorializer or generator
        assert mock_editorializer.editorialize.call_count == 0
        assert mock_generator.generate_from_frozen_input.call_count == 0

    async def test_generation_service_article_enforces_bounded_ai_budget(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Article generation consumes exactly 1 writer call and records winning attempt."""
        uow = DatabaseUnitOfWork(pool)
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        # Create story & fragment
        cur = await conn.execute(
            """
            INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
            VALUES (%s, 'active', 'event_first', %s)
            RETURNING id
            """,
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-10099', 'https://t.me/art', 'Art') RETURNING id"
        )
        src_id = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
            (src_id, edition.id),
        )
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', 'm-art', %s) RETURNING id",
            (src_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h-art', 'Текст') RETURNING id",
            (item_id,),
        )
        sir_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'Текст', 'h-fart', 'v1', TRUE, %s) RETURNING id",
            (sir_id, _NOW),
        )
        frag_id = (await cur.fetchone())[0]

        event_payload = {
            "topic": "Капитальный ремонт",
            "headline": "Капитальный ремонт проспекта",
            "digest_summary": "Дорожники ведут укладку асфальта.",
            "category": "infrastructure",
            "evidence_items": [
                {
                    "text": "Уложено 2 километра нового полотна",
                    "kind": "established_fact",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [frag_id],
                }
            ],
            "tags": ["дороги", "ремонт"],
        }
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (
                story_id, revision_no, current_state, semantic_text, content_hash,
                title, summary, event_payload, created_at
            ) VALUES (%s, 1, 'open', %s, 'h-ev-art', %s, %s, %s, %s)
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

        run = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-event-art-budget",
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
                confidence=0.98,
                reason="Good news",
                rank=1,
                metadata={},
                created_at=_NOW,
            ),
        )
        await repo.freeze_selected_input(
            conn,
            run.id,
            story_id=story_id,
            story_revision_id=rev_id,
            selection_decision_id=dec.id,
            presentation_intent="lead",
            rank=1,
            fragment_ids=[frag_id],
        )
        await repo.transition_run(conn, run.id, "selected_inputs_sealed")

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

        class MockArticleGenerator:
            def __init__(self):
                self.calls = 0

            async def generate_from_frozen_input(self, frozen, attempt_observer=None):
                self.calls += 1
                if attempt_observer:
                    att_id = await attempt_observer.attempt_started("writer", provider="mock")
                    await attempt_observer.attempt_finished(att_id, "succeeded")
                return ("Заголовок статьи", "Лид статьи", "## Раздел\n\nТекст статьи.")

        mock_gen = MockArticleGenerator()
        service = PublicationGenerationService(
            uow=uow,
            config=config,
            repo=repo,
            generator=mock_gen,
        )

        pub = await service.generate(run.id, defer_delivery=False)
        assert pub.publication_run_id == run.id
        assert mock_gen.calls == 1
        assert pub.title == "Заголовок статьи"


@pytest.mark.postgres
async def test_event_first_article_validation_failure_fallback_attempt(conn, pool, edition):
    """When the 1 AI writer attempt returns unsupported claims, it records failed writer attempt

    and successful story_renderer_fallback attempt without making a 2nd AI call.
    """
    from unittest.mock import AsyncMock

    from src.article_generator import ArticleGenerator
    from src.config_loader import Config, PublicationEditorialConfig, Settings

    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_ids = await _seed_policies(conn, edition.id)

    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _NOW),
    )
    story_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-10042', 'https://t.me/res', 'РЭС', 'official')
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
        VALUES (%s, 'msg', 'm1', %s) RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h1', 'Авария на подстанции. Бригады ведут восстановительные работы.')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'Авария на подстанции. Бригады ведут восстановительные работы.', 'hf1', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Авария на подстанции",
        "headline": "Отключение света",
        "digest_summary": "Авария на подстанции в центре.",
        "evidence_items": [
            {
                "text": "Авария на подстанции в центре",
                "kind": "established_fact",
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
        ) VALUES (%s, 1, 'open', %s, 'h-rev-val', %s, %s, %s, %s)
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

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="article",
        request_key="test-val-fail-run",
        snapshot_at=_NOW,
        policy_ids=policy_ids,
    )
    cand = await repo.insert_candidate(
        conn, run.id, story_id=story_id, story_revision_id=rev_id, deterministic_rank=1
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
            confidence=0.98,
            reason="Good",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )
    await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        selection_decision_id=dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[frag_id],
    )
    await repo.transition_run(conn, run.id, "selected_inputs_sealed")

    settings = Settings(
        schedule_time="09:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4",
        openai_temperature=0.7,
        ai_provider="openai",
        publication_editorial=PublicationEditorialConfig(
            article_min_words=5,
            article_min_sections=1,
        ),
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

    import logging

    generator = ArticleGenerator(config=config, logger=logging.getLogger("test"))
    mock_provider = AsyncMock()
    # Return draft with invented duration "полтора часа"
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "title": "Отключение света в центре",
            "title_support_ids": [f"story:{story_id}:evidence:0:frag:{frag_id}"],
            "lead": "В центре города ликвидируют аварию на подстанции.",
            "lead_support_ids": [f"story:{story_id}:evidence:0:frag:{frag_id}"],
            "sections": [
                {
                    "heading": "Энергоснабжение",
                    "heading_support_ids": [f"story:{story_id}:evidence:0:frag:{frag_id}"],
                    "paragraphs": [
                        {
                            "text": "Бригады восстановили питание в течение полутора часов.",
                            "cited_support_ids": [f"story:{story_id}:evidence:0:frag:{frag_id}"],
                        }
                    ],
                }
            ],
        }
    )
    generator.provider = mock_provider

    service = PublicationGenerationService(
        uow=uow,
        config=config,
        repo=repo,
        generator=generator,
    )

    pub = await service.generate(run.id, defer_delivery=False)
    assert pub.publication_run_id == run.id
    # Assert provider was called exactly ONCE
    assert mock_provider.chat_completion.call_count == 1

    # Check generation attempts in DB
    cur = await conn.execute(
        """
        SELECT kind, status, error_kind, metadata
        FROM publication_generation_attempts
        WHERE publication_run_id = %s
        ORDER BY attempt_no ASC
        """,
        (run.id,),
    )
    attempts = await cur.fetchall()
    assert len(attempts) == 2
    # 1. writer failed with ValidationFailed
    assert attempts[0][0] == "writer"
    assert attempts[0][1] == "failed"
    assert attempts[0][2] == "ValidationFailed"
    # 2. story_renderer_fallback succeeded
    assert attempts[1][0] == "story_renderer_fallback"
    assert attempts[1][1] == "succeeded"

    # Winning attempt is story_renderer_fallback
    cur = await conn.execute(
        "SELECT kind FROM publication_generation_attempts WHERE id = %s",
        (pub.winning_generation_attempt_id,),
    )
    assert (await cur.fetchone())[0] == "story_renderer_fallback"
    # Ensure unsupported hallucinated phrase is NOT in publication
    assert "полутора часов" not in pub.body


@pytest.mark.postgres
async def test_event_first_article_successful_writer_records_claim_trace(conn, pool, edition):
    """When the AI writer attempt succeeds, it stores claim_trace in attempt metadata."""
    from unittest.mock import AsyncMock

    from src.article_generator import ArticleGenerator
    from src.config_loader import Config, PublicationEditorialConfig, Settings

    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_ids = await _seed_policies(conn, edition.id)

    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _NOW),
    )
    story_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-10043', 'https://t.me/res', 'РЭС', 'official')
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
        VALUES (%s, 'msg', 'm2', %s) RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h2', 'Авария на подстанции: временно обесточен центр города.')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'Авария на подстанции: временно обесточен центр города.', 'hf2', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Авария на подстанции",
        "headline": "Отключение света в центре",
        "digest_summary": "Авария на подстанции в центре.",
        "evidence_items": [
            {
                "text": "Авария на подстанции в центре",
                "kind": "established_fact",
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
        ) VALUES (%s, 1, 'open', %s, 'h-rev-trace', %s, %s, %s, %s)
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

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="article",
        request_key="test-trace-run",
        snapshot_at=_NOW,
        policy_ids=policy_ids,
    )
    cand = await repo.insert_candidate(
        conn, run.id, story_id=story_id, story_revision_id=rev_id, deterministic_rank=1
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
            confidence=0.98,
            reason="Good",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )
    await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        selection_decision_id=dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[frag_id],
    )
    await repo.transition_run(conn, run.id, "selected_inputs_sealed")

    settings = Settings(
        schedule_time="09:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4",
        openai_temperature=0.7,
        ai_provider="openai",
        publication_editorial=PublicationEditorialConfig(
            article_min_words=5,
            article_min_sections=1,
        ),
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

    import logging

    generator = ArticleGenerator(config=config, logger=logging.getLogger("test"))
    mock_provider = AsyncMock()
    sup_id = f"story:{story_id}:evidence:0:frag:{frag_id}"
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "title": "Авария на подстанции в центре",
            "title_support_ids": [sup_id],
            "title_claims": [
                {"text": "Авария на подстанции в центре", "cited_support_ids": [sup_id]}
            ],
            "lead": "Авария на подстанции в центре.",
            "lead_support_ids": [sup_id],
            "lead_claims": [
                {"text": "Авария на подстанции в центре", "cited_support_ids": [sup_id]}
            ],
            "sections": [
                {
                    "heading": "Авария на подстанции",
                    "heading_support_ids": [sup_id],
                    "heading_claims": [
                        {"text": "Авария на подстанции", "cited_support_ids": [sup_id]}
                    ],
                    "paragraphs": [
                        {
                            "text": "Авария на подстанции в центре.",
                            "cited_support_ids": [sup_id],
                            "claims": [
                                {
                                    "text": "Авария на подстанции в центре",
                                    "cited_support_ids": [sup_id],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    generator.provider = mock_provider

    service = PublicationGenerationService(
        uow=uow,
        config=config,
        repo=repo,
        generator=generator,
    )

    pub = await service.generate(run.id, defer_delivery=False)
    assert pub.publication_run_id == run.id
    assert mock_provider.chat_completion.call_count == 1

    cur = await conn.execute(
        """
        SELECT kind, status, metadata
        FROM publication_generation_attempts
        WHERE publication_run_id = %s
        """,
        (run.id,),
    )
    row = await cur.fetchone()
    assert row[0] == "writer"
    assert row[1] == "succeeded"
    meta = row[2]
    assert "claim_trace" in meta
    assert len(meta["claim_trace"]) >= 3
    assert meta["validation"]["is_valid"] is True
