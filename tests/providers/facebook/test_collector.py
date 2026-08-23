"""Tests for Facebook post collection, semantic parsing, and execution locks (Plan 5 Task 3)."""

import datetime as dt
from unittest.mock import patch

import psycopg
import pytest

from src.domain.sources import Source
from src.ingestion.models import CollectionOutcome, CollectionTrigger
from src.jobs.facebook import resolve_facebook_execution_lock
from src.providers.facebook.auth import FacebookAuthState, FacebookHumanActionRequired
from src.providers.facebook.collector import (
    FacebookCollector,
    canonicalize_post_url,
    extract_post_id_from_url,
    parse_post_from_data,
)
from src.repositories.facebook import FacebookRepository


class TestFacebookPostParser:
    """Tests for post ID extraction, canonical URLs, and semantic item parsing."""

    @pytest.mark.parametrize(
        ("url", "expected_id"),
        [
            ("https://www.facebook.com/groups/12345/posts/9876543210/", "9876543210"),
            ("https://www.facebook.com/groups/berdyansk/permalink/1122334455/", "1122334455"),
            ("https://www.facebook.com/permalink.php?story_fbid=9988776655&id=123", "9988776655"),
            ("https://www.facebook.com/page_name/posts/5544332211", "5544332211"),
            ("https://www.facebook.com/page_name/videos/6677889900/", "6677889900"),
        ],
    )
    def test_extract_post_id_from_url(self, url: str, expected_id: str):
        assert extract_post_id_from_url(url) == expected_id

    def test_canonicalize_post_url(self):
        source_url = "https://www.facebook.com/groups/berdyansk_news"
        canonical = canonicalize_post_url(source_url, "98765")
        assert canonical == "https://www.facebook.com/groups/berdyansk_news/posts/98765/"

    def test_parse_post_from_data(self):
        now = dt.datetime.now(dt.timezone.utc)
        source = Source(
            id=1,
            platform="facebook",
            kind="group",
            external_id="https://facebook.com/groups/123",
            url="https://facebook.com/groups/123",
            name="Berdyansk Group",
            role="community",
            enabled=True,
            collector_options={},
            created_at=now,
            updated_at=now,
        )
        published_at = dt.datetime(2026, 8, 23, 10, 0, tzinfo=dt.timezone.utc)
        item, assets = parse_post_from_data(
            source=source,
            post_id="98765",
            text="Breaking local news in Berdyansk",
            author_name="Local Resident",
            author_id="user_123",
            published_at=published_at,
            media_urls=["https://facebook.com/photo1.jpg"],
        )

        assert item.kind == "facebook_post"
        assert item.external_id == "post:98765"
        assert item.canonical_url == "https://facebook.com/groups/123/posts/98765/"
        assert item.author_name == "Local Resident"
        assert item.published_at == published_at
        assert len(assets) == 1
        assert assets[0].mime_type == "image/jpeg"
        assert item.metadata["post_id"] == "98765"


class TestFacebookExecutionLock:
    """Tests cross-source execution lock resolver for Facebook auth profiles."""

    def test_resolve_facebook_execution_lock(self):
        now = dt.datetime.now(dt.timezone.utc)
        source = Source(
            id=42,
            platform="facebook",
            kind="group",
            external_id="https://facebook.com/groups/1",
            url="https://facebook.com/groups/1",
            name="Group 1",
            role="community",
            enabled=True,
            collector_options={"auth_profile": "operator_profile"},
            created_at=now,
            updated_at=now,
        )
        lock = resolve_facebook_execution_lock(source)
        assert lock == "facebook-auth-profile:operator_profile"


@pytest.mark.postgres
class TestFacebookCollectorIntegration:
    """Integration tests for FacebookCollector outcomes and database updates."""

    async def test_collector_handles_auth_checkpoint_and_updates_profile_status(
        self, conn: psycopg.AsyncConnection, edition
    ):
        fb_repo = FacebookRepository()
        prof = await fb_repo.get_or_create_auth_profile(
            conn, name="test_prof", storage_ref="test_prof"
        )
        await conn.commit()

        now = dt.datetime.now(dt.timezone.utc)
        source = Source(
            id=101,
            platform="facebook",
            kind="group",
            external_id="https://facebook.com/groups/test_group",
            url="https://facebook.com/groups/test_group",
            name="Test Group",
            role="community",
            enabled=True,
            collector_options={"auth_profile_id": prof.id},
            created_at=now,
            updated_at=now,
        )

        collector = FacebookCollector(auth_root="data/test_auth", fb_repo=fb_repo)

        # Mock FacebookBrowserSession to raise FacebookHumanActionRequired(CHECKPOINT_REQUIRED)
        with patch(
            "src.providers.facebook.collector.FacebookBrowserSession.__aenter__",
            side_effect=FacebookHumanActionRequired(FacebookAuthState.CHECKPOINT_REQUIRED),
        ):
            batch = await collector.collect(
                source, checkpoint=None, trigger=CollectionTrigger.SCHEDULED
            )

        assert batch.outcome == CollectionOutcome.AUTH_REQUIRED
        assert batch.error_kind == "checkpoint_required"

        # Check DB status was updated
        await conn.commit()
        updated_prof = await fb_repo.get_auth_profile_by_id(conn, prof.id)
        assert updated_prof is not None
        assert updated_prof.status == "checkpoint_required"
