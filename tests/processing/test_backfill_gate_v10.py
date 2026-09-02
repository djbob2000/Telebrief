"""Tests for targeted Gate v10 backfill with quiet-window bypass and stagnation guard (Task 3)."""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock, patch

import psycopg
import pytest

from scripts.backfill_gate_v10 import run_gate_v10_backfill
from src.config_loader import EditionScopeConfig
from src.publication.policies import PublicationPolicyService
from src.publication.repository import PublicationRepository

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


@pytest.mark.postgres
async def test_gate_v10_backfill_drains_authority_gap_with_quiet_window_bypass(
    conn: psycopg.AsyncConnection, pool, uow, sample_config
):
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    snapshot_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)
    sample_config.settings.event_pipeline = sample_config.settings.event_pipeline.__class__(
        mode="event_first",
        analysis_quiet_seconds=3600,  # 1 hour quiet window: would block normal processing
        analysis_min_interval_seconds=300,
        direct_analysis_min_fragments=1,
    )
    sample_config.settings.edition_scopes = {
        "berdyansk-backfill": EditionScopeConfig(
            name="Бердянск",
            focus_places=("Бердянск",),
        )
    }

    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk-backfill', 'Berdyansk Backfill') RETURNING id"
    )
    edition_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name)
        VALUES ('telegram', 'channel', '-1009988', 'https://t.me/testbf', 'Test BF')
        RETURNING id
        """
    )
    source_id = (await cursor.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (source_id, edition_id),
    )

    cursor = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', 'msg-bf-1', %s)
        RETURNING id
        """,
        (source_id, now),
    )
    item_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'h-sir-bf-1', 'В Бердянске пассажиры автобусов могут воспользоваться скидкой 8 рублей при безналичной оплате проезда.')
        RETURNING id
        """,
        (item_id,),
    )
    sir_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at)
        VALUES (%s, 0, 'В Бердянске пассажиры автобусов могут воспользоваться скидкой 8 рублей при безналичной оплате проезда.', 'h-frag-bf-1', 'v1', TRUE, %s)
        RETURNING id
        """,
        (sir_id, now),
    )
    frag_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
        VALUES ('h-frag-bf-1', '[1, 0]'::vector, 'm', 2)
        ON CONFLICT (normalized_hash, model, dimensions) DO UPDATE SET embedding = EXCLUDED.embedding
        RETURNING id
        """
    )
    vec_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
        VALUES (%s, %s) RETURNING id
        """,
        (frag_id, vec_id),
    )
    sfe_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at)
        VALUES (%s, 'active', 'event_first', %s) RETURNING id
        """,
        (edition_id, now),
    )
    story_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        """
        INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
        VALUES (%s, 1, 'active', 'Скидка на проезд', 'h-srev-1', %s) RETURNING id
        """,
        (story_id, now),
    )
    srev_id = (await cursor.fetchone())[0]
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s", (srev_id, story_id)
    )

    cursor = await conn.execute(
        """
        INSERT INTO story_fragments (story_id, fragment_id, fragment_embedding_id, assignment_kind)
        VALUES (%s, %s, %s, 'new_story') RETURNING id
        """,
        (story_id, frag_id, sfe_id),
    )
    aid = (await cursor.fetchone())[0]

    await conn.execute(
        """
        INSERT INTO story_cluster_state (story_id, centroid, model, dimensions, fragment_count, unique_source_count, first_seen_at, last_seen_at, latest_assignment_id, analysis_dirty)
        VALUES (%s, '[1, 0]'::vector, 'm', 2, 1, 1, %s, %s, %s, FALSE)
        """,
        (story_id, now, now, aid),
    )

    # 1. Verify this story is in authority gap initially
    policy_service = PublicationPolicyService()
    policy_set = await policy_service.ensure_current(
        conn, edition_id=edition_id, publication_type="digest_grouped", config=sample_config
    )
    repo = PublicationRepository()
    gap_ids = await repo.find_authority_gap_story_ids(
        conn,
        edition_id=edition_id,
        snapshot_at=snapshot_at,
        eligibility_policy_id=policy_set.eligibility.id,
    )
    assert story_id in gap_ids

    # Setup infrastructure mock
    triage_output = [
        {
            "story_id": story_id,
            "scope": "LOCAL",
            "scope_confidence": 0.99,
            "scope_reason": "in city",
            "scope_basis_fragment_ids": [frag_id],
            "retention": "KEEP",
            "enrichment": "BRIEF",
            "confidence": 0.98,
            "reason": "Transport discount in Berdyansk",
            "brief_payload": {
                "topic": "Транспорт",
                "headline": "Скидка на проезд 8 рублей",
                "digest_summary": "В автобусах Бердянска действует скидка при безналичной оплате.",
                "publishability": "news",
                "confidence_score": 0.98,
                "evidence_items": [
                    {
                        "text": "Скидка на проезд составит 8 рублей при безналичной оплате",
                        "kind": "established_fact",
                        "publication_use": "PUBLISH",
                        "source_fragment_ids": [frag_id],
                    }
                ],
            },
        }
    ]
    mock_ai = AsyncMock()
    mock_ai.generate_text.return_value = json.dumps({"results": triage_output})

    from types import SimpleNamespace

    infra = SimpleNamespace(
        uow=uow,
        pool=pool,
        config=sample_config,
        provider_cascade=mock_ai,
        close=AsyncMock(),
    )

    result = await run_gate_v10_backfill(
        edition_id=edition_id,
        snapshot_at=snapshot_at,
        infrastructure=infra,
    )

    assert result["initial_gap"] == 1
    assert result["final_gap"] == 0
    assert result["rounds"] == 1

    # Verify story now has authoritative v10 triage decision with retention == 'KEEP'
    gap_after = await repo.find_authority_gap_story_ids(
        conn,
        edition_id=edition_id,
        snapshot_at=snapshot_at,
        eligibility_policy_id=policy_set.eligibility.id,
    )
    assert len(gap_after) == 0

    cur = await conn.execute(
        "SELECT triage_version, retention, enrichment FROM story_event_triage_decisions WHERE story_id = %s",
        (story_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "v10"
    assert row[1] == "KEEP"
    assert row[2] == "BRIEF"


@pytest.mark.postgres
async def test_gate_v10_backfill_stagnation_guard(
    conn: psycopg.AsyncConnection, pool, uow, sample_config
):
    """Stagnation (gap not reducing after coalesce round) triggers explicit RuntimeError."""
    now = _NOW
    cursor = await conn.execute(
        "INSERT INTO editions (slug, name) VALUES ('berdyansk-stag', 'Berdyansk Stag') RETURNING id"
    )
    edition_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        "INSERT INTO sources (platform, kind, external_id, url, name) VALUES ('telegram', 'channel', '-10011', 'https://t.me/t', 'T') RETURNING id"
    )
    source_id = (await cursor.fetchone())[0]
    await conn.execute(
        "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
        (source_id, edition_id),
    )

    cursor = await conn.execute(
        "INSERT INTO source_items (source_id, kind, external_id, first_collected_at) VALUES (%s, 'message', 'm-1', %s) RETURNING id",
        (source_id, now),
    )
    item_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content) VALUES (%s, 1, 'h-1', 'T') RETURNING id",
        (item_id,),
    )
    sir_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        "INSERT INTO source_fragments (source_item_revision_id, ordinal, text_content, normalized_hash, fragmenter_version, is_candidate, created_at) VALUES (%s, 0, 'T', 'h-f', 'v1', TRUE, %s) RETURNING id",
        (sir_id, now),
    )
    frag_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        "INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions) VALUES ('h-f', '[1, 0]'::vector, 'm', 2) ON CONFLICT DO NOTHING RETURNING id"
    )
    vec_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        "INSERT INTO source_fragment_embeddings (fragment_id, vector_id) VALUES (%s, %s) RETURNING id",
        (frag_id, vec_id),
    )
    sfe_id = (await cursor.fetchone())[0]

    cursor = await conn.execute(
        "INSERT INTO stories (edition_id, lifecycle_state, knowledge_source, created_at) VALUES (%s, 'active', 'event_first', %s) RETURNING id",
        (edition_id, now),
    )
    story_id = (await cursor.fetchone())[0]
    cursor = await conn.execute(
        "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) VALUES (%s, 1, 'active', 'T', 'h-r', %s) RETURNING id",
        (story_id, now),
    )
    srev_id = (await cursor.fetchone())[0]
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s", (srev_id, story_id)
    )
    cursor = await conn.execute(
        "INSERT INTO story_fragments (story_id, fragment_id, fragment_embedding_id, assignment_kind) VALUES (%s, %s, %s, 'new_story') RETURNING id",
        (story_id, frag_id, sfe_id),
    )
    aid = (await cursor.fetchone())[0]
    await conn.execute(
        "INSERT INTO story_cluster_state (story_id, centroid, model, dimensions, fragment_count, unique_source_count, first_seen_at, last_seen_at, latest_assignment_id, analysis_dirty) VALUES (%s, '[1, 0]'::vector, 'm', 2, 1, 1, %s, %s, %s, FALSE)",
        (story_id, now, now, aid),
    )

    from types import SimpleNamespace

    infra = SimpleNamespace(
        uow=uow,
        config=sample_config,
        close=AsyncMock(),
    )

    # Mock coalesce_dirty_stories_task to do nothing so gap doesn't reduce
    with patch(
        "scripts.backfill_gate_v10.coalesce_dirty_stories_task.func",
        new=AsyncMock(return_value={"scanned": 0}),
    ):
        with pytest.raises(RuntimeError, match="Backfill stagnation detected"):
            await run_gate_v10_backfill(
                edition_id=edition_id,
                snapshot_at=now,
                infrastructure=infra,
            )
