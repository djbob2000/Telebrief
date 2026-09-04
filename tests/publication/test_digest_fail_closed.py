from __future__ import annotations

import datetime as dt
import json
import logging
from unittest.mock import AsyncMock, patch

import psycopg
import pytest

from src.article_generator import ArticleGenerator
from src.config_loader import Config, PublicationEditorialConfig, Settings
from src.db.uow import DatabaseUnitOfWork
from src.publication.errors import PublicationGenerationError
from src.publication.generation import PublicationGenerationService
from src.publication.models import PublicationSelectionDecision
from src.publication.policies import PublicationPolicyRepository
from src.publication.repository import PublicationRepository

pytestmark = pytest.mark.unit


def test_fail_closed_raises_error() -> None:
    # Verify PublicationGenerationError has correct hierarchy
    err = PublicationGenerationError("Journalistic digest generation failed")
    assert isinstance(err, RuntimeError)
    assert "Journalistic digest generation failed" in str(err)


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
async def test_journalistic_digest_writer_exception_fails_closed(conn, pool, edition) -> None:
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_ids = await _seed_policies(conn, edition.id)

    now = dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.timezone.utc)
    cur = await conn.execute(
        "INSERT INTO sources (platform, kind, external_id, url, name, role) VALUES ('telegram', 'channel', 'c1-fail-close', 'https://t.me/c1', 'Chan', 'official') RETURNING id"
    )
    source_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (source_id, edition.id),
    )

    cur = await conn.execute(
        "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', 'm1', %s) RETURNING id",
        (source_id, now),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h1', 'Отключение света в центре') RETURNING id",
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'Отключение света в центре', 'hf1', 'v1', TRUE, %s) RETURNING id",
        (sir_id, now),
    )
    frag_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at) VALUES (%s, 'active', 'event_first', %s) RETURNING id",
        (edition.id, now),
    )
    sid = (await cur.fetchone())[0]

    payload = {
        "event_id": f"story:{sid}",
        "schema_version": "v3",
        "story_id": sid,
        "headline": "Отключение света",
        "digest_summary": "Света нет в центре.",
        "category": "utilities",
        "tags": ["жкх", "свет"],
        "evidence_items": [
            {
                "evidence_id": f"story:{sid}:evidence:0:frag:{frag_id}",
                "source_fragment_ids": [frag_id],
                "kind": "service_access",
                "text": "Отключение света в центре",
                "source_text": "Отключение света в центре",
                "publication_use": "PUBLISH",
            }
        ],
        "operational_observations": [
            {
                "subject_key": "power",
                "subject_label": "Электроснабжение",
                "dimension": "availability",
                "state": "UNAVAILABLE",
                "detail": "Отключение света в центре",
                "location": "Центр",
                "source_fragment_ids": [frag_id],
                "observed_at": now.isoformat(),
            }
        ],
    }

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-rev-fc', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            sid,
            payload["digest_summary"],
            payload["headline"],
            payload["digest_summary"],
            json.dumps(payload),
            now,
        ),
    )
    rev_id = (await cur.fetchone())[0]

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        request_key="test-key-fail-closed-1",
        snapshot_at=now,
        policy_ids=policy_ids,
    )
    cand = await repo.insert_candidate(
        conn,
        run.id,
        story_id=sid,
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
            confidence=0.95,
            reason="Power update",
            rank=1,
            metadata={},
            created_at=now,
        ),
    )
    await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=sid,
        story_revision_id=rev_id,
        selection_decision_id=dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[frag_id],
    )
    await repo.transition_run(conn, run.id, "selected_inputs_sealed")

    editorial_cfg = PublicationEditorialConfig(
        digest_narrative_mode="journalistic",
        digest_city_situation_max_items=5,
        digest_city_situation_max_details_per_item=2,
    )
    settings = Settings(
        schedule_time="09:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4",
        openai_temperature=0.7,
        publication_editorial=editorial_cfg,
    )
    config = Config(
        channels=[],
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
        settings=settings,
    )
    generator = ArticleGenerator(config=config, logger=logging.getLogger("test"))
    service = PublicationGenerationService(
        uow=uow,
        config=config,
        repo=repo,
        generator=generator,
    )

    with patch(
        "src.publication.digest_narrative.DigestNarrativeWriter.generate_journalistic_digest",
        new_callable=AsyncMock,
    ) as mock_generate:
        mock_generate.side_effect = RuntimeError("AI synthesis timeout")

        with pytest.raises(
            PublicationGenerationError, match="Journalistic digest generation failed"
        ):
            await service.generate(run.id, defer_delivery=True)

    # Verify no publication was created
    pub = await repo.get_publication_by_run_id(conn, run.id)
    assert pub is None

    # Verify no fallback attempt was started
    cur = await conn.execute(
        "SELECT kind FROM publication_generation_attempts WHERE publication_run_id = %s",
        (run.id,),
    )
    kinds = [r[0] for r in await cur.fetchall()]
    assert "story_renderer_fallback" not in kinds


