"""Tests for Facebook post collection, semantic parsing, and execution locks (Plan 5 Task 3)."""

import datetime as dt
from unittest.mock import patch

import psycopg
import pytest

from src.domain.sources import Source
from src.ingestion.models import CollectionOutcome, CollectionTrigger
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
        assert item.metadata["temporal_fidelity"] == "precise"

        item_none, _ = parse_post_from_data(
            source=source,
            post_id="98766",
            text="Post without timestamp",
            published_at=None,
        )
        assert item_none.published_at is None
        assert item_none.metadata["temporal_fidelity"] == "unknown"

    def test_parse_facebook_timestamp_str(self):
        from src.providers.facebook.collector import parse_facebook_timestamp_str

        ref = dt.datetime(2026, 8, 24, 12, 0, 0, tzinfo=dt.timezone.utc)

        # Epoch
        epoch_dt = parse_facebook_timestamp_str("1787486400", reference_time=ref)
        assert epoch_dt == dt.datetime.fromtimestamp(1787486400, tz=dt.timezone.utc)

        # ISO
        iso_dt = parse_facebook_timestamp_str("2026-08-24T10:15:00Z", reference_time=ref)
        assert iso_dt == dt.datetime(2026, 8, 24, 10, 15, 0, tzinfo=dt.timezone.utc)

        # Relative mins/hours RU
        dt_mins = parse_facebook_timestamp_str("15 мин.", reference_time=ref)
        assert dt_mins == dt.datetime(2026, 8, 24, 11, 45, 0, tzinfo=dt.timezone.utc)
        dt_hrs = parse_facebook_timestamp_str("2 ч.", reference_time=ref)
        assert dt_hrs == dt.datetime(2026, 8, 24, 10, 0, 0, tzinfo=dt.timezone.utc)

        # Relative mins/hours UA
        dt_ua = parse_facebook_timestamp_str("3 год тому", reference_time=ref)
        assert dt_ua == dt.datetime(2026, 8, 24, 9, 0, 0, tzinfo=dt.timezone.utc)

        # Yesterday
        dt_yest = parse_facebook_timestamp_str("вчера в 14:30", reference_time=ref)
        assert dt_yest == dt.datetime(2026, 8, 23, 14, 30, 0, tzinfo=dt.timezone.utc)

        # Month and Day
        dt_month = parse_facebook_timestamp_str("20 августа в 08:00", reference_time=ref)
        assert dt_month == dt.datetime(2026, 8, 20, 8, 0, 0, tzinfo=dt.timezone.utc)

        # Unresolved
        assert parse_facebook_timestamp_str("недавно", reference_time=ref) is None
        assert parse_facebook_timestamp_str("", reference_time=ref) is None


class TestFacebookExecutionLock:
    """Tests cross-source execution lock resolver for Facebook auth profiles."""

    @pytest.mark.asyncio
    async def test_resolve_facebook_execution_lock(self):
        from unittest.mock import AsyncMock

        from src.jobs.ingestion import resolve_execution_lock
        from src.repositories.facebook import FacebookRepository

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
            collector_options={},
            created_at=now,
            updated_at=now,
        )
        mock_conn = AsyncMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                FacebookRepository,
                "get_source_config_by_source_id",
                AsyncMock(return_value=None),
            )
            lock = await resolve_execution_lock(mock_conn, source)
            assert lock == "facebook-auth-profile:default"


@pytest.mark.postgres
class TestFacebookCollectorIntegration:
    """Integration tests for FacebookCollector outcomes and database updates."""

    async def test_collector_handles_auth_checkpoint_and_updates_profile_status(
        self, conn: psycopg.AsyncConnection, edition
    ):
        now = dt.datetime.now(dt.timezone.utc)
        cur = await conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role, enabled)
            VALUES ('facebook', 'group', 'https://facebook.com/groups/test_group', 'https://facebook.com/groups/test_group', 'Test Group', 'community', true)
            RETURNING id
            """
        )
        source_id = (await cur.fetchone())[0]

        fb_repo = FacebookRepository()
        prof = await fb_repo.get_or_create_auth_profile(
            conn, name="test_prof", storage_ref="test_prof"
        )
        await fb_repo.update_auth_profile_status(
            conn, prof.id, status="ready", error_kind=None, error_message=None
        )
        await fb_repo.get_or_create_source_config(
            conn,
            source_id=source_id,
            auth_profile_id=prof.id,
            url="https://facebook.com/groups/test_group",
        )
        await conn.commit()

        source = Source(
            id=source_id,
            platform="facebook",
            kind="group",
            external_id="https://facebook.com/groups/test_group",
            url="https://facebook.com/groups/test_group",
            name="Test Group",
            role="community",
            enabled=True,
            collector_options={},
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
