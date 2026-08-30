"""Tests for EventEditorialAdapter and event-first formatting."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from src.db.uow import DatabaseUnitOfWork
from src.publication.event_editorial_adapter import (
    EventEditorialAdapter,
    format_event_digest_grouped,
)
from src.publication.models import PublicationSelectionDecision
from src.publication.repository import PublicationPolicyRepository, PublicationRepository

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


@pytest.mark.postgres
async def test_event_editorial_adapter_end_to_end(conn, pool, edition):
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

    # 1. Create event_first story & revision with rich payload
    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _NOW),
    )
    story_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Авария на водоводе",
        "tags": ["водовод", "авария", "АКЗ"],
        "category": "utilities",
        "urgency": "high",
        "publishability": "news",
        "headline": "Ликвидация аварии на водоводе в АКЗ",
        "digest_summary": "Специалисты водоканала завершают сварочные работы на Мелитопольском шоссе.",
        "key_facts": ["Порыв на трубопроводе d=500мм", "Водоснабжение восстановят к 18:00"],
        "official_positions": [{"source": "Водоканал", "statement": "Работы идут по графику"}],
        "community_observations": ["На улице Западной слабый напор"],
        "conflicts_or_uncertainties": [],
        "affected_areas": ["АКЗ", "РТС"],
        "timeline_summary": "Авария произошла в 8:00",
        "confidence_score": 0.96,
        "representative_fragment_ids": [],
    }

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-ev-rev', %s, %s, %s, %s)
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

    # Source and fragment
    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-10042', 'https://t.me/vodokanal', 'Бердянскводоканал', 'official')
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
        VALUES (%s, 'message', 'msg-42', %s)
        RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-rev-42', 'Сварочные работы продолжаются')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'Сварочные работы продолжаются', 'h-f-42', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    # Publication run & input
    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        request_key="test-event-adapter-key",
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

    from src.publication.models import PublicationSelectionDecision

    dec = await repo.insert_selection_decision(
        conn,
        run.id,
        PublicationSelectionDecision(
            id=0,
            publication_run_id=run.id,
            candidate_id=cand.id,
            decision="INCLUDE",
            presentation_intent="lead",
            confidence=0.96,
            reason="Important news",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )

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

    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    editorial_input = await adapter.adapt_inputs_on(conn, run.id, inputs=[pub_input])

    assert len(editorial_input.analysis.cards) == 1
    card = editorial_input.analysis.cards[0]
    assert card.topic == "Ликвидация аварии на водоводе в АКЗ"
    assert (
        card.summary == "Специалисты водоканала завершают сварочные работы на Мелитопольском шоссе."
    )
    assert card.tags == ["водовод", "авария", "АКЗ"]
    assert card.rubric_id == ""
    assert len(card.hard_facts) == 2
    assert card.hard_facts[0].text == "Порыв на трубопроводе d=500мм"
    assert len(card.community_observations) == 1

    # Test digest formatter
    rendered_digest = format_event_digest_grouped(editorial_input)
    assert "Ликвидация аварии на водоводе в АКЗ" in rendered_digest
    assert "Порыв на трубопроводе d=500мм" in rendered_digest


@pytest.mark.postgres
async def test_event_editorial_adapter_exact_observation_provenance(conn, pool, edition):
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()

    elig = await policy_repo.get_or_create_eligibility_policy(
        conn,
        edition_id=edition.id,
        config_hash="h-e2",
        prompt_version="v1",
        config={"lookback_hours": 24},
    )

    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s2", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w2", prompt_version="v1"
    )

    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _NOW),
    )
    story_id = (await cur.fetchone())[0]

    # Source and 2 fragments
    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-10099', 'https://t.me/news', 'News', 'community')
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
        VALUES (%s, 'message', 'msg-prov', %s)
        RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-rev-prov', 'Текст новости')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES
        (%s, 0, 'В центре нет света', 'h-f-101', 'v1', TRUE, %s),
        (%s, 1, 'На Косе свет есть', 'h-f-102', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, _NOW, sir_id, _NOW),
    )
    frag_ids = [r[0] for r in await cur.fetchall()]
    f1_id, f2_id = frag_ids[0], frag_ids[1]

    event_payload = {
        "topic": "Электричество в городе",
        "headline": "Ситуация со светом",
        "digest_summary": "В центре отключение, на Косе свет есть.",
        "operational_observations": [
            {
                "subject_key": "power_supply",
                "subject_label": "Электросеть",
                "dimension": "availability",
                "location": "Центр",
                "entity": "сеть",
                "state": "UNAVAILABLE",
                "detail": "нет света",
                "source_fragment_ids": [f1_id],
            },
            {
                "subject_key": "power_supply",
                "subject_label": "Электросеть",
                "dimension": "availability",
                "location": "Коса",
                "entity": "сеть",
                "state": "AVAILABLE",
                "detail": "со светом",
                "source_fragment_ids": [f2_id],
            },
        ],
    }

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-prov-rev', %s, %s, %s, %s)
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
        request_key="test-event-prov-key",
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
    from src.publication.models import PublicationSelectionDecision

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
            reason="OK",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )
    pub_input = await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        selection_decision_id=dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[f1_id, f2_id],
    )

    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    editorial_input = await adapter.adapt_inputs_on(conn, run.id, inputs=[pub_input])

    card = editorial_input.analysis.cards[0]
    # Check that the 2 observation elements carry exact separate fragment source refs
    assert len(card.community_observations) == 2
    obs1 = card.community_observations[0]
    obs2 = card.community_observations[1]

    assert f"frag:{f1_id}" in obs1.source_refs[0]
    assert f"frag:{f2_id}" not in obs1.source_refs[0]

    assert f"frag:{f2_id}" in obs2.source_refs[0]
    assert f"frag:{f1_id}" not in obs2.source_refs[0]
    assert editorial_input.analysis.article_context.edition_name == edition.name


@pytest.mark.postgres
async def test_event_editorial_adapter_preserves_evidence_source_text(conn, pool, edition):
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn,
        edition_id=edition.id,
        config_hash="h-e3",
        prompt_version="v1",
        config={"lookback_hours": 24},
    )

    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s3", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w3", prompt_version="v1"
    )

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
        VALUES ('telegram', 'channel', '-10042', 'https://t.me/res', 'Бердянский РЭС', 'official')
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
        VALUES (%s, 'message', 'msg-res', %s)
        RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-rev-res', 'Авария на подстанции: временно обесточена центральная часть Бердянска. Бригада РЭС ведет восстановительные работы.')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    raw_fragment_text = (
        "Авария на подстанции: временно обесточена центральная часть Бердянска. "
        "Бригада РЭС ведет восстановительные работы."
    )
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, %s, 'h-f-res', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, raw_fragment_text, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Авария на подстанции",
        "headline": "Отключение света в центре",
        "digest_summary": "Центральная часть города временно обесточена.",
        "evidence_items": [
            {
                "text": "Центральная часть города временно обесточена",
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
        ) VALUES (%s, 1, 'open', %s, 'h-st-res', %s, %s, %s, %s)
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
        request_key="test-evidence-source-text",
        snapshot_at=_NOW,
        policy_ids=(elig.id, sel.id, wri.id),
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
            confidence=0.95,
            reason="OK",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )
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

    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    editorial = await adapter.adapt_inputs_on(conn, run.id, inputs=[pub_input])

    ctx = editorial.analysis.article_context
    assert ctx is not None
    # Check that PublicationEvidence in the context has text and exact source_text
    evidence = ctx.evidence_index[0]
    assert evidence.text == "Центральная часть города временно обесточена"
    assert evidence.source_text == raw_fragment_text
    assert evidence.fragment_id == frag_id


@pytest.mark.postgres
async def test_event_editorial_adapter_frozen_lookback_binding(conn, pool, edition):
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()

    elig = await policy_repo.get_or_create_eligibility_policy(
        conn,
        edition_id=edition.id,
        config_hash="h-e-24",
        prompt_version="v1",
        config={"lookback_hours": 24},
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-24", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-24", prompt_version="v1"
    )

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="article",
        request_key="test-frozen-lookback",
        snapshot_at=_NOW,
        policy_ids=(elig.id, sel.id, wri.id),
    )

    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    editorial = await adapter.adapt_inputs_on(conn, run.id, inputs=[])

    ctx = editorial.analysis.article_context
    assert ctx is not None
    assert ctx.publication_window is not None
    assert ctx.publication_window.snapshot_at == _NOW
    assert ctx.publication_window.lookback_start == _NOW - dt.timedelta(hours=24)


@pytest.mark.postgres
async def test_event_editorial_adapter_missing_or_invalid_lookback_raises(conn, pool, edition):
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()

    elig_missing = await policy_repo.get_or_create_eligibility_policy(
        conn,
        edition_id=edition.id,
        config_hash="h-e-missing",
        prompt_version="v1",
        config={},
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-x", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-x", prompt_version="v1"
    )

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="article",
        request_key="test-missing-lookback",
        snapshot_at=_NOW,
        policy_ids=(elig_missing.id, sel.id, wri.id),
    )

    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    with pytest.raises(
        ValueError, match="frozen eligibility policy missing numeric lookback_hours"
    ):
        await adapter.adapt_inputs_on(conn, run.id, inputs=[])


@pytest.mark.postgres
async def test_event_editorial_adapter_epistemic_evidence_partitioning(conn, pool, edition):
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()

    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition.id, config_hash="h-e-ep", prompt_version="v1"
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-ep", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-ep", prompt_version="v1"
    )

    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _NOW),
    )
    story_id = (await cur.fetchone())[0]

    # Official source
    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-10042', 'https://t.me/official', 'Официальный канал', 'official')
        RETURNING id
        """
    )
    src_off_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (src_off_id, edition.id),
    )
    cur = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', 'msg-off', %s)
        RETURNING id
        """,
        (src_off_id, _NOW),
    )
    item_off_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-off-rev', 'Официальное сообщение')
        RETURNING id
        """,
        (item_off_id,),
    )
    sir_off_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'Официальный фрагмент', 'h-f-off', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_off_id, _NOW),
    )
    official_frag_id = (await cur.fetchone())[0]

    # Community source
    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-10099', 'https://t.me/comm', 'Городской чат', 'community')
        RETURNING id
        """
    )
    src_comm_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (src_comm_id, edition.id),
    )
    cur = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', 'msg-comm', %s)
        RETURNING id
        """,
        (src_comm_id, _NOW),
    )
    item_comm_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-comm-rev', 'Сообщение жителей')
        RETURNING id
        """,
        (item_comm_id,),
    )
    sir_comm_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'Фрагмент жителей', 'h-f-comm', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_comm_id, _NOW),
    )
    community_frag_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Энергетика и сервисы",
        "headline": "Ситуация с электричеством",
        "digest_summary": "Сводка событий.",
        "evidence_items": [
            {
                "text": "На подстанции зафиксировано технологическое повреждение",
                "kind": "established_fact",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [official_frag_id],
            },
            {
                "text": "Водоканал сообщил о ремонтных работах",
                "kind": "official_statement",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [official_frag_id],
            },
            {
                "text": "На Горе света нет",
                "kind": "community_report",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [community_frag_id],
            },
            {
                "text": "Банкомат на Морозова выдает наличные",
                "kind": "service_access",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [community_frag_id],
            },
        ],
    }

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-ep-rev', %s, %s, %s, %s)
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
        request_key="test-epistemic-partitioning",
        snapshot_at=_NOW,
        policy_ids=(elig.id, sel.id, wri.id),
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
            confidence=0.95,
            reason="OK",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )
    pub_input = await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        selection_decision_id=dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[official_frag_id, community_frag_id],
    )

    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    editorial = await adapter.adapt_inputs_on(conn, run.id, inputs=[pub_input])

    assert len(editorial.analysis.cards) == 1
    card = editorial.analysis.cards[0]

    assert [e.text for e in card.hard_facts] == [
        "На подстанции зафиксировано технологическое повреждение",
        "Водоканал сообщил о ремонтных работах",
    ]
    assert card.hard_facts[0].status == "established"
    assert card.hard_facts[1].status == "attributed"

    assert any(e.text == "На Горе света нет" for e in card.community_observations)
    community = next(e for e in card.community_observations if e.text == "На Горе света нет")
    assert community.status == "attributed"

    assert any(e.text == "Банкомат на Морозова выдает наличные" for e in card.useful_details)
    service = next(
        e for e in card.useful_details if e.text == "Банкомат на Морозова выдает наличные"
    )
    assert service.status == "attributed"

    # Assert the community item does not appear in hard_facts with status="established"
    assert not any(e.text == "На Горе света нет" for e in card.hard_facts)


@pytest.mark.postgres
async def test_event_editorial_adapter_resident_questions_suppressed_as_cards_kept_as_context(
    conn, pool, edition
):
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()

    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition.id, config_hash="h-e-q", prompt_version="v1"
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-q", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-q", prompt_version="v1"
    )

    # 1. Pure question story
    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _NOW),
    )
    q_story_id = (await cur.fetchone())[0]

    q_payload = {
        "topic": "Вопрос о пенсионном фонде",
        "tags": ["пенсионный фонд", "вопрос"],
        "urgency": "normal",
        "publishability": "brief",
        "headline": "Жители спрашивают о работе пенсионного фонда",
        "digest_summary": "В чатах интересуются графиком работы учреждения.",
        "key_facts": [],
        "evidence_items": [
            {
                "text": "Работает ли пенсионный фонд?",
                "kind": "resident_question",
                "publication_use": "CONTEXT",
                "source_fragment_ids": [7001],
            }
        ],
        "operational_observations": [],
    }

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-q-rev', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            q_story_id,
            q_payload["digest_summary"],
            q_payload["headline"],
            q_payload["digest_summary"],
            json.dumps(q_payload),
            _NOW,
        ),
    )
    q_rev_id = (await cur.fetchone())[0]

    # 2. Mixed story (question + actual answer)
    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _NOW),
    )
    m_story_id = (await cur.fetchone())[0]

    m_payload = {
        "topic": "Работа нотариуса",
        "tags": ["нотариус", "услуги"],
        "urgency": "normal",
        "publishability": "brief",
        "headline": "Нотариус принимает по записи",
        "digest_summary": "Нотариус работает от генератора.",
        "key_facts": [],
        "evidence_items": [
            {
                "text": "Работает ли нотариус?",
                "kind": "resident_question",
                "publication_use": "CONTEXT",
                "source_fragment_ids": [7002],
            },
            {
                "text": "Нотариус работает от генератора",
                "kind": "service_access",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [7003],
            },
        ],
        "operational_observations": [],
    }

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-m-rev', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            m_story_id,
            m_payload["digest_summary"],
            m_payload["headline"],
            m_payload["digest_summary"],
            json.dumps(m_payload),
            _NOW,
        ),
    )
    m_rev_id = (await cur.fetchone())[0]

    # Insert sources & fragments
    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-10099', 'https://t.me/chat', 'Чат Бердянск', 'community')
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
        VALUES (%s, 'message', 'msg-7001', %s)
        RETURNING id
        """,
        (src_id, _NOW),
    )
    si_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-si-7001', 'Текст')
        RETURNING id
        """,
        (si_id,),
    )
    sir_id = (await cur.fetchone())[0]

    for ord_idx, (fid, txt) in enumerate(
        [
            (7001, "Работает ли пенсионный фонд?"),
            (7002, "Работает ли нотариус?"),
            (7003, "Нотариус работает от генератора"),
        ]
    ):
        await conn.execute(
            """
            INSERT INTO source_fragments (
                id, source_item_revision_id, ordinal, text_content, normalized_hash,
                fragmenter_version, is_candidate, created_at
            ) OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, 'v1', TRUE, %s)
            """,
            (fid, sir_id, ord_idx, txt, f"hash-{fid}", _NOW),
        )

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        request_key="test-resident-questions-cards",
        snapshot_at=_NOW,
        policy_ids=(elig.id, sel.id, wri.id),
    )
    cand_q = await repo.insert_candidate(
        conn, run.id, story_id=q_story_id, story_revision_id=q_rev_id, deterministic_rank=1
    )
    dec_q = await repo.insert_selection_decision(
        conn,
        run.id,
        PublicationSelectionDecision(
            id=0,
            publication_run_id=run.id,
            candidate_id=cand_q.id,
            decision="INCLUDE",
            presentation_intent="lead",
            confidence=0.95,
            reason="OK",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )
    pub_in_q = await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=q_story_id,
        story_revision_id=q_rev_id,
        selection_decision_id=dec_q.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[7001],
    )

    cand_m = await repo.insert_candidate(
        conn, run.id, story_id=m_story_id, story_revision_id=m_rev_id, deterministic_rank=2
    )
    dec_m = await repo.insert_selection_decision(
        conn,
        run.id,
        PublicationSelectionDecision(
            id=0,
            publication_run_id=run.id,
            candidate_id=cand_m.id,
            decision="INCLUDE",
            presentation_intent="normal",
            confidence=0.95,
            reason="OK",
            rank=2,
            metadata={},
            created_at=_NOW,
        ),
    )
    pub_in_m = await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=m_story_id,
        story_revision_id=m_rev_id,
        selection_decision_id=dec_m.id,
        presentation_intent="normal",
        rank=2,
        fragment_ids=[7002, 7003],
    )

    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    editorial = await adapter.adapt_inputs_on(conn, run.id, inputs=[pub_in_q, pub_in_m])

    # Question-only story creates no StoryCard
    assert all(card.id != f"story:{q_story_id}" for card in editorial.analysis.cards)
    # But question evidence is preserved in all_evidence as CONTEXT
    q_evi = next(e for e in editorial.analysis.evidence.values() if e.story_id == q_story_id)
    assert q_evi.kind == "resident_question"
    assert q_evi.publication_use == "CONTEXT"

    # Mixed story creates a StoryCard with uncertainty for the question and useful_detail for the answer
    m_card = next(card for card in editorial.analysis.cards if card.id == f"story:{m_story_id}")
    assert any(u.basis == "resident_question" for u in m_card.uncertainties)
    assert any("работает" in d.text.lower() for d in m_card.useful_details)
