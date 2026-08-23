"""Tests for completeness-oriented Facebook comments collection and persistence (Plan 5 Task 4)."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest

from src.config_loader import FacebookCommentsConfig
from src.domain.sources import Source
from src.ingestion.repository import IngestionRepository
from src.ingestion.service import IngestionService
from src.providers.facebook.comments import (
    CommentCollectionBatch,
    FacebookCommentCollector,
    FacebookCommentRefreshService,
    parse_comment_from_data,
)
from src.repositories.facebook import FacebookRepository


def _make_source() -> Source:
    now = dt.datetime.now(dt.timezone.utc)
    return Source(
        id=1,
        platform="facebook",
        kind="group",
        external_id="https://facebook.com/groups/1",
        url="https://facebook.com/groups/1",
        name="Test Group",
        role="community",
        enabled=True,
        collector_options={},
        created_at=now,
        updated_at=now,
    )


class TestCommentParsingAndLinkage:
    """Tests for parent_external_id and root_external_id linkages in comments/replies."""

    def test_top_level_comment_links_to_post(self):
        source = _make_source()
        item, assets = parse_comment_from_data(
            source=source,
            post_external_id="post:1001",
            comment_id="2001",
            text="First comment",
            author_name="Commenter A",
        )

        assert item.kind == "facebook_comment"
        assert item.external_id == "comment:2001"
        assert item.parent_external_id == "post:1001"
        assert item.root_external_id == "post:1001"

    def test_nested_reply_links_to_parent_comment_and_root_post(self):
        source = _make_source()
        item, assets = parse_comment_from_data(
            source=source,
            post_external_id="post:1001",
            comment_id="3001",
            parent_comment_id="2001",
            text="Reply to first comment",
            author_name="Commenter B",
        )

        assert item.kind == "facebook_comment"
        assert item.external_id == "comment:3001"
        assert item.parent_external_id == "comment:2001"
        assert item.root_external_id == "post:1001"


class TestCommentCompletenessClassification:
    """Tests for stop reasons and partial vs complete classifications."""

    @pytest.mark.asyncio
    async def test_stops_on_max_comments_limit(self):
        source = _make_source()
        collector = FacebookCommentCollector()
        limits = FacebookCommentsConfig(max_comments_per_post=2, max_pages_per_refresh=10)

        # Mock page returning 5 comment nodes
        page = MagicMock()
        node = MagicMock()
        node.inner_text = AsyncMock(
            side_effect=["Comment 1", "Comment 2", "Comment 3", "Comment 4", "Comment 5"]
        )
        node.query_selector_all = AsyncMock(return_value=[])

        page.query_selector_all = AsyncMock(
            side_effect=[
                [node, node, node, node, node],  # first call to comment_nodes
                [],  # more buttons
            ]
        )

        batch = await collector.scan_post_with_page(
            source=source,
            post_item_id=10,
            post_external_id="post:1001",
            page=page,
            limits=limits,
        )

        assert batch.completeness == "partial"
        assert batch.stop_reason == "max_comments"
        assert len(batch.items) == 2

    @pytest.mark.asyncio
    async def test_stops_on_max_pages_limit(self):
        source = _make_source()
        collector = FacebookCommentCollector()
        limits = FacebookCommentsConfig(max_comments_per_post=50, max_pages_per_refresh=1)

        page = MagicMock()
        node = MagicMock()
        node.inner_text = AsyncMock(return_value="Comment A")
        node.query_selector_all = AsyncMock(return_value=[])
        button = MagicMock()
        button.click = AsyncMock()

        page.query_selector_all = AsyncMock(
            side_effect=[
                [node],  # first page comments
                [button],  # more buttons exist
                [node],  # second page comments
            ]
        )
        page.wait_for_timeout = AsyncMock()

        batch = await collector.scan_post_with_page(
            source=source,
            post_item_id=10,
            post_external_id="post:1001",
            page=page,
            limits=limits,
        )

        assert batch.completeness == "partial"
        assert batch.stop_reason == "max_pages"


@pytest.mark.postgres
class TestCommentRefreshServiceIntegration:
    """Integration test for atomic persistence of comments and comment collection state."""

    async def test_refresh_batch_persists_comments_and_updates_state(
        self, conn: psycopg.AsyncConnection, uow, edition
    ):
        fb_repo = FacebookRepository()
        ingestion_repo = IngestionRepository()
        ingestion_service = IngestionService(uow=uow, repo=ingestion_repo)
        collector = FacebookCommentCollector(fb_repo=fb_repo)
        service = FacebookCommentRefreshService(
            uow=uow,
            fb_repo=fb_repo,
            ingestion_service=ingestion_service,
            collector=collector,
        )

        # 1. Create a source and post item in DB
        now = dt.datetime.now(dt.timezone.utc)
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role, enabled)
            VALUES ('facebook', 'group', 'https://facebook.com/groups/1', 'https://facebook.com/groups/1', 'Group 1', 'community', true)
            RETURNING id
            """
        )
        source_id = (await cur.fetchone())[0]

        cur = await conn.execute(
            """
            INSERT INTO source_items (source_id, kind, external_id, published_at, first_collected_at)
            VALUES (%s, 'facebook_post', 'post:1001', %s, %s)
            RETURNING id
            """,
            (source_id, now, now),
        )
        post_item_id = (await cur.fetchone())[0]
        await fb_repo.get_or_create_comment_state(conn, source_item_id=post_item_id)
        await conn.commit()

        # 2. Build comment batch
        source = Source(
            id=source_id,
            platform="facebook",
            kind="group",
            external_id="https://facebook.com/groups/1",
            url="https://facebook.com/groups/1",
            name="Group 1",
            role="community",
            enabled=True,
            collector_options={},
            created_at=now,
            updated_at=now,
        )
        item1, assets1 = parse_comment_from_data(
            source=source,
            post_external_id="post:1001",
            comment_id="c1",
            text="Comment 1",
            published_at=now,
        )
        item2, assets2 = parse_comment_from_data(
            source=source,
            post_external_id="post:1001",
            comment_id="c2",
            parent_comment_id="c1",
            text="Reply 1",
            published_at=now,
        )

        batch = CommentCollectionBatch(
            source_id=source_id,
            post_item_id=post_item_id,
            completeness="complete",
            stop_reason="exhausted",
            total_comments_observed=2,
            oldest_comment_published_at=now,
            newest_comment_published_at=now,
            items=[item1, item2],
            assets=[],
            state_events=[],
        )

        result = await service.refresh_batch(
            source_id=source_id,
            post_item_id=post_item_id,
            batch=batch,
        )

        assert result.ingestion.new_items == 2

        # Verify comment state in DB
        await conn.commit()
        state = await fb_repo.get_or_create_comment_state(conn, source_item_id=post_item_id)
        assert state.completeness == "complete"
        assert state.total_comments_observed == 2
        assert state.continuation_state.get("stop_reason") == "exhausted"
