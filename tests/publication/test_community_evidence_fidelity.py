"""Cross-layer integration regression test verifying uncorroborated community evidence fidelity.

Traces a single community message from fragmentation -> triage -> selection ->
editorial adapter -> article context -> writer prompt -> validation -> claim trace.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from unittest.mock import AsyncMock

import pytest

from src.article_generator import ArticleGenerator
from src.config_loader import Config, PublicationEditorialConfig, Settings
from src.db.uow import DatabaseUnitOfWork
from src.domain.event_payload import EventPayload
from src.processing.fragments import split_into_fragments
from src.publication.article_models import StructuredArticleDraft
from src.publication.article_trace import build_article_claim_trace
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.models import PublicationSelectionDecision
from src.publication.repository import PublicationPolicyRepository, PublicationRepository

_NOW = dt.datetime(2026, 8, 30, 14, 0, tzinfo=dt.timezone.utc)


@pytest.mark.postgres
async def test_uncorroborated_community_report_full_lifecycle(conn, pool, edition):
    """A single community report flows through all pipeline stages without being dropped or corrupted."""
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()

    # 1. Deterministic fragmentation of short civic message
    raw_message = "На Горе света нет, когда включат?"
    fragments = split_into_fragments(raw_message)
    candidate_texts = [f.text_content for f in fragments if f.is_candidate]
    assert any("света нет" in t.lower() for t in candidate_texts)
    frag_text = next(t for t in candidate_texts if "света нет" in t.lower())

    # 2. Persist community source, item, and fragment in DB
    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-100888', 'https://t.me/chat', 'Чат Горы', 'community')
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
        VALUES (%s, 'message', 'msg-comm-1', %s)
        RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-comm-rev-1', %s)
        RETURNING id
        """,
        (item_id, raw_message),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (
            source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, created_at
        ) VALUES (%s, 0, %s, 'h-comm-frag-1', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, frag_text, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    # 3. Create story and story revision representing Gate V2 kept triage decision
    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _NOW),
    )
    story_id = (await cur.fetchone())[0]

    # Gate V2 decision: single community source, keep as brief, community_report evidence kind
    triage_payload = EventPayload.from_dict(
        {
            "topic": "Электроснабжение на Горе",
            "category": "utilities",
            "urgency": "medium",
            "publishability": "brief",
            "headline": "Сообщения об отсутствии света на Горе",
            "digest_summary": "Жители сообщают об отключении электричества в районе Горы.",
            "evidence_items": [
                {
                    "text": "На Горе света нет",
                    "kind": "community_report",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [frag_id],
                }
            ],
        }
    )

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-rev-triage', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            story_id,
            triage_payload.digest_summary,
            triage_payload.headline,
            triage_payload.digest_summary,
            json.dumps(triage_payload.to_dict()),
            _NOW,
        ),
    )
    rev_id = (await cur.fetchone())[0]

    # 4. Selection and Publication Run setup
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn,
        edition_id=edition.id,
        config_hash="h-e-com",
        prompt_version="v1",
        config={"lookback_hours": 24},
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-com", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-com", prompt_version="v1"
    )

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="article",
        request_key="test-comm-fidelity",
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
            presentation_intent="normal",
            confidence=0.9,
            reason="Uncorroborated community report is eligible local news",
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
        presentation_intent="normal",
        rank=1,
        fragment_ids=[frag_id],
    )

    # 5. EventEditorialAdapter adapts input into StoryCard and ArticleEditorialContext
    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    editorial = await adapter.adapt_inputs_on(conn, run.id, inputs=[pub_input])

    assert len(editorial.analysis.cards) == 1
    card = editorial.analysis.cards[0]
    # In StoryCard: must be partitioned into community_observations, NOT hard_facts
    assert any("На Горе света нет" in e.text for e in card.community_observations)
    assert not any("На Горе света нет" in e.text for e in card.hard_facts)

    # In ArticleEditorialContext: check ArticleSupport
    article_ctx = editorial.analysis.article_context
    assert article_ctx is not None
    assert len(article_ctx.support_index) >= 1
    comm_support = next(s for s in article_ctx.support_index if frag_id in s.fragment_ids)
    assert comm_support.evidence_kind == "community_report"
    assert comm_support.source_roles == ("community",)
    assert comm_support.temporal_role == "CURRENT_WINDOW"

    prompt_ctx = article_ctx.to_prompt_context()
    assert f"[SUPPORT {comm_support.support_id}]" in prompt_ctx
    assert "evidence_kind=community_report" in prompt_ctx
    assert "source_roles=community" in prompt_ctx
    assert "framing=attributed_report" in prompt_ctx

    # 6. Single-call ArticleGenerator with mocked LLM output
    settings = Settings(
        schedule_time="09:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4",
        openai_temperature=0.3,
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
    gen = ArticleGenerator(config=config, logger=logging.getLogger("test"))
    gen.provider = AsyncMock()

    writer_json = json.dumps(
        {
            "title": "На Горе света нет",
            "title_support_ids": [comm_support.support_id],
            "title_claims": [
                {"text": "На Горе света нет", "cited_support_ids": [comm_support.support_id]}
            ],
            "lead": "Жители сообщают о перебоях с электроснабжением.",
            "lead_support_ids": [comm_support.support_id],
            "lead_claims": [
                {"text": "На Горе света нет", "cited_support_ids": [comm_support.support_id]}
            ],
            "sections": [
                {
                    "heading": "Обстановка на Горе",
                    "heading_support_ids": [comm_support.support_id],
                    "heading_claims": [],
                    "paragraphs": [
                        {
                            "text": "По сообщениям местных жителей, на Горе нет света.",
                            "cited_support_ids": [comm_support.support_id],
                            "claims": [
                                {
                                    "text": "На Горе света нет",
                                    "cited_support_ids": [comm_support.support_id],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    gen.provider.chat_completion.return_value = writer_json

    title, lead, body = await gen.generate_from_frozen_input(editorial)
    assert title == "На Горе света нет"
    assert "Жители сообщают" in lead

    # 7. Validate claim trace contains exact provenance
    trace = build_article_claim_trace(
        StructuredArticleDraft.from_dict(json.loads(writer_json)),
        article_ctx,
    )
    assert len(trace) >= 3  # TITLE, LEAD, H001, P001
    p_trace = next(u for u in trace if u.unit_id == "P001")
    assert p_trace.evidence_kinds == ("community_report",)
    assert p_trace.source_roles == ("community",)
    assert frag_id in p_trace.fragment_ids
    assert item_id in p_trace.source_item_ids
    assert p_trace.claim_atoms[0].evidence_kinds == ("community_report",)
    assert p_trace.claim_atoms[0].source_roles == ("community",)


@pytest.mark.postgres
async def test_resident_question_full_lifecycle_suppression_and_context_propagation(
    conn, pool, edition
):
    """A resident question is kept as context, suppressed as a standalone card, and framed properly in articles."""
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_repo = PublicationPolicyRepository()

    # 1. Deterministic fragmentation of question message and main news message
    raw_message = "Подскажите, работает ли пенсионный фонд?"
    fragments = split_into_fragments(raw_message)
    candidate_texts = [f.text_content for f in fragments if f.is_candidate]
    assert any("пенсионный фонд" in t.lower() for t in candidate_texts)
    frag_text = next(t for t in candidate_texts if "пенсионный фонд" in t.lower())

    main_message = "Службы города ведут плановые работы в штатном режиме."
    main_frag_text = "Службы города ведут плановые работы"

    # 2. Persist source, items, and fragments in DB
    cur = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name, role)
        VALUES ('telegram', 'channel', '-100999', 'https://t.me/chat_q', 'Чат Вопросов', 'community')
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
        VALUES (%s, 'message', 'msg-q-1', %s)
        RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-q-rev-1', %s)
        RETURNING id
        """,
        (item_id, raw_message),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (
            source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, created_at
        ) VALUES (%s, 0, %s, 'h-q-frag-1', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, frag_text, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    # Main news fragment
    cur = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', 'msg-main-1', %s)
        RETURNING id
        """,
        (src_id, _NOW),
    )
    main_item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-main-rev-1', %s)
        RETURNING id
        """,
        (main_item_id, main_message),
    )
    main_sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (
            source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, created_at
        ) VALUES (%s, 0, %s, 'h-main-frag-1', 'v1', TRUE, %s)
        RETURNING id
        """,
        (main_sir_id, main_frag_text, _NOW),
    )
    main_frag_id = (await cur.fetchone())[0]

    # 3. Create question story and main story
    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _NOW),
    )
    story_id = (await cur.fetchone())[0]

    triage_payload = EventPayload.from_dict(
        {
            "topic": "Вопрос о пенсионном фонде",
            "publishability": "brief",
            "headline": "Жители спрашивают о графике пенсионного фонда",
            "digest_summary": "В чатах интересуются приемом граждан.",
            "evidence_items": [
                {
                    "text": frag_text,
                    "kind": "resident_question",
                    "publication_use": "CONTEXT",
                    "source_fragment_ids": [frag_id],
                }
            ],
            "operational_observations": [],
        },
        allowed_fragment_ids={frag_id},
    )

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-q-rev', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            story_id,
            triage_payload.digest_summary,
            triage_payload.headline,
            triage_payload.digest_summary,
            json.dumps(triage_payload.to_dict()),
            _NOW,
        ),
    )
    rev_id = (await cur.fetchone())[0]

    # Main news story
    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, _NOW),
    )
    main_story_id = (await cur.fetchone())[0]

    main_payload = EventPayload.from_dict(
        {
            "topic": "Работы городских служб",
            "publishability": "news",
            "headline": "Городские службы ведут плановые работы",
            "digest_summary": "Службы города ведут плановые работы.",
            "evidence_items": [
                {
                    "text": main_frag_text,
                    "kind": "established_fact",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [main_frag_id],
                }
            ],
            "operational_observations": [],
        },
        allowed_fragment_ids={main_frag_id},
    )

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-main-rev', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            main_story_id,
            main_payload.digest_summary,
            main_payload.headline,
            main_payload.digest_summary,
            json.dumps(main_payload.to_dict()),
            _NOW,
        ),
    )
    main_rev_id = (await cur.fetchone())[0]

    # 4. Publication run selection & freezing
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn,
        edition_id=edition.id,
        config_hash="h-e-q2",
        prompt_version="v1",
        config={"lookback_hours": 24},
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition.id, config_hash="h-s-q2", prompt_version="v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition.id, config_hash="h-w-q2", prompt_version="v1"
    )

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="article",
        request_key="test-q-fidelity-run",
        snapshot_at=_NOW,
        policy_ids=(elig.id, sel.id, wri.id),
    )
    cand_main = await repo.insert_candidate(
        conn, run.id, story_id=main_story_id, story_revision_id=main_rev_id, deterministic_rank=1
    )
    dec_main = await repo.insert_selection_decision(
        conn,
        run.id,
        PublicationSelectionDecision(
            id=0,
            publication_run_id=run.id,
            candidate_id=cand_main.id,
            decision="INCLUDE",
            presentation_intent="lead",
            confidence=0.95,
            reason="Main story",
            rank=1,
            metadata={},
            created_at=_NOW,
        ),
    )
    pub_input_main = await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=main_story_id,
        story_revision_id=main_rev_id,
        selection_decision_id=dec_main.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[main_frag_id],
    )

    cand_q = await repo.insert_candidate(
        conn, run.id, story_id=story_id, story_revision_id=rev_id, deterministic_rank=2
    )
    dec_q = await repo.insert_selection_decision(
        conn,
        run.id,
        PublicationSelectionDecision(
            id=0,
            publication_run_id=run.id,
            candidate_id=cand_q.id,
            decision="INCLUDE",
            presentation_intent="normal",
            confidence=0.9,
            reason="Resident question context",
            rank=2,
            metadata={},
            created_at=_NOW,
        ),
    )
    pub_input_q = await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=story_id,
        story_revision_id=rev_id,
        selection_decision_id=dec_q.id,
        presentation_intent="normal",
        rank=2,
        fragment_ids=[frag_id],
    )

    # 5. Editorial adapter preserves evidence as CONTEXT and suppresses standalone card
    adapter = EventEditorialAdapter(uow=uow, repo=repo)
    editorial = await adapter.adapt_inputs_on(conn, run.id, inputs=[pub_input_main, pub_input_q])

    # Suppressed as standalone card
    assert all(card.id != f"story:{story_id}" for card in editorial.analysis.cards)
    # Main story card is present
    assert any(card.id == f"story:{main_story_id}" for card in editorial.analysis.cards)

    # But ArticleSupport is created for question with question_context framing
    article_ctx = editorial.analysis.article_context
    assert article_ctx is not None
    q_support = next(s for s in article_ctx.support_index if frag_id in s.fragment_ids)
    assert q_support.evidence_kind == "resident_question"
    assert q_support.publication_use == "CONTEXT"

    main_support = next(s for s in article_ctx.support_index if main_frag_id in s.fragment_ids)
    assert main_support.publication_use == "PUBLISH"

    prompt_ctx = article_ctx.to_prompt_context()
    assert "framing=question_context" in prompt_ctx
    assert "publication_use=CONTEXT" in prompt_ctx

    # 6. Single-call ArticleGenerator with inquiry framing
    settings = Settings(
        schedule_time="09:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4",
        openai_temperature=0.3,
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
    gen = ArticleGenerator(config=config, logger=logging.getLogger("test"))
    gen.provider = AsyncMock()

    writer_json = json.dumps(
        {
            "title": "Городские службы ведут плановые работы",
            "title_support_ids": [main_support.support_id],
            "title_claims": [
                {
                    "text": "Городские службы ведут плановые работы",
                    "cited_support_ids": [main_support.support_id],
                }
            ],
            "lead": "Службы города ведут плановые работы в штатном режиме.",
            "lead_support_ids": [main_support.support_id],
            "lead_claims": [
                {
                    "text": "Службы города ведут плановые работы",
                    "cited_support_ids": [main_support.support_id],
                }
            ],
            "sections": [
                {
                    "heading": "Работа учреждений",
                    "heading_support_ids": [main_support.support_id],
                    "heading_claims": [],
                    "paragraphs": [
                        {
                            "text": "Службы города ведут плановые работы. При этом жители в чатах спрашивают, работает ли пенсионный фонд.",
                            "cited_support_ids": [main_support.support_id, q_support.support_id],
                            "claims": [
                                {
                                    "text": "Службы города ведут плановые работы",
                                    "cited_support_ids": [main_support.support_id],
                                },
                                {
                                    "text": "Жители спрашивают, работает ли пенсионный фонд",
                                    "cited_support_ids": [q_support.support_id],
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    )
    gen.provider.chat_completion.return_value = writer_json

    title, lead, body = await gen.generate_from_frozen_input(editorial)
    assert title == "Городские службы ведут плановые работы"
    assert "спрашивают" in body

    # 7. Trace validation
    trace = build_article_claim_trace(
        StructuredArticleDraft.from_dict(json.loads(writer_json)),
        article_ctx,
    )
    p_trace = next(u for u in trace if u.unit_id == "P001")
    assert "resident_question" in p_trace.evidence_kinds
    assert frag_id in p_trace.fragment_ids
