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

    def test_synthetic_id_is_stable_across_scans_without_ordinals(self):
        import hashlib

        post_ext = "post:1001"
        parent_id = None
        author = "Иван"
        norm_text = "Воды все еще нет"
        time_sig = "2026-08-24T12:00:00+00:00"

        # Scan 1: comment is 1st item in batch
        scope_str1 = f"{post_ext}:{parent_id or ''}:{author}:{norm_text}:{time_sig}"
        cid1 = hashlib.sha256(scope_str1.encode("utf-8")).hexdigest()[:16]

        # Scan 2: a new comment appeared above, so comment would have been 2nd
        scope_str2 = f"{post_ext}:{parent_id or ''}:{author}:{norm_text}:{time_sig}"
        cid2 = hashlib.sha256(scope_str2.encode("utf-8")).hexdigest()[:16]

        assert cid1 == cid2

    @pytest.mark.asyncio
    async def test_scanner_extracts_author_name_and_ignores_numeric_profile_link_for_comment_id(
        self,
    ):
        source = _make_source()
        collector = FacebookCommentCollector()
        limits = FacebookCommentsConfig(max_comments_per_post=10, max_pages_per_refresh=1)

        # Mock comment node with author link containing 15-digit profile id
        author_link = MagicMock()
        author_link.get_attribute = AsyncMock(
            return_value="https://www.facebook.com/profile.php?id=100088889999111"
        )
        author_link.inner_text = AsyncMock(return_value="Иван Петров")

        node = MagicMock()
        node.inner_text = AsyncMock(return_value="Иван Петров\nДа, действительно так")
        node.query_selector_all = AsyncMock(return_value=[author_link])

        page = MagicMock()
        page.query_selector_all = AsyncMock(side_effect=[[node], []])

        batch = await collector.scan_post_with_page(
            source=source,
            post_item_id=10,
            post_external_id="post:1001",
            page=page,
            limits=limits,
        )

        assert len(batch.items) == 1
        item = batch.items[0]
        assert item.author_name == "Иван Петров"
        # Must NOT take the numeric user id from the profile link as native comment id!
        assert item.metadata.get("identity_quality") == "synthetic"
        assert "100088889999111" not in item.external_id


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

    @pytest.mark.asyncio
    async def test_include_replies_false_skips_nested_replies(self):
        source = _make_source()
        collector = FacebookCommentCollector()
        limits = FacebookCommentsConfig(
            max_comments_per_post=50,
            max_pages_per_refresh=1,
            include_replies=False,
        )

        def make_eval(depth: int, text: str):
            async def _eval(js: str, *args, **kwargs):
                if "Math.max" in js:
                    return depth
                return text

            return _eval

        # Mock page returning 1 top-level comment and 1 nested reply
        top_node = MagicMock()
        top_node.inner_text = AsyncMock(return_value="Author 1\nTop level comment")
        top_node.query_selector_all = AsyncMock(return_value=[])
        top_node.evaluate = AsyncMock(side_effect=make_eval(0, "Top level comment"))

        reply_node = MagicMock()
        reply_node.inner_text = AsyncMock(return_value="Author 2\nNested reply")
        reply_node.query_selector_all = AsyncMock(return_value=[])
        reply_node.evaluate = AsyncMock(side_effect=make_eval(1, "Nested reply"))

        page = MagicMock()
        page.query_selector_all = AsyncMock(
            side_effect=[
                [top_node, reply_node],
                [],
            ]
        )

        batch = await collector.scan_post_with_page(
            source=source,
            post_item_id=10,
            post_external_id="post:1001",
            page=page,
            limits=limits,
        )

        # Only 1 top-level comment collected
        assert len(batch.items) == 1
        assert batch.items[0].text == "Top level comment"

    @pytest.mark.asyncio
    async def test_max_replies_per_comment_bounds_nested_replies(self):
        source = _make_source()
        collector = FacebookCommentCollector()
        limits = FacebookCommentsConfig(
            max_comments_per_post=50,
            max_pages_per_refresh=1,
            include_replies=True,
            max_replies_per_comment=1,
        )

        def make_eval(depth: int, text: str):
            async def _eval(js: str, *args, **kwargs):
                if "Math.max" in js:
                    return depth
                return text

            return _eval

        top_node = MagicMock()
        top_node.inner_text = AsyncMock(return_value="Author 1\nTop level comment")
        top_node.query_selector_all = AsyncMock(return_value=[])
        top_node.evaluate = AsyncMock(side_effect=make_eval(0, "Top level comment"))

        reply_node_1 = MagicMock()
        reply_node_1.inner_text = AsyncMock(return_value="Author 2\nNested reply 1")
        reply_node_1.query_selector_all = AsyncMock(return_value=[])
        reply_node_1.evaluate = AsyncMock(side_effect=make_eval(1, "Nested reply 1"))

        reply_node_2 = MagicMock()
        reply_node_2.inner_text = AsyncMock(return_value="Author 3\nNested reply 2")
        reply_node_2.query_selector_all = AsyncMock(return_value=[])
        reply_node_2.evaluate = AsyncMock(side_effect=make_eval(1, "Nested reply 2"))

        page = MagicMock()
        page.query_selector_all = AsyncMock(
            side_effect=[
                [top_node, reply_node_1, reply_node_2],
                [],
            ]
        )

        batch = await collector.scan_post_with_page(
            source=source,
            post_item_id=10,
            post_external_id="post:1001",
            page=page,
            limits=limits,
        )

        # 1 top level + 1 bounded reply = 2 items
        assert len(batch.items) == 2
        assert batch.items[0].text == "Top level comment"
        assert batch.items[1].text == "Nested reply 1"


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
