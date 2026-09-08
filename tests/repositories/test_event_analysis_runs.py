"""Repository tests for rich-analysis budget accounting."""

from __future__ import annotations

import datetime as dt
import uuid

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
    token = uuid.uuid4().hex
    source = await (
        await repo_conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, name)
            VALUES ('test', 'fixture', %s, 'Analysis Runs Source')
            RETURNING id
            """,
            (f"analysis-runs-source-{token}",),
        )
    ).fetchone()
    item = await (
        await repo_conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
            VALUES (%s, 'message', %s, %s)
            RETURNING id
            """,
            (source[0], f"analysis-runs-item-{token}", now),
        )
    ).fetchone()
    revision = await (
        await repo_conn.execute(
            """
            INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
            VALUES (%s, 1, %s, 'analysis runs fixture')
            RETURNING id
            """,
            (item[0], f"analysis-runs-content-{token}"),
        )
    ).fetchone()
    vector = await (
        await repo_conn.execute(
            """
            INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
            VALUES (%s, '[1, 0]'::vector, 'test', 2)
            RETURNING id
            """,
            (f"analysis-runs-vector-{token}",),
        )
    ).fetchone()
    fragment = await (
        await repo_conn.execute(
            """
            INSERT INTO source_fragments (
                source_item_revision_id, ordinal, text_content, normalized_hash,
                fragmenter_version, is_candidate
            ) VALUES (%s, 0, 'analysis runs fixture', %s, 'test', TRUE)
            RETURNING id
            """,
            (revision[0], f"analysis-runs-fragment-{token}"),
        )
    ).fetchone()
    embedding = await (
        await repo_conn.execute(
            """
            INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
            VALUES (%s, %s)
            RETURNING id
            """,
            (fragment[0], vector[0]),
        )
    ).fetchone()
    first_assignment = await (
        await repo_conn.execute(
            """
            INSERT INTO story_fragments (
                story_id, fragment_id, fragment_embedding_id, assignment_kind
            ) VALUES (%s, %s, %s, 'manual')
            RETURNING id
            """,
            (story_id, fragment[0], embedding[0]),
        )
    ).fetchone()

    second_fragment = await (
        await repo_conn.execute(
            """
            INSERT INTO source_fragments (
                source_item_revision_id, ordinal, text_content, normalized_hash,
                fragmenter_version, is_candidate
            ) VALUES (%s, 1, 'analysis runs fixture 2', %s, 'test', TRUE)
            RETURNING id
            """,
            (revision[0], f"analysis-runs-fragment-2-{token}"),
        )
    ).fetchone()
    second_embedding = await (
        await repo_conn.execute(
            """
            INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
            VALUES (%s, %s)
            RETURNING id
            """,
            (second_fragment[0], vector[0]),
        )
    ).fetchone()
    second_assignment = await (
        await repo_conn.execute(
            """
            INSERT INTO story_fragments (
                story_id, fragment_id, fragment_embedding_id, assignment_kind
            ) VALUES (%s, %s, %s, 'manual')
            RETURNING id
            """,
            (story_id, second_fragment[0], second_embedding[0]),
        )
    ).fetchone()
    assignments = [int(first_assignment[0]), int(second_assignment[0])]
    for status in ("failed", "succeeded"):
        await repo_conn.execute(
            """
            INSERT INTO story_event_analysis_runs (
                story_id, latest_assignment_id, analysis_version, provider, model,
                prompt_hash, input_fragment_count, input_chars, status, completed_at
            ) VALUES (%s, %s, 'v-test', 'test', 'test', 'hash', 1, 10, %s, %s)
            """,
            (story_id, assignments[0] if status == "failed" else assignments[1], status, now),
        )

    assert (
        await EventAnalysisRunRepository().count_calls_since(
            repo_conn, story_id=story_id, since=now - dt.timedelta(minutes=1)
        )
        == 2
    )
