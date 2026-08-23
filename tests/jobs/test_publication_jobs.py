"""Tests for publication jobs in src/jobs/publication.py."""

from __future__ import annotations

import datetime as dt
import os
from unittest.mock import AsyncMock, patch

import psycopg
import pytest

from src.db.uow import DatabaseUnitOfWork
from src.jobs.publication import (
    select_stories_for_publication,
)
from src.publication.snapshot import PublicationSnapshotService

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)

pytestmark = pytest.mark.skipif(
    "TELEBRIEF_TEST_DATABASE_URL" not in os.environ,
    reason="TELEBRIEF_TEST_DATABASE_URL is not set",
)


@pytest.mark.postgres
async def test_select_stories_job_runs_with_fail_open_selection(
    conn: psycopg.AsyncConnection, pool, edition
):
    uow = DatabaseUnitOfWork(pool)
    snap_service = PublicationSnapshotService(uow=uow)

    cur = await conn.execute(
        "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
        (edition.id, _NOW),
    )
    story_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
        VALUES (%s, 1, 'open', 'Свежая новость', 'h-job-1', %s)
        RETURNING id
        """,
        (story_id, _NOW),
    )
    rev_id = (await cur.fetchone())[0]
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
    )

    run = await snap_service.create_run(
        edition_id=edition.id,
        publication_type="digest_grouped",
        snapshot_at=_NOW,
        request_key="test-job-select-run",
    )
    await snap_service.seal_candidates(run.id)

    # Execute job
    from types import SimpleNamespace

    from src import runtime

    runtime._runtime = SimpleNamespace(uow=uow)

    # Job executes select_stories_for_publication
    context = {}
    with patch("src.jobs.publication.generate_publication.configure") as mock_gen_conf:
        mock_task = AsyncMock()
        mock_gen_conf.return_value = mock_task
        await select_stories_for_publication(context, run.id)

    # Check that run transitioned to selected_inputs_sealed
    async with uow.transaction() as check_conn:
        cur = await check_conn.execute(
            "SELECT status FROM publication_runs WHERE id = %s", (run.id,)
        )
        status = (await cur.fetchone())[0]
        assert status == "selected_inputs_sealed"
