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
    assert len(card.hard_facts) == 2
    assert card.hard_facts[0].text == "Порыв на трубопроводе d=500мм"
    assert len(card.community_observations) == 1

    # Test digest formatter
    rendered_digest = format_event_digest_grouped(editorial_input)
    assert "Ликвидация аварии на водоводе в АКЗ" in rendered_digest
    assert "Порыв на трубопроводе d=500мм" in rendered_digest
