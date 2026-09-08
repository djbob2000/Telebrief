"""Repository tests for rich-analysis budget accounting."""

from __future__ import annotations

import datetime as dt

from src.repositories.event_analysis_runs import EventAnalysisRunRepository


async def test_count_calls_since_includes_failed_and_successful_runs(repo_conn):
    edition = await (
        await repo_conn.execute(
            "INSERT INTO editions (slug, name) VALUES ('analysis-runs', 'Analysis Runs') RETURNING id"
        )
    ).fetchone()
    story = await (
        await repo_conn.execute(
            "INSERT INTO stories (edition_id, knowledge_source) VALUES (%s, 'event_first') RETURNING id",
            (edition[0],),
        )
    ).fetchone()
    story_id = int(story[0])
    now = dt.datetime.now(dt.timezone.utc)
    for status in ("failed", "succeeded"):
        await repo_conn.execute(
            """
            INSERT INTO story_event_analysis_runs (
                story_id, latest_assignment_id, analysis_version, provider, model,
                prompt_hash, input_fragment_count, input_chars, status, completed_at
            ) VALUES (%s, %s, 'v-test', 'test', 'test', 'hash', 1, 10, %s, %s)
            """,
            (story_id, 100 if status == "failed" else 101, status, now),
        )

    assert (
        await EventAnalysisRunRepository().count_calls_since(
            repo_conn, story_id=story_id, since=now - dt.timedelta(minutes=1)
        )
        == 2
    )
