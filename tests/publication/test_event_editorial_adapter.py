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
        conn, edition_id=edition.id, config_hash="h-e2", prompt_version="v1"
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


@pytest.mark.postgres
async def test_event_editorial_adapter_preserves_evidence_source_text(conn, pool, edition):
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()

    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition.id, config_hash="h-e3", prompt_version="v1"
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
