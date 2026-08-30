"""Integration tests for end-to-end evidence-bound article generation and bounded AI budget."""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock

import psycopg
import pytest

from src.article_generator import ArticleGenerator
from src.config_loader import Config, PublicationEditorialConfig, Settings
from src.db.uow import DatabaseUnitOfWork
from src.publication.generation import PublicationGenerationService
from src.publication.models import PublicationSelectionDecision
from src.publication.repository import (
    PublicationPolicyRepository,
    PublicationRepository,
)

_NOW = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)


async def _seed_policies(conn: psycopg.AsyncConnection, edition_id: int) -> tuple[int, int, int]:
    policy_repo = PublicationPolicyRepository()
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn,
        edition_id=edition_id,
        config_hash="elig-hash-e2e",
        prompt_version="elig-v1",
        config={"lookback_hours": 24},
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition_id, config_hash="sel-hash-e2e", prompt_version="sel-v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition_id, config_hash="wri-hash-e2e", prompt_version="wri-v1"
    )
    return (elig.id, sel.id, wri.id)


@pytest.mark.postgres
@pytest.mark.integration
async def test_end_to_end_article_generation_valid_draft_with_claim_trace(conn, pool, edition):
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
        VALUES ('telegram', 'channel', '-10055', 'https://t.me/adm', 'Администрация', 'official')
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
        VALUES (%s, 'msg', 'm-e2e-1', %s) RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-e2e-1', 'Специалисты водоканала завершили замену 500 метров труб на проспекте Труда.')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'Специалисты водоканала завершили замену 500 метров труб на проспекте Труда.', 'hf-e2e-1', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Ремонт водовода",
        "headline": "Замена водопроводных труб",
        "digest_summary": "Завершена замена водопроводных труб на проспекте Труда.",
        "evidence_items": [
            {
                "text": "Завершена замена 500 метров водопроводных труб",
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
        ) VALUES (%s, 1, 'open', %s, 'h-rev-e2e-1', %s, %s, %s, %s)
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
        request_key="test-e2e-valid-run",
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
    # Valid draft strictly matching support vocabulary
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "title": "Замена водопроводных труб на проспекте Труда",
            "title_support_ids": [sup_id],
            "title_claims": [{"text": "Замена водопроводных труб", "cited_support_ids": [sup_id]}],
            "lead": "В городе завершена замена водопроводных труб.",
            "lead_support_ids": [sup_id],
            "lead_claims": [
                {"text": "Завершена замена водопроводных труб", "cited_support_ids": [sup_id]}
            ],
            "sections": [
                {
                    "heading": "Водопроводные трубы",
                    "heading_support_ids": [sup_id],
                    "heading_claims": [
                        {"text": "Водопроводные трубы", "cited_support_ids": [sup_id]}
                    ],
                    "paragraphs": [
                        {
                            "text": "Специалисты водоканала завершили замену 500 метров труб на проспекте Труда.",
                            "cited_support_ids": [sup_id],
                            "claims": [
                                {
                                    "text": "Завершена замена 500 метров водопроводных труб на проспекте Труда",
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
    assert "500 метров" in pub.body


@pytest.mark.postgres
@pytest.mark.integration
async def test_end_to_end_article_generation_invalid_draft_fails_to_fallback(conn, pool, edition):
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
        VALUES ('telegram', 'channel', '-10056', 'https://t.me/adm2', 'Администрация', 'official')
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
        VALUES (%s, 'msg', 'm-e2e-2', %s) RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-e2e-2', 'Аварийные бригады работают на линии электропередач.')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'Аварийные бригады работают на линии электропередач.', 'hf-e2e-2', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Ремонт ЛЭП",
        "headline": "Работы на ЛЭП",
        "digest_summary": "Работы на линии электропередач.",
        "evidence_items": [
            {
                "text": "Аварийные бригады работают на линии электропередач",
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
        ) VALUES (%s, 1, 'open', %s, 'h-rev-e2e-2', %s, %s, %s, %s)
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
        request_key="test-e2e-invalid-run",
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
    # Unverified duration "в течение 20 минут"
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "title": "Работы на ЛЭП",
            "title_support_ids": [sup_id],
            "lead": "В городе ведутся работы на ЛЭП.",
            "lead_support_ids": [sup_id],
            "sections": [
                {
                    "heading": "Энергетика",
                    "heading_support_ids": [sup_id],
                    "paragraphs": [
                        {
                            "text": "Электроснабжение восстановили в течение 20 минут.",
                            "cited_support_ids": [sup_id],
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
        SELECT kind, status, error_kind
        FROM publication_generation_attempts
        WHERE publication_run_id = %s
        ORDER BY attempt_no ASC
        """,
        (run.id,),
    )
    attempts = await cur.fetchall()
    assert len(attempts) == 2
    assert attempts[0][0] == "writer"
    assert attempts[0][1] == "failed"
    assert attempts[0][2] == "ValidationFailed"
    assert attempts[1][0] == "story_renderer_fallback"
    assert attempts[1][1] == "succeeded"
    assert "20 минут" not in pub.body


@pytest.mark.postgres
@pytest.mark.integration
async def test_end_to_end_article_generation_multi_claim_narrative_paragraph_passes(
    conn, pool, edition
):
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
        VALUES ('telegram', 'channel', '-10057', 'https://t.me/adm3', 'Администрация', 'official')
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
        VALUES (%s, 'msg', 'm-e2e-3', %s) RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-e2e-3', 'Авария на электросетях в центре. Аптеки перешли на генераторы. В Колонии свет есть.')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES
            (%s, 0, 'Авария на электросетях в центре города.', 'hf-e2e-3a', 'v1', TRUE, %s),
            (%s, 1, 'Аптеки и магазины перешли на генераторы.', 'hf-e2e-3b', 'v1', TRUE, %s),
            (%s, 2, 'В микрорайоне Колония свет есть без перебоев.', 'hf-e2e-3c', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, _NOW, sir_id, _NOW, sir_id, _NOW),
    )
    frag_rows = await cur.fetchall()
    frag_ids = [r[0] for r in frag_rows]

    event_payload = {
        "topic": "Энергоснабжение",
        "headline": "Перебои с электричеством",
        "digest_summary": "Авария на сетях в центре, аптеки на генераторах, в Колонии порядок.",
        "evidence_items": [
            {
                "text": "Авария на электросетях в центре города",
                "kind": "established_fact",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [frag_ids[0]],
            },
            {
                "text": "Аптеки и магазины перешли на генераторы",
                "kind": "established_fact",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [frag_ids[1]],
            },
            {
                "text": "В микрорайоне Колония свет есть без перебоев",
                "kind": "established_fact",
                "publication_use": "PUBLISH",
                "source_fragment_ids": [frag_ids[2]],
            },
        ],
    }

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-rev-e2e-3', %s, %s, %s, %s)
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
        request_key="test-e2e-narrative-para-run",
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
        fragment_ids=frag_ids,
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
    sup0 = f"story:{story_id}:evidence:0:frag:{frag_ids[0]}"
    sup1 = f"story:{story_id}:evidence:1:frag:{frag_ids[1]}"
    sup2 = f"story:{story_id}:evidence:2:frag:{frag_ids[2]}"

    # Single paragraph combining 3 supported claims and citing 3 support IDs
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "title": "Авария на электросетях в центре города",
            "title_support_ids": [sup0],
            "title_claims": [
                {"text": "Авария на электросетях в центре города", "cited_support_ids": [sup0]}
            ],
            "lead": "В центре города произошла авария на электросетях.",
            "lead_support_ids": [sup0],
            "lead_claims": [
                {"text": "Авария на электросетях в центре города", "cited_support_ids": [sup0]}
            ],
            "sections": [
                {
                    "heading": "Электросети в центре города",
                    "heading_support_ids": [sup0],
                    "heading_claims": [
                        {"text": "Электросети в центре города", "cited_support_ids": [sup0]}
                    ],
                    "paragraphs": [
                        {
                            "text": "В центре города произошла авария на электросетях; местные аптеки и магазины перешли на генераторы, тогда как в микрорайоне Колония свет есть без перебоев.",
                            "cited_support_ids": [sup0, sup1, sup2],
                            "claims": [
                                {
                                    "text": "Авария на электросетях в центре города",
                                    "cited_support_ids": [sup0],
                                },
                                {
                                    "text": "Аптеки и магазины перешли на генераторы",
                                    "cited_support_ids": [sup1],
                                },
                                {
                                    "text": "В микрорайоне Колония свет есть без перебоев",
                                    "cited_support_ids": [sup2],
                                },
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
    assert meta["validation"]["is_valid"] is True
    assert len(meta["claim_trace"]) >= 4
    total_claim_atoms = sum(len(u["claim_atoms"]) for u in meta["claim_trace"])
    assert total_claim_atoms >= 6
    p001_trace = next(u for u in meta["claim_trace"] if u["unit_id"] == "P001")
    assert len(p001_trace["claim_atoms"]) == 3
    assert len(p001_trace["support_ids"]) == 3


@pytest.mark.postgres
@pytest.mark.integration
async def test_thin_safe_prose_under_800_words_not_rejected_as_fallback(conn, pool, edition):
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
        VALUES ('telegram', 'channel', '-10099', 'https://t.me/adm_power', 'Администрация', 'official')
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
        VALUES (%s, 'msg', 'm-e2e-thin-1', %s) RETURNING id
        """,
        (src_id, _NOW),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-e2e-thin-1', 'В центральной части города аварийное отключение электроэнергии.')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'В центральной части города аварийное отключение электроэнергии.', 'hf-e2e-thin-1', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, _NOW),
    )
    frag_id = (await cur.fetchone())[0]

    event_payload = {
        "topic": "Электроснабжение",
        "headline": "Отключение электроэнергии в центре",
        "digest_summary": "В центральной части города аварийное отключение электроэнергии.",
        "evidence_items": [
            {
                "text": "В центральной части города аварийное отключение электроэнергии.",
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
        ) VALUES (%s, 1, 'open', %s, 'h-rev-thin-1', %s, %s, %s, %s)
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
        request_key="test-e2e-thin-run",
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
        schedule_time="08:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4",
        openai_temperature=0.7,
        ai_provider="openai",
        # Default config has article_min_words=800
        publication_editorial=PublicationEditorialConfig(),
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

    # Create a valid safe draft of ~200 words (between 180 and 800)
    body_text = (
        "В центральной части города зафиксировано аварийное отключение электроэнергии. " * 25
    )
    mock_provider.chat_completion.return_value = json.dumps(
        {
            "title": "Аварийное отключение электроэнергии в центральной части города",
            "title_support_ids": [sup_id],
            "title_claims": [
                {
                    "text": "В центральной части города аварийное отключение электроэнергии.",
                    "cited_support_ids": [sup_id],
                }
            ],
            "lead": "В центральной части города аварийное отключение электроэнергии.",
            "lead_support_ids": [sup_id],
            "lead_claims": [
                {
                    "text": "В центральной части города аварийное отключение электроэнергии.",
                    "cited_support_ids": [sup_id],
                }
            ],
            "sections": [
                {
                    "heading": "Ситуация с электроснабжением в центре",
                    "heading_support_ids": [sup_id],
                    "heading_claims": [
                        {
                            "text": "В центральной части города аварийное отключение электроэнергии.",
                            "cited_support_ids": [sup_id],
                        }
                    ],
                    "paragraphs": [
                        {
                            "text": body_text,
                            "cited_support_ids": [sup_id],
                            "claims": [
                                {
                                    "text": "В центральной части города аварийное отключение электроэнергии.",
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

    cur = await conn.execute(
        """
        SELECT kind, status, metadata
        FROM publication_generation_attempts
        WHERE publication_run_id = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (run.id,),
    )
    row = await cur.fetchone()
    assert row[0] == "writer"
    assert row[1] == "succeeded"
