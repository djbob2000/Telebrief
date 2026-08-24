"""Tests for facebook.editorial_enabled switch and temporal publication semantics."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from src.config_loader import ArticleConfig, Config, FacebookConfig, Settings
from src.db.uow import DatabaseUnitOfWork
from src.publication.editorial_adapter import KnowledgeEditorialAdapter
from src.publication.policies import PublicationPolicyService
from src.publication.selection import (
    EditorialSelectionService,
    SelectionProposal,
)
from src.publication.snapshot import PublicationSnapshotService

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)
_PAST = _NOW - dt.timedelta(hours=2)


class SimpleSelectionModel:
    def __init__(self, decision: str = "INCLUDE", intent: str = "normal") -> None:
        self.decision = decision
        self.intent = intent

    async def select_stories(self, *, run, candidates):
        return [
            SelectionProposal(
                story_id=c.story_id,
                story_revision_id=c.story_revision_id,
                decision=self.decision,
                presentation_intent=self.intent,
                confidence=0.95,
                reason="Selected by test model",
                rank=idx,
            )
            for idx, c in enumerate(candidates, start=1)
        ]


@pytest.mark.postgres
class TestFacebookEditorialEnabled:
    """Regression suite for facebook.editorial_enabled policy and point-in-time lifecycle."""

    async def _setup_sources_and_claims(
        self, conn: psycopg.AsyncConnection, edition_id: int
    ) -> dict[str, int]:
        # 1. Telegram source
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role, enabled)
            VALUES ('telegram', 'telegram_channel', '@berdyansk_city', 'https://t.me/berdyansk_city', 'Бердянск Горсовет', 'official', true)
            RETURNING id
            """
        )
        tg_src_id = (await cur.fetchone())[0]

        # 2. Facebook source
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role, enabled)
            VALUES ('facebook', 'facebook_group', 'fb_group_123', 'https://www.facebook.com/groups/berdyansk', 'Бердянск FB', 'community', true)
            RETURNING id
            """
        )
        fb_src_id = (await cur.fetchone())[0]

        # Source items
        cur = await conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, first_collected_at, published_at)
            VALUES (%s, 'msg', 'msg:101', %s, %s) RETURNING id
            """,
            (tg_src_id, _PAST, _PAST),
        )
        tg_item_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, first_collected_at, published_at)
            VALUES (%s, 'facebook_post', 'post:999', %s, %s) RETURNING id
            """,
            (fb_src_id, _PAST, _PAST),
        )
        fb_item_id = (await cur.fetchone())[0]

        # Source item revisions
        cur = await conn.execute(
            """
            INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at)
            VALUES (%s, 1, 'hash-tg-1', 'Телеграм новость: ремонт водопровода', %s) RETURNING id
            """,
            (tg_item_id, _PAST),
        )
        tg_sir_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at)
            VALUES (%s, 1, 'hash-fb-1', 'Фейсбук комментарий: на Восточном нет воды', %s) RETURNING id
            """,
            (fb_item_id, _PAST),
        )
        fb_sir_id = (await cur.fetchone())[0]

        # Relevance & extraction policies / runs
        cur = await conn.execute(
            """
            INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version)
            VALUES (%s, 1, 'h-rel', 'v-rel') RETURNING id
            """,
            (edition_id,),
        )
        rel_pol_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason)
            VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id
            """,
            (tg_sir_id, edition_id, rel_pol_id),
        )
        tg_rel_dec_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason)
            VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id
            """,
            (fb_sir_id, edition_id, rel_pol_id),
        )
        fb_rel_dec_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version)
            VALUES (%s, 1, 'h-extr', 'v-extr') RETURNING id
            """,
            (edition_id,),
        )
        extr_pol_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status)
            VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id
            """,
            (tg_sir_id, edition_id, extr_pol_id, tg_rel_dec_id),
        )
        tg_extr_run_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status)
            VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id
            """,
            (fb_sir_id, edition_id, extr_pol_id, fb_rel_dec_id),
        )
        fb_extr_run_id = (await cur.fetchone())[0]

        # Claims
        cur = await conn.execute(
            """
            INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at)
            VALUES (%s, %s, %s, 'Ремонт водопровода начат', 'Ремонт водопровода начат', %s) RETURNING id
            """,
            (tg_extr_run_id, tg_sir_id, edition_id, _PAST),
        )
        tg_claim_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at)
            VALUES (%s, %s, %s, 'На Восточном отключили воду', 'На Восточном отключили воду', %s) RETURNING id
            """,
            (fb_extr_run_id, fb_sir_id, edition_id, _PAST),
        )
        fb_claim_id = (await cur.fetchone())[0]

        return {
            "tg_src_id": tg_src_id,
            "fb_src_id": fb_src_id,
            "tg_sir_id": tg_sir_id,
            "fb_sir_id": fb_sir_id,
            "tg_claim_id": tg_claim_id,
            "fb_claim_id": fb_claim_id,
            "tg_extr_run_id": tg_extr_run_id,
            "fb_extr_run_id": fb_extr_run_id,
        }

    async def test_case_1_fb_only_story_is_not_candidate_when_editorial_disabled(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Case 1: editorial_enabled=false + FB-only Story -> not candidate."""
        ids = await self._setup_sources_and_claims(conn, edition.id)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _PAST),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Facebook-only новость', 'h-fb-only', %s) RETURNING id
            """,
            (story_id, _PAST),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )

        # Attach only Facebook claim
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, ids["fb_claim_id"], _PAST),
        )

        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cfg = Config(
            channels=[],
            settings=Settings(
                schedule_time="08:00",
                timezone="UTC",
                lookback_hours=24,
                openai_model="gpt-5-nano",
                openai_temperature=0.7,
            ),
            telegram_api_id=12345,
            telegram_api_hash="h",
            telegram_bot_token="t",
            openai_api_key="k",
            log_level="INFO",
            facebook=FacebookConfig(enabled=True, editorial_enabled=False),
        )

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            config=cfg,
            request_key="req-fb-only-disabled",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 0

    async def test_case_2_mixed_story_filters_facebook_claims_and_urls(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Case 2: Mixed TG+FB Story -> candidate remains, but no Facebook Claim/URL/text in selection/writer."""
        ids = await self._setup_sources_and_claims(conn, edition.id)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _PAST),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Ремонт водопровода начат. На Восточном отключили воду.', 'h-mixed-1', %s) RETURNING id
            """,
            (story_id, _PAST),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )

        # Attach BOTH Telegram and Facebook claims
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s), (%s, %s, %s)",
            (story_id, ids["tg_claim_id"], _PAST, story_id, ids["fb_claim_id"], _PAST),
        )

        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cfg = Config(
            channels=[],
            settings=Settings(
                schedule_time="08:00",
                timezone="UTC",
                lookback_hours=24,
                openai_model="gpt-5-nano",
                openai_temperature=0.7,
            ),
            telegram_api_id=12345,
            telegram_api_hash="h",
            telegram_bot_token="t",
            openai_api_key="k",
            log_level="INFO",
            facebook=FacebookConfig(enabled=True, editorial_enabled=False),
        )

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            config=cfg,
            request_key="req-mixed-disabled",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.snapshot_features["claim_count"] == 1
        assert "ремонт водопровода" in cand.snapshot_features["semantic_text"].lower()
        assert "восточном" not in cand.snapshot_features["semantic_text"].lower()

        # Run selection
        sel_service = EditorialSelectionService(uow=uow, model=SimpleSelectionModel())
        inputs = await sel_service.select(run.id)
        assert len(inputs) == 1
        inp = inputs[0]
        assert inp.claim_ids == [ids["tg_claim_id"]]
        assert ids["fb_claim_id"] not in inp.claim_ids

        # Build editorial input for writer
        adapter = KnowledgeEditorialAdapter(uow=uow)
        editorial_input = await adapter.build(run.id)
        assert len(editorial_input.analysis.cards) == 1
        card = editorial_input.analysis.cards[0]
        assert "ремонт водопровода" in card.summary.lower()
        assert "восточном" not in card.summary.lower()
        assert "отключили" not in card.summary.lower()
        assert "facebook.com" not in editorial_input.writer_bundle.prompt_text
        for r in editorial_input.writer_bundle.records.values():
            assert "facebook" not in r.ref.lower()

    async def test_case_3_editorial_enabled_true_includes_both_sources(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Case 3: editorial_enabled=true -> multisource behavior intact."""
        ids = await self._setup_sources_and_claims(conn, edition.id)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _PAST),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Смешанная новость', 'h-mixed-2', %s) RETURNING id
            """,
            (story_id, _PAST),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )

        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s), (%s, %s, %s)",
            (story_id, ids["tg_claim_id"], _PAST, story_id, ids["fb_claim_id"], _PAST),
        )

        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cfg = Config(
            channels=[],
            settings=Settings(
                schedule_time="08:00",
                timezone="UTC",
                lookback_hours=24,
                openai_model="gpt-5-nano",
                openai_temperature=0.7,
            ),
            telegram_api_id=12345,
            telegram_api_hash="h",
            telegram_bot_token="t",
            openai_api_key="k",
            log_level="INFO",
            facebook=FacebookConfig(enabled=True, editorial_enabled=True),
        )

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            config=cfg,
            request_key="req-mixed-enabled",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 1
        assert candidates[0].snapshot_features["claim_count"] == 2

        sel_service = EditorialSelectionService(uow=uow, model=SimpleSelectionModel())
        inputs = await sel_service.select(run.id)
        assert len(inputs) == 1
        assert sorted(inputs[0].claim_ids) == sorted([ids["tg_claim_id"], ids["fb_claim_id"]])

    async def test_case_4_switching_switch_freezes_old_and_excludes_in_new(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Case 4: Switching true -> false does not alter frozen run, but next run excludes FB."""
        ids = await self._setup_sources_and_claims(conn, edition.id)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _PAST),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Смешанная новость', 'h-mixed-switch', %s) RETURNING id
            """,
            (story_id, _PAST),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )

        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s), (%s, %s, %s)",
            (story_id, ids["tg_claim_id"], _PAST, story_id, ids["fb_claim_id"], _PAST),
        )

        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        # 1. Run 1 created with editorial_enabled=True
        cfg_enabled = Config(
            channels=[],
            settings=Settings(
                schedule_time="08:00",
                timezone="UTC",
                lookback_hours=24,
                openai_model="gpt-5-nano",
                openai_temperature=0.7,
            ),
            telegram_api_id=12345,
            telegram_api_hash="h",
            telegram_bot_token="t",
            openai_api_key="k",
            log_level="INFO",
            facebook=FacebookConfig(enabled=True, editorial_enabled=True),
        )
        run1 = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            config=cfg_enabled,
            request_key="req-switch-1",
        )
        candidates1 = await snap_service.seal_candidates(run1.id)
        assert candidates1[0].snapshot_features["claim_count"] == 2

        # 2. Run 2 created after switch to editorial_enabled=False
        cfg_disabled = Config(
            channels=[],
            settings=Settings(
                schedule_time="08:00",
                timezone="UTC",
                lookback_hours=24,
                openai_model="gpt-5-nano",
                openai_temperature=0.7,
            ),
            telegram_api_id=12345,
            telegram_api_hash="h",
            telegram_bot_token="t",
            openai_api_key="k",
            log_level="INFO",
            facebook=FacebookConfig(enabled=True, editorial_enabled=False),
        )
        run2 = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            config=cfg_disabled,
            request_key="req-switch-2",
        )
        candidates2 = await snap_service.seal_candidates(run2.id)
        assert candidates2[0].snapshot_features["claim_count"] == 1

        # Confirm Run 1 candidates remain untouched
        sealed1 = await snap_service.repo.load_sealed_candidates(conn, run1.id)
        assert sealed1[0].snapshot_features["claim_count"] == 2

    async def test_case_5_point_in_time_historical_lifecycle_state(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Important 9: Candidate query evaluates historical lifecycle state at snapshot_at."""
        ids = await self._setup_sources_and_claims(conn, edition.id)

        # Story created in past
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'archived', %s) RETURNING id",
            (edition.id, _PAST),
        )
        story_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Активная на момент среза новость', 'h-hist-1', %s) RETURNING id
            """,
            (story_id, _PAST),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, ids["tg_claim_id"], _PAST),
        )

        # State event in past: active
        await conn.execute(
            """
            INSERT INTO story_state_events (story_id, type, reason, observed_at, created_at)
            VALUES (%s, 'active', 'Created active', %s, %s)
            """,
            (story_id, _PAST, _PAST),
        )

        # State event in future (after _NOW): archived
        after_now = _NOW + dt.timedelta(minutes=30)
        await conn.execute(
            """
            INSERT INTO story_state_events (story_id, type, reason, observed_at, created_at)
            VALUES (%s, 'archived', 'Archived later', %s, %s)
            """,
            (story_id, after_now, after_now),
        )

        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        # Run at snapshot_at=_NOW sees the story as active because archiving happened after snapshot_at
        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            request_key="req-hist-lifecycle",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 1
        assert candidates[0].story_id == story_id

    async def test_case_6_article_lookback_hours_policy(
        self, conn: psycopg.AsyncConnection, edition
    ):
        """Important 8: ensure_current uses settings.article.lookback_hours for daily_article."""
        policy_service = PublicationPolicyService()

        cfg = Config(
            channels=[],
            settings=Settings(
                schedule_time="08:00",
                timezone="UTC",
                lookback_hours=24,
                openai_model="gpt-5-nano",
                openai_temperature=0.7,
                article=ArticleConfig(enabled=True, lookback_hours=72),
            ),
            telegram_api_id=12345,
            telegram_api_hash="h",
            telegram_bot_token="t",
            openai_api_key="k",
            log_level="INFO",
        )

        policies = await policy_service.ensure_current(
            conn,
            edition_id=edition.id,
            publication_type="daily_article",
            config=cfg,
        )
        cur = await conn.execute(
            "SELECT config->>'lookback_hours' FROM eligibility_policy_versions WHERE id = %s",
            (policies.eligibility.id,),
        )
        lookback = int((await cur.fetchone())[0])
        assert lookback == 72

    async def test_case_7_revision_only_activity_from_excluded_platform_does_not_make_old_story_candidate(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Important 5: Old story with past TG claim + recent FB claim revision -> NOT candidate when FB excluded."""
        old_time = _NOW - dt.timedelta(hours=48)
        ids = await self._setup_sources_and_claims(conn, edition.id)

        # 1. Story created 48h ago
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, old_time),
        )
        story_id = (await cur.fetchone())[0]

        # 2. Revision 1 created 48h ago with TG claim
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Старая телеграм новость', 'h-old-1', %s) RETURNING id
            """,
            (story_id, old_time),
        )
        rev1_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev1_id, story_id)
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, ids["tg_claim_id"], old_time),
        )

        # 3. Revision 2 created at _PAST (recent, within 24h) triggered by FB claim
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 2, 'open', 'Обновление из Фейсбука', 'h-fb-2', %s) RETURNING id
            """,
            (story_id, _PAST),
        )
        rev2_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev2_id, story_id)
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, ids["fb_claim_id"], _PAST),
        )

        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cfg = Config(
            channels=[],
            settings=Settings(
                schedule_time="08:00",
                timezone="UTC",
                lookback_hours=24,
                openai_model="gpt-5-nano",
                openai_temperature=0.7,
            ),
            telegram_api_id=12345,
            telegram_api_hash="h",
            telegram_bot_token="t",
            openai_api_key="k",
            log_level="INFO",
            facebook=FacebookConfig(enabled=True, editorial_enabled=False),
        )

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            config=cfg,
            request_key="req-fb-excluded-revision-activity",
        )
        candidates = await snap_service.seal_candidates(run.id)
        # Because TG claim is 48h old (outside 24h lookback) and FB is excluded,
        # FB revision activity must NOT make this story a candidate!
        assert len(candidates) == 0
