"""Repository tests for durable Event-First retry state."""

from __future__ import annotations

import datetime as dt

from src.repositories.event_retries import EventProcessingRetryRepository


async def _create_story(repo_conn) -> int:
    edition = await (
        await repo_conn.execute(
            """
            INSERT INTO editions (slug, name)
            VALUES ('retry-test', 'Retry Test')
            RETURNING id
            """
        )
    ).fetchone()
    story = await (
        await repo_conn.execute(
            """
            INSERT INTO stories (edition_id, knowledge_source)
            VALUES (%s, 'event_first')
            RETURNING id
            """,
            (edition[0],),
        )
    ).fetchone()
    return int(story[0])


async def test_record_failure_increments_and_reads_assignment_state(repo_conn):
    story_id = await _create_story(repo_conn)
    repo = EventProcessingRetryRepository()
    now = dt.datetime.now(dt.timezone.utc)

    first = await repo.record_failure(
        repo_conn,
        story_id=story_id,
        latest_assignment_id=101,
        stage="triage",
        error_kind="server",
        next_retry_at=now,
        prompt_hash="hash-1",
    )
    second = await repo.record_failure(
        repo_conn,
        story_id=story_id,
        latest_assignment_id=101,
        stage="triage",
        error_kind="server",
        next_retry_at=None,
        exhausted=True,
        prompt_hash="hash-2",
    )

    assert first.attempt_count == 1
    assert second.attempt_count == 2
    assert second.exhausted_at is not None
    assert second.last_prompt_hash == "hash-2"
    found = await repo.get_for_assignments(
        repo_conn, [(story_id, 101), (story_id, 202)], stage="triage"
    )
    assert found[(story_id, 101)] == second
    assert (story_id, 202) not in found


async def test_new_assignment_has_independent_retry_budget(repo_conn):
    story_id = await _create_story(repo_conn)
    repo = EventProcessingRetryRepository()

    await repo.record_failure(
        repo_conn,
        story_id=story_id,
        latest_assignment_id=101,
        stage="analysis",
        error_kind="token_budget",
        next_retry_at=None,
        exhausted=True,
    )
    fresh = await repo.record_failure(
        repo_conn,
        story_id=story_id,
        latest_assignment_id=202,
        stage="analysis",
        error_kind="token_budget",
        next_retry_at=None,
    )

    assert fresh.attempt_count == 1
    assert fresh.exhausted_at is None


async def test_clear_removes_successful_assignment_state(repo_conn):
    story_id = await _create_story(repo_conn)
    repo = EventProcessingRetryRepository()
    await repo.record_failure(
        repo_conn,
        story_id=story_id,
        latest_assignment_id=101,
        stage="triage",
        error_kind="other",
        next_retry_at=None,
    )

    await repo.clear(repo_conn, story_id=story_id, latest_assignment_id=101, stage="triage")
    assert await repo.get_for_assignments(repo_conn, [(story_id, 101)], stage="triage") == {}
