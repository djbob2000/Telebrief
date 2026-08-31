"""Tests for scripts/rescreen_stories.py CLI."""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock

import pytest

from scripts.rescreen_stories import run_rescreen

_NOW = dt.datetime.now(dt.timezone.utc)


@pytest.mark.postgres
async def test_run_rescreen_end_to_end(conn, pool, edition, database_config, monkeypatch):
    test_db_url = database_config.url
    now = dt.datetime.now(dt.timezone.utc)

    # Insert test active story and fragments
    cur = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s)
        RETURNING id
        """,
        (edition.id, now),
    )
    story_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-10099', 'https://t.me/r', 'R') RETURNING id"
    )
    src_id = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (src_id, edition.id),
    )
    cur = await conn.execute(
        "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'msg', 'm-rescr', %s) RETURNING id",
        (src_id, now),
    )
    item_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h-rescr', 'Водоканал ремонтирует трубу на АКЗ') RETURNING id",
        (item_id,),
    )
    sir_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'Водоканал ремонтирует трубу на АКЗ', 'h-frescr', 'v1', TRUE, %s) RETURNING id",
        (sir_id, now),
    )
    frag_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)

        VALUES ('h-frescr', '[0.1, 0.2]'::vector, 'text-embedding-3-small', 2)
        RETURNING id
        """
    )
    vec_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
        VALUES (%s, %s)
        RETURNING id
        """,
        (frag_id, vec_id),
    )
    sfe_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO story_fragments (story_id, fragment_id, fragment_embedding_id, assignment_kind, assigned_at)
        VALUES (%s, %s, %s, 'new_story', %s)
        RETURNING id
        """,
        (story_id, frag_id, sfe_id, now),
    )
    assignment_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        INSERT INTO story_cluster_state (
            story_id, centroid, model, dimensions, fragment_count,
            unique_source_count, first_seen_at, last_seen_at,
            latest_assignment_id, analysis_dirty
        ) VALUES (%s, '[0.1, 0.2]'::vector, 'text-embedding-3-small', 2, 1, 1, %s, %s, %s, TRUE)
        """,
        (story_id, now, now, assignment_id),
    )

    ai_triage_response = {
        "results": [
            {
                "story_id": story_id,
                "scope": "LOCAL",
                "scope_basis_fragment_ids": [frag_id],
                "scope_confidence": 0.95,
                "scope_reason": "Local utility update",
                "confidence": 0.95,
                "reason": "Local update",
                "retention": "KEEP",
                "enrichment": "BRIEF",
                "brief_payload": {
                    "topic": "Ремонт водовода",
                    "headline": "Ремонт водовода на АКЗ",
                    "digest_summary": "Специалисты устраняют порыв.",
                    "tags": ["utilities"],
                    "evidence_items": [
                        {
                            "text": "Водоканал ремонтирует трубу на АКЗ",
                            "kind": "service_access",
                            "publication_use": "PUBLISH",
                            "source_fragment_ids": [frag_id],
                            "service_state": {
                                "subject_key": "water_supply",
                                "subject_label": "Водоснабжение",
                                "dimension": "availability",
                                "state": "UNAVAILABLE",
                                "expected_now": True,
                                "basis": "direct_failure",
                            },
                        }
                    ],
                    "confidence_score": 0.95,
                },
            }
        ]
    }

    mock_ai = AsyncMock()
    mock_ai.generate_text = AsyncMock(return_value=json.dumps(ai_triage_response))
    mock_ai.provider_name = "test_provider"
    mock_ai.model_name = "test_model"

    await conn.commit()

    stats = await run_rescreen(
        hours=24,
        edition_slug=edition.slug,
        batch_size=10,
        dry_run=False,
        override_database_url=test_db_url,
        custom_ai_provider=mock_ai,
    )

    assert stats["total_screened"] == 1
    assert stats["scope"]["LOCAL"] == 1
    assert stats["retention"]["KEEP_BRIEF"] == 1

    # Verify decision persistence and brief creation in DB
    cur = await conn.execute(
        "SELECT retention, enrichment FROM story_event_triage_decisions WHERE story_id = %s",
        (story_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "KEEP"
    assert row[1] == "BRIEF"

    # Verify story revision was created with brief
    cur = await conn.execute(
        "SELECT title, summary FROM story_revisions WHERE story_id = %s",
        (story_id,),
    )
    rev_row = await cur.fetchone()
    assert rev_row is not None
    assert rev_row[0] == "Ремонт водовода на АКЗ"