@pytest.mark.postgres
async def test_journalistic_digest_empty_candidate_fails_closed(conn, pool, edition) -> None:
    uow = DatabaseUnitOfWork(pool)
    repo = PublicationRepository()
    policy_ids = await _seed_policies(conn, edition.id)

    now = dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.timezone.utc)
    cur = await conn.execute(
        "INSERT INTO sources (platform, kind, external_id, url, name, role) VALUES ('telegram', 'channel', 'c1-fail-close-2', 'https://t.me/c1', 'Chan', 'official') RETURNING id"
    )
    source_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (source_id, edition.id),
    )

    cur = await conn.execute(
        "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', 'm2', %s) RETURNING id",
        (source_id, now),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h2', 'Отключение света в центре') RETURNING id",
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'Отключение света в центре', 'hf2', 'v1', TRUE, %s) RETURNING id",
        (sir_id, now),
    )
    frag_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at) VALUES (%s, 'active', 'event_first', %s) RETURNING id",
        (edition.id, now),
    )
    sid = (await cur.fetchone())[0]

    payload = {
        "event_id": f"story:{sid}",
        "schema_version": "v3",
        "story_id": sid,
        "headline": "Отключение света",
        "digest_summary": "Света нет в центре.",
        "category": "utilities",
        "tags": ["жкх", "свет"],
        "evidence_items": [
            {
                "evidence_id": f"story:{sid}:evidence:0:frag:{frag_id}",
                "source_fragment_ids": [frag_id],
                "kind": "service_access",
                "text": "Отключение света в центре",
                "source_text": "Отключение света в центре",
                "publication_use": "PUBLISH",
            }
        ],
    }

    cur = await conn.execute(
        """
        INSERT INTO story_revisions (
            story_id, revision_no, current_state, semantic_text, content_hash,
            title, summary, event_payload, created_at
        ) VALUES (%s, 1, 'open', %s, 'h-rev-fc2', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            sid,
            payload["digest_summary"],
            payload["headline"],
            payload["digest_summary"],
            json.dumps(payload),
            now,
        ),
    )
    rev_id = (await cur.fetchone())[0]

    run = await repo.get_or_create_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        request_key="test-key-fail-closed-2",
        snapshot_at=now,
        policy_ids=policy_ids,
    )
    cand = await repo.insert_candidate(
        conn,
        run.id,
        story_id=sid,
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
            confidence=0.95,
            reason="Power update",
            rank=1,
            metadata={},
            created_at=now,
        ),
    )
    await repo.freeze_selected_input(
        conn,
        run.id,
        story_id=sid,
        story_revision_id=rev_id,
        selection_decision_id=dec.id,
        presentation_intent="lead",
        rank=1,
        fragment_ids=[frag_id],
    )
    await repo.transition_run(conn, run.id, "selected_inputs_sealed")

    editorial_cfg = PublicationEditorialConfig(
        digest_narrative_mode="journalistic",
        digest_city_situation_max_items=5,
        digest_city_situation_max_details_per_item=2,
    )
    settings = Settings(
        schedule_time="09:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4",
        openai_temperature=0.7,
        publication_editorial=editorial_cfg,
    )
    config = Config(
        channels=[],
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
        settings=settings,
    )
    generator = ArticleGenerator(config=config, logger=logging.getLogger("test"))
    service = PublicationGenerationService(
        uow=uow,
        config=config,
        repo=repo,
        generator=generator,
    )

    with patch(
        "src.publication.digest_narrative.DigestNarrativeWriter.generate_journalistic_digest",
        new_callable=AsyncMock,
    ) as mock_generate:
        # Returns empty text and None draft
        mock_generate.return_value = ("", None)

        with pytest.raises(
            PublicationGenerationError,
            match="Journalistic digest generation failed: AI writer was unable to produce a valid draft",
        ):
            await service.generate(run.id, defer_delivery=True)

    # Verify no publication was created
    pub = await repo.get_publication_by_run_id(conn, run.id)
    assert pub is None
