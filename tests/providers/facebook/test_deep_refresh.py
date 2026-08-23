"""Tests for periodic Facebook deep sweep maintenance (Plan 5 Task 5)."""

import datetime as dt
from unittest.mock import patch

import psycopg
import pytest

from src.ingestion.enrichment import EnrichmentRequest
from src.repositories.facebook import FacebookRepository


@pytest.mark.postgres
class TestFacebookDeepSweep:
    """Tests periodic deep refresh task querying due posts and deferring deep requests."""

    async def test_deep_sweep_defers_requests_for_due_posts(
        self, conn: psycopg.AsyncConnection, edition
    ):
        from src.jobs.facebook import dispatch_facebook_deep_sweep

        fb_repo = FacebookRepository()
        now = dt.datetime.now(dt.timezone.utc)

        # 1. Insert a source
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role, enabled)
            VALUES ('facebook', 'group', 'https://facebook.com/groups/deep_test', 'https://facebook.com/groups/deep_test', 'Deep Test', 'community', true)
            RETURNING id
            """
        )
        source_id = (await cur.fetchone())[0]

        # 2. Insert two posts with revisions
        for pid in [101, 102]:
            cur = await conn.execute(
                """
                INSERT INTO source_items (source_id, kind, external_id, published_at, first_collected_at)
                VALUES (%s, 'facebook_post', %s, %s, %s)
                RETURNING id
                """,
                (source_id, f"post:{pid}", now, now),
            )
            item_id = (await cur.fetchone())[0]
            await conn.execute(
                """
                INSERT INTO source_item_revisions (source_item_id, revision_no, text_content, collected_at, content_hash)
                VALUES (%s, 1, 'Post content', %s, 'hash123')
                """,
                (item_id, now),
            )
            await fb_repo.get_or_create_comment_state(conn, source_item_id=item_id)

        await conn.commit()

        deferred_requests: list[EnrichmentRequest] = []

        async def fake_defer(request, priority=0):
            deferred_requests.append(request)
            return len(deferred_requests)

        with patch(
            "src.ingestion.enrichment.EnrichmentDispatcher.defer_without_domain_transaction",
            side_effect=fake_defer,
        ):
            await dispatch_facebook_deep_sweep(timestamp=int(now.timestamp()))

        assert len(deferred_requests) == 2
        for req in deferred_requests:
            assert req.kind == "facebook_comments"
            assert req.mode == "deep"
            assert req.metadata.get("post_item_id") in [1, 2, 3, 4, item_id]
