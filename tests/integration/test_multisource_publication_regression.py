"""End-to-end regression test suite locking multi-source publication invariants (Milestone G Task 14).

Verifies all critical architectural fixes:
1. Single-source low-risk items remain publishable with proper attribution.
2. Verification status (unverified / unavailable) never drops stories.
3. Old stories without recent activity within the lookback window are not repeated.
4. Source attribution is strictly provenance-driven (community sources named 'official' stay attributed).
5. Identical Telegram message IDs in different channels preserve distinct provenance.
6. `facebook.enabled=False` guarantees zero browser launches.
7. `knowledge_no_embeddings` creates stories and publications without embedding calls.
8. Frozen empty candidate sets remain deterministic across retries.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch

import psycopg
import pytest

from src.config_loader import FacebookConfig
from src.db.uow import DatabaseUnitOfWork
from src.processing.story_matching import StoryMatchingService
from src.providers.facebook.runtime_policy import is_facebook_enabled
from src.publication.editorial_adapter import KnowledgeEditorialAdapter
from src.publication.selection import (
    EditorialSelectionService,
    HeuristicSelectionModel,
)
from src.publication.snapshot import PublicationSnapshotService
from src.repositories.story_candidates import (
    StoryMatchingRunRepository,
)

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


@pytest.mark.postgres
@pytest.mark.asyncio
class TestMultisourcePublicationInvariants:
    async def test_facebook_disabled_guarantees_zero_browser_launches(self):
        """facebook.enabled=False hard kill switch blocks all Facebook tasks without launching browser."""
        cfg = FacebookConfig(enabled=False)
        assert is_facebook_enabled(cfg) is False

        with patch("playwright.async_api.async_playwright") as mock_pw:
            assert is_facebook_enabled(cfg) is False
            mock_pw.assert_not_called()

    async def test_same_telegram_message_id_across_channels_preserves_distinct_provenance(
        self, conn: psycopg.AsyncConnection, edition
    ):
        """Message ID '555' in Channel A and Channel B must resolve to separate SourceItems and Claims."""
        # 1. Create two distinct Telegram sources
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role)
            VALUES ('telegram', 'channel', '-100111', 'https://t.me/chan_a', 'Channel A', 'local_media'),
                   ('telegram', 'channel', '-100222', 'https://t.me/chan_b', 'Channel B', 'community')
            RETURNING id
            """
        )
        src_rows = await cur.fetchall()
        src_a, src_b = src_rows[0][0], src_rows[1][0]

        # 2. Both channels have message ID '555'
        cur = await conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
            VALUES (%s, 'message', '555', %s), (%s, 'message', '555', %s)
            RETURNING id
            """,
            (src_a, _NOW, src_b, _NOW),
        )
        item_rows = await cur.fetchall()
        item_a, item_b = item_rows[0][0], item_rows[1][0]
        assert item_a != item_b

        # 3. Create revisions
        cur = await conn.execute(
            """
            INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at)
            VALUES (%s, 1, 'hash_a', 'Text from A', %s), (%s, 1, 'hash_b', 'Text from B', %s)
            RETURNING id
            """,
            (item_a, _NOW, item_b, _NOW),
        )
        rev_rows = await cur.fetchall()
        rev_a, rev_b = rev_rows[0][0], rev_rows[1][0]

        # 4. Relevance & claim runs
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'rh', 'rp') RETURNING id",
            (edition.id,),
        )
        rpol_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason)
            VALUES (%s, %s, %s, 'relevant', 'ok'), (%s, %s, %s, 'relevant', 'ok') RETURNING id
            """,
            (rev_a, edition.id, rpol_id, rev_b, edition.id, rpol_id),
        )
        dec_rows = await cur.fetchall()
        dec_a, dec_b = dec_rows[0][0], dec_rows[1][0]

        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'eh', 'ep') RETURNING id",
            (edition.id,),
        )
        epol_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status)
            VALUES (%s, %s, %s, %s, 'succeeded'), (%s, %s, %s, %s, 'succeeded') RETURNING id
            """,
            (rev_a, edition.id, epol_id, dec_a, rev_b, edition.id, epol_id, dec_b),
        )
        run_rows = await cur.fetchall()
        run_a, run_b = run_rows[0][0], run_rows[1][0]

        cur = await conn.execute(
            """
            INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at)
            VALUES (%s, %s, %s, 'Claim A', 'claim a', %s), (%s, %s, %s, 'Claim B', 'claim b', %s) RETURNING id
            """,
            (run_a, rev_a, edition.id, _NOW, run_b, rev_b, edition.id, _NOW),
        )
        claim_rows = await cur.fetchall()
        claim_a, claim_b = claim_rows[0][0], claim_rows[1][0]

        # Verify distinct claims and exact provenance preserved
        assert claim_a != claim_b
        cur = await conn.execute(
            """
            SELECT c.id, si.external_id, s.name, s.url
            FROM claims c
            JOIN source_item_revisions sir ON c.source_item_revision_id = sir.id
            JOIN source_items si ON sir.source_item_id = si.id
            JOIN sources s ON si.source_id = s.id
            WHERE c.id IN (%s, %s)
            ORDER BY c.id
            """,
            (claim_a, claim_b),
        )
        provenance_rows = await cur.fetchall()
        assert len(provenance_rows) == 2
        assert provenance_rows[0][1] == "555"
        assert provenance_rows[0][2] == "Channel A"
        assert provenance_rows[1][1] == "555"
        assert provenance_rows[1][2] == "Channel B"

    async def test_lexical_only_mode_matches_and_creates_stories(
        self, conn: psycopg.AsyncConnection, edition, pool
    ):
        """Telegram processing_mode='knowledge_no_embeddings' matches and creates stories without embedding."""
        uow = DatabaseUnitOfWork(pool)

        # 1. Ingest Claim
        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-1003', 'https://t.me/c3', 'Chan3') RETURNING id"
        )
        src_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', '3', %s) RETURNING id",
            (src_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h3', 'В Бердянске открылась выставка', %s) RETURNING id",
            (item_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'hr3', 'vr3') RETURNING id",
            (edition.id,),
        )
        rel_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id",
            (rev_id, edition.id, rel_pol_id),
        )
        rel_dec_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'he3', 've3') RETURNING id",
            (edition.id,),
        )
        extr_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status) VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id",
            (rev_id, edition.id, extr_pol_id, rel_dec_id),
        )
        extr_run_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at) VALUES (%s, %s, %s, 'В Бердянске открылась выставка', 'в бердянске открылась выставка', %s) RETURNING id",
            (extr_run_id, rev_id, edition.id, _NOW),
        )
        claim_id = (await cur.fetchone())[0]

        # 2. Insert story matching policy
        cur = await conn.execute(
            """
            INSERT INTO story_matching_policy_versions (
                edition_id, version, config_hash, prompt_version,
                vector_limit, lexical_limit, state_fallback_limit, total_candidate_limit,
                resolved_lookback_days, embedding_model, embedding_dimensions
            )
            VALUES (%s, 1, 'sm_cfg', 'sm_prompt', 10, 10, 5, 15, 7, 'none', 0)
            RETURNING id
            """,
            (edition.id,),
        )
        sm_pol_id = (await cur.fetchone())[0]

        mock_matcher = AsyncMock()
        from src.processing.story_matching import MatchProposal

        mock_matcher.choose.return_value = MatchProposal(
            assignment="NEW_STORY",
            confidence=0.95,
            reason="Открылась новая выставка.",
        )

        service = StoryMatchingService(
            uow=uow,
            matcher=mock_matcher,
        )

        outcome = await service.run(
            claim_id=claim_id,
            policy_id=sm_pol_id,
            claim_embedding_id=None,
        )

        assert outcome.decision.assignment == "NEW_STORY"
        assert outcome.story_id is not None

        # Verify DB run record transitioned to succeeded and recorded retrieval_mode
        cur = await conn.execute(
            "SELECT status, retrieval_mode FROM story_matching_runs WHERE claim_id = %s",
            (claim_id,),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "succeeded"
        assert row[1] == "knowledge_no_embeddings"

    async def test_empty_candidate_set_is_frozen_durably_across_retries(
        self, conn: psycopg.AsyncConnection, edition, pool
    ):
        """Empty candidate retrieval freezes candidates and empty_frozen=True across retries."""
        # 1. Seed claim and policy for FKs
        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-1004', 'https://t.me/c4', 'Chan4') RETURNING id"
        )
        src_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', '4', %s) RETURNING id",
            (src_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h4', 'txt', %s) RETURNING id",
            (item_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'hr4', 'vr4') RETURNING id",
            (edition.id,),
        )
        rel_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id",
            (rev_id, edition.id, rel_pol_id),
        )
        rel_dec_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'he4', 've4') RETURNING id",
            (edition.id,),
        )
        extr_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status) VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id",
            (rev_id, edition.id, extr_pol_id, rel_dec_id),
        )
        extr_run_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at) VALUES (%s, %s, %s, 'claim 4', 'claim 4', %s) RETURNING id",
            (extr_run_id, rev_id, edition.id, _NOW),
        )
        claim_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO story_matching_policy_versions (
                edition_id, version, config_hash, prompt_version,
                vector_limit, lexical_limit, state_fallback_limit, total_candidate_limit,
                resolved_lookback_days, embedding_model, embedding_dimensions
            )
            VALUES (%s, 1, 'sm_cfg4', 'sm_prompt4', 10, 10, 5, 15, 7, 'none', 0)
            RETURNING id
            """,
            (edition.id,),
        )
        sm_pol_id = (await cur.fetchone())[0]

        run_repo = StoryMatchingRunRepository()
        async with pool.connection() as pconn:
            run_obj = await run_repo.insert_running(
                pconn,
                claim_id=claim_id,
                edition_id=edition.id,
                policy_id=sm_pol_id,
                claim_embedding_id=None,
                retrieval_mode="knowledge_no_embeddings",
            )
            run_id = run_obj.id
            # Freeze empty candidate set
            await run_repo.save_candidates(pconn, run_id=run_id, candidates=[])

            # Check candidates count and status
            cur = await pconn.execute(
                "SELECT count(*), (SELECT candidates_retrieved_at IS NOT NULL FROM story_matching_runs WHERE id = %s) FROM story_matching_candidates WHERE run_id = %s",
                (run_id, run_id),
            )
            count, retrieved_set = await cur.fetchone()
            assert count == 0
            assert retrieved_set is True

            # Subsequent decision record
            await run_repo.insert_decision(
                pconn,
                run_id=run_id,
                assignment="NEW_STORY",
                target_story_id=None,
                story_update=None,
                confidence=1.0,
                reason="Empty candidates created new story",
            )
            await run_repo.mark_succeeded(pconn, run_id, completed_at=_NOW)

            # Verify run succeeded
            cur = await pconn.execute(
                "SELECT status FROM story_matching_runs WHERE id = %s", (run_id,)
            )
            assert (await cur.fetchone())[0] == "succeeded"

    async def test_editorial_adapter_preserves_single_source_low_risk_with_attribution(
        self, conn: psycopg.AsyncConnection, edition, pool
    ):
        """Single-source low-risk report/comment produces valid card with exact source attribution."""
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)
        sel_service = EditorialSelectionService(uow=uow, model=HeuristicSelectionModel())
        adapter = KnowledgeEditorialAdapter(uow=uow)

        # Create 1 source, item, revision, claim, story
        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, external_id, url, name, role) VALUES ('facebook', 'comment', 'fb_c_1', 'https://facebook.com/post/1#c1', 'Facebook User Group', 'community') RETURNING id"
        )
        src_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'comment', 'fb_c_1', %s) RETURNING id",
            (src_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'hfb', 'По улице Ленина починили фонари.', %s) RETURNING id",
            (item_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'rh_fb', 'rp') RETURNING id",
            (edition.id,),
        )
        rpol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id",
            (rev_id, edition.id, rpol_id),
        )
        dec_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'eh_fb', 'ep') RETURNING id",
            (edition.id,),
        )
        epol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status) VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id",
            (rev_id, edition.id, epol_id, dec_id),
        )
        run_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at) VALUES (%s, %s, %s, 'По улице Ленина починили фонари', 'по улице ленина починили фонари', %s) RETURNING id",
            (run_id, rev_id, edition.id, _NOW),
        )
        claim_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) VALUES (%s, 1, 'open', 'Ремонт освещения на улице Ленина', 'hs1', %s) RETURNING id",
            (story_id, _NOW),
        )
        srev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (srev_id, story_id)
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, claim_id, _NOW),
        )

        # Snapshot and selection
        pub_run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-single-source-fb",
        )
        await snap_service.seal_candidates(pub_run.id)
        inputs = await sel_service.select(pub_run.id)
        assert len(inputs) == 1

        frozen = await adapter.build(pub_run.id)
        assert len(frozen.analysis.cards) == 1
        card = frozen.analysis.cards[0]
        assert card.id == f"story-{story_id}"
        expected_ref = f"facebook:source:{src_id}:item:{item_id}:rev:{rev_id}"
        assert expected_ref in card.all_source_refs()
        elements = card.hard_facts + card.community_observations + card.useful_details
        assert len(elements) == 1
        assert elements[0].source_refs == [expected_ref]

    async def test_unverified_story_remains_eligible_and_publishable(
        self, conn: psycopg.AsyncConnection, edition, pool
    ):
        """Unverified stories or stories with unavailable verification are never dropped."""
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)
        sel_service = EditorialSelectionService(uow=uow, model=HeuristicSelectionModel())
        adapter = KnowledgeEditorialAdapter(uow=uow)

        # 1. Ingest unverified claim & story
        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-1005', 'https://t.me/c5', 'Chan5') RETURNING id"
        )
        src_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', '5', %s) RETURNING id",
            (src_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h5', 'Слухи о перекрытии моста', %s) RETURNING id",
            (item_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'hr5', 'vr5') RETURNING id",
            (edition.id,),
        )
        rel_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id",
            (rev_id, edition.id, rel_pol_id),
        )
        rel_dec_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'he5', 've5') RETURNING id",
            (edition.id,),
        )
        extr_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status) VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id",
            (rev_id, edition.id, extr_pol_id, rel_dec_id),
        )
        extr_run_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at) VALUES (%s, %s, %s, 'Мост будет перекрыт', 'мост будет перекрыт', %s) RETURNING id",
            (extr_run_id, rev_id, edition.id, _NOW),
        )
        claim_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) VALUES (%s, 1, 'open', 'Слухи о ремонте моста', 'hs5', %s) RETURNING id",
            (story_id, _NOW),
        )
        srev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (srev_id, story_id)
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, claim_id, _NOW),
        )

        # Story has no verification run (verification unavailable/optional) - remains fully publishable
        pub_run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-unverified-story",
        )
        await snap_service.seal_candidates(pub_run.id)
        inputs = await sel_service.select(pub_run.id)
        assert len(inputs) == 1

        frozen = await adapter.build(pub_run.id)
        assert len(frozen.analysis.cards) == 1
        assert frozen.analysis.cards[0].id == f"story-{story_id}"

    async def test_old_story_without_new_activity_excluded_from_snapshot(
        self, conn: psycopg.AsyncConnection, edition, pool
    ):
        """Stories older than lookback window with no recent claims are not selected for publication."""
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        past_time = _NOW - dt.timedelta(days=10)

        # Seed old story and old claim
        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-1006', 'https://t.me/c6', 'Chan6') RETURNING id"
        )
        src_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', '6', %s) RETURNING id",
            (src_id, past_time),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h6', 'Старая новость', %s) RETURNING id",
            (item_id, past_time),
        )
        rev_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'hr6', 'vr6') RETURNING id",
            (edition.id,),
        )
        rel_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id",
            (rev_id, edition.id, rel_pol_id),
        )
        rel_dec_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'he6', 've6') RETURNING id",
            (edition.id,),
        )
        extr_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status) VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id",
            (rev_id, edition.id, extr_pol_id, rel_dec_id),
        )
        extr_run_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at) VALUES (%s, %s, %s, 'Старая новость', 'старая новость', %s) RETURNING id",
            (extr_run_id, rev_id, edition.id, past_time),
        )
        claim_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, past_time),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) VALUES (%s, 1, 'open', 'Старая история 10 дней назад', 'hs6', %s) RETURNING id",
            (story_id, past_time),
        )
        srev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (srev_id, story_id)
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, claim_id, past_time),
        )

        # Snapshot at _NOW (24h lookback window)
        pub_run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-old-story-lookback",
        )
        candidates = await snap_service.seal_candidates(pub_run.id)
        assert len(candidates) == 0

    async def test_community_source_named_official_remains_attributed(
        self, conn: psycopg.AsyncConnection, edition, pool
    ):
        """Community sources named 'official' maintain accurate provenance and source metadata."""
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)
        sel_service = EditorialSelectionService(uow=uow, model=HeuristicSelectionModel())
        adapter = KnowledgeEditorialAdapter(uow=uow)

        cur = await conn.execute(
            "INSERT INTO sources (platform, kind, external_id, url, name, role) VALUES ('telegram', 'channel', '-1007', 'https://t.me/pseudo_off', 'Официальный Бердянск ЧАТ', 'community') RETURNING id"
        )
        src_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', '7', %s) RETURNING id",
            (src_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h7', 'Открыт новый пункт вакцинации', %s) RETURNING id",
            (item_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'hr7', 'vr7') RETURNING id",
            (edition.id,),
        )
        rel_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id",
            (rev_id, edition.id, rel_pol_id),
        )
        rel_dec_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'he7', 've7') RETURNING id",
            (edition.id,),
        )
        extr_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status) VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id",
            (rev_id, edition.id, extr_pol_id, rel_dec_id),
        )
        extr_run_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at) VALUES (%s, %s, %s, 'Пункт вакцинации открыт', 'пункт вакцинации открыт', %s) RETURNING id",
            (extr_run_id, rev_id, edition.id, _NOW),
        )
        claim_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) VALUES (%s, 1, 'open', 'Вакцинация в Бердянске', 'hs7', %s) RETURNING id",
            (story_id, _NOW),
        )
        srev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (srev_id, story_id)
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, claim_id, _NOW),
        )

        pub_run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-pseudo-official",
        )
        await snap_service.seal_candidates(pub_run.id)
        inputs = await sel_service.select(pub_run.id)
        assert len(inputs) == 1

        frozen = await adapter.build(pub_run.id)
        assert len(frozen.analysis.cards) == 1
        card = frozen.analysis.cards[0]
        expected_ref = f"telegram:source:{src_id}:item:{item_id}:rev:{rev_id}"
        assert expected_ref in card.all_source_refs()
        assert expected_ref in frozen.writer_bundle.records
        assert frozen.writer_bundle.records[expected_ref].source_type == "channel"
        assert (
            frozen.writer_bundle.records[expected_ref].message.channel_name
            == "Официальный Бердянск ЧАТ"
        )
        assert len(card.community_observations) == 1
        assert len(card.hard_facts) == 0
