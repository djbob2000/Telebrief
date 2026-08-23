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
        expected_ref = f"telegram:source:{source_id}:item:{item_id}:rev:{rev_id}"
        assert expected_ref in card.all_source_refs()
        assert len(frozen.writer_bundle.records) == 1
        assert expected_ref in frozen.writer_bundle.records

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

    async def test_knowledge_editorial_adapter_facebook_link_generation(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        adapter = KnowledgeEditorialAdapter(uow=uow)
        snap_service = PublicationSnapshotService(uow=uow)
        sel_service = EditorialSelectionService(uow=uow)

        # 1. Seed Facebook source & item with no canonical_url / s_url
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, name, role, enabled)
            VALUES ('facebook', 'facebook_group', 'group-999', 'Facebook Group', 'community', true)
            RETURNING id
            """
        )
        src_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO source_items (source_id, external_id, kind, first_collected_at)
            VALUES (%s, 'post:987654', 'facebook_post', %s)
            RETURNING id
            """,
            (src_id, _NOW),
        )
        item_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at)
            VALUES (%s, 1, 'h-fb1', 'Новость из группы Фейсбука', %s)
            RETURNING id
            """,
            (item_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]

        # 2. Seed relevance + claim extraction
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'rh-fb', 'rv-fb') RETURNING id",
            (edition.id,),
        )
        rel_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id",
            (rev_id, edition.id, rel_pol_id),
        )
        rel_dec_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'ch-fb', 'cv-fb') RETURNING id",
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
            VALUES (%s, %s, %s, 'Ремонт набережной завершен', 'ремонт набережной завершен', %s)
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
            VALUES (%s, 1, 'open', 'Ремонт набережной', 'h-sfb', %s)
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
            request_key="test-fb-adapter-build",
        )
        await snap_service.seal_candidates(run.id)
        await sel_service.select(run.id)

        # 6. Adapter build & verify Facebook link
        frozen = await adapter.build(run.id)
        expected_fb_ref = f"facebook:source:{src_id}:item:{item_id}:rev:{rev_id}"
        rec = frozen.writer_bundle.records.get(expected_fb_ref)
        assert rec is not None
        assert rec.message.link == "https://www.facebook.com/987654"

    async def test_community_source_named_official_stays_attributed_and_elements_reference_only_own_claim(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)
        sel_service = EditorialSelectionService(uow=uow, model=HeuristicSelectionModel())
        adapter = KnowledgeEditorialAdapter(uow=uow)

        # 1. Source 1: Community source with "Официально" in its name but role="community"
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role)
            VALUES ('telegram', 'channel', '@berdyansk_community', 'https://t.me/berdyansk_community', 'Бердянск Официально', 'community')
            RETURNING id
            """
        )
        src1_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', '101', %s) RETURNING id",
            (src1_id, _NOW),
        )
        item1_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h-msg101-src1', 'Сообщение от жителей о ремонте', %s) RETURNING id",
            (item1_id, _NOW),
        )
        rev1_id = (await cur.fetchone())[0]

        # 2. Source 2: Official source with exact same message external_id '101'
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role)
            VALUES ('telegram', 'channel', '@real_admin', 'https://t.me/real_admin', 'Администрация Города', 'official')
            RETURNING id
            """
        )
        src2_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', '101', %s) RETURNING id",
            (src2_id, _NOW),
        )
        item2_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h-msg101-src2', 'Официальный пресс-релиз администрации', %s) RETURNING id",
            (item2_id, _NOW),
        )
        rev2_id = (await cur.fetchone())[0]

        # Relevance + Extraction setup
        cur = await conn.execute(
            "INSERT INTO relevance_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'rh-prov', 'rv') RETURNING id",
            (edition.id,),
        )
        rel_pol_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_policy_versions (edition_id, version, config_hash, prompt_version) VALUES (%s, 1, 'ch-prov', 'cv') RETURNING id",
            (edition.id,),
        )
        extr_pol_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id",
            (rev1_id, edition.id, rel_pol_id),
        )
        rdec1_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status) VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id",
            (rev1_id, edition.id, extr_pol_id, rdec1_id),
        )
        extr1_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            "INSERT INTO edition_relevance_decisions (source_item_revision_id, edition_id, relevance_policy_id, status, reason) VALUES (%s, %s, %s, 'relevant', 'ok') RETURNING id",
            (rev2_id, edition.id, rel_pol_id),
        )
        rdec2_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO claim_extraction_runs (source_item_revision_id, edition_id, extraction_policy_id, relevance_decision_id, status) VALUES (%s, %s, %s, %s, 'succeeded') RETURNING id",
            (rev2_id, edition.id, extr_pol_id, rdec2_id),
        )
        extr2_id = (await cur.fetchone())[0]

        # Claims from both
        cur = await conn.execute(
            """
            INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at)
            VALUES (%s, %s, %s, 'Жители пишут о шуме на стройке', 'жители пишут о шуме', %s) RETURNING id
            """,
            (extr1_id, rev1_id, edition.id, _NOW),
        )
        claim1_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO claims (claim_extraction_run_id, source_item_revision_id, edition_id, assertion_text, normalized_assertion, created_at)
            VALUES (%s, %s, %s, 'Администрация утвердила график работ', 'график утвержден', %s) RETURNING id
            """,
            (extr2_id, rev2_id, edition.id, _NOW),
        )
        claim2_id = (await cur.fetchone())[0]

        # Story containing both claims
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Строительные работы в центре города', 'h-s-multi', %s) RETURNING id
            """,
            (story_id, _NOW),
        )
        s_rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (s_rev_id, story_id)
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, claim1_id, _NOW),
        )
        await conn.execute(
            "INSERT INTO story_claims (story_id, claim_id, attached_at) VALUES (%s, %s, %s)",
            (story_id, claim2_id, _NOW),
        )

        # Snapshot & select
        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-provenance-distinction",
        )
        await snap_service.seal_candidates(run.id)
        await sel_service.select(run.id)

        frozen = await adapter.build(run.id)
        card = frozen.analysis.cards[0]

        ref1 = f"telegram:source:{src1_id}:item:{item1_id}:rev:{rev1_id}"
        ref2 = f"telegram:source:{src2_id}:item:{item2_id}:rev:{rev2_id}"

        # Distinct refs despite same message ID '101'
        assert ref1 != ref2
        assert ref1 in frozen.writer_bundle.records
        assert ref2 in frozen.writer_bundle.records
        assert len(frozen.writer_bundle.records) == 2

        # Claim 1 from community source named "Бердянск Официально" must be in community_observations with attributed status
        assert len(card.community_observations) == 1
        comm_elem = card.community_observations[0]
        assert comm_elem.text == "Жители пишут о шуме на стройке"
        assert comm_elem.status == "attributed"
        assert comm_elem.source_refs == [ref1]  # References only its own claim ref!

        # Claim 2 from official source must be in hard_facts with established status
        assert len(card.hard_facts) == 1
        off_elem = card.hard_facts[0]
        assert off_elem.text == "Администрация утвердила график работ"
        assert off_elem.status == "established"
        assert off_elem.source_refs == [ref2]  # References only its own claim ref!
