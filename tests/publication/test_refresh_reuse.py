from __future__ import annotations

import datetime as dt

import pytest

from src.publication.readiness_repository import PublicationReadinessRepository

UTC = dt.timezone.utc


async def _source(conn, external_id: str) -> int:
    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, name)
        VALUES ('telegram', 'channel', %s, %s)
        RETURNING id
        """,
        (external_id, external_id),
    )
    return int((await cursor.fetchone())[0])


@pytest.mark.postgres
async def test_frozen_digest_refresh_can_be_reused_for_article_when_snapshot_is_fresh(
    conn,
    edition,
) -> None:
    repository = PublicationReadinessRepository()
    now = dt.datetime(2026, 9, 13, 19, 30, tzinfo=UTC)
    snapshot_at = now - dt.timedelta(minutes=1)
    freshness_cutoff_at = now - dt.timedelta(minutes=2)
    source_id = await _source(conn, "reuse-test-source")

    refresh = await repository.get_or_create_refresh_run(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        slot_at=now,
        requested_at=now,
        trigger="manual",
        request_key="reuse-test-source-refresh",
        freshness_cutoff_at=freshness_cutoff_at,
        deadline_at=now + dt.timedelta(minutes=20),
        requested_by_user_id=None,
        source_ids=[source_id],
    )
    await conn.execute(
        """
        UPDATE publication_refresh_runs
        SET status = 'publication_queued',
            collection_ready_at = %s,
            processing_ready_at = %s,
            knowledge_snapshot_at = %s
        WHERE id = %s
        """,
        (now, now, snapshot_at, refresh.id),
    )

    reusable = await repository.find_reusable_frozen_refresh(
        conn,
        edition_id=edition.id,
        freshness_cutoff_at=freshness_cutoff_at,
        target_at=now,
        lookback_hours=24,
    )
    assert reusable is not None
    assert reusable.id == refresh.id

    article_refresh = await repository.create_reused_refresh_run(
        conn,
        source_refresh=reusable,
        publication_type="daily_article",
        slot_at=now,
        requested_at=now,
        trigger="manual",
        request_key="reuse-test-article-refresh",
        freshness_cutoff_at=freshness_cutoff_at,
        deadline_at=now + dt.timedelta(minutes=20),
        requested_by_user_id=None,
        lookback_hours=24,
    )

    assert article_refresh.status == "ready_for_preparation"
    assert article_refresh.knowledge_snapshot_at == snapshot_at
    assert article_refresh.metadata["reused_from_refresh_run_id"] == refresh.id
