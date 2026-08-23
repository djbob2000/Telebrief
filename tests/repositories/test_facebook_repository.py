"""Tests for Facebook repository, auth profile resolution, and circuit breaker."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from src.domain.sources import NewSource
from src.ingestion.registry import upsert_bootstrap_source
from src.providers.facebook.auth import FacebookAuthState
from src.repositories.facebook import (
    DEFAULT_AUTH_PROFILE_NAME,
    FacebookRepository,
    resolve_auth_profile_name,
)


@pytest.mark.postgres
class TestFacebookAuthProfileResolution:
    """Tests for profile name resolution isolated to Facebook configuration."""

    async def test_resolves_profile_from_source_config(self, repo_conn: psycopg.AsyncConnection):
        repo = FacebookRepository()
        prof = await repo.get_or_create_auth_profile(
            repo_conn, name="profile-alpha", storage_ref="alpha-dir"
        )

        src = await upsert_bootstrap_source(
            repo_conn,
            NewSource(
                platform="facebook",
                kind="facebook_group",
                external_id="https://facebook.com/groups/alpha",
                url="https://facebook.com/groups/alpha",
                name="Alpha Group",
                role="community",
                enabled=True,
                collector_options={},
            ),
        )
        assert src is not None

        await repo.get_or_create_source_config(
            repo_conn,
            source_id=src.id,
            auth_profile_id=prof.id,
            url=src.url,
        )

        resolved = await resolve_auth_profile_name(repo_conn, src.id, src.collector_options)
        assert resolved == "profile-alpha"

    async def test_telegram_source_without_facebook_config_returns_default(
        self, repo_conn: psycopg.AsyncConnection
    ):
        src = await upsert_bootstrap_source(
            repo_conn,
            NewSource(
                platform="telegram",
                kind="channel",
                external_id="@telegram_news",
                url="https://t.me/telegram_news",
                name="Telegram News",
                role="local_media",
                enabled=True,
                collector_options={},
            ),
        )
        assert src is not None

        resolved = await resolve_auth_profile_name(repo_conn, src.id, src.collector_options)
        assert resolved == DEFAULT_AUTH_PROFILE_NAME

    async def test_disabled_profile_circuit_breaker_recorded(
        self, repo_conn: psycopg.AsyncConnection
    ):
        repo = FacebookRepository()
        prof = await repo.get_or_create_auth_profile(
            repo_conn, name="breaker-prof", storage_ref="breaker-dir"
        )
        assert prof.status == "unknown"

        await repo.update_auth_profile_status(
            repo_conn,
            prof.id,
            status=FacebookAuthState.DISABLED.value,
            error_kind="challenge_failed",
            error_message="Too many checkpoints",
        )

        refreshed = await repo.get_auth_profile_by_id(repo_conn, prof.id)
        assert refreshed is not None
        assert refreshed.status == "disabled"
        assert refreshed.error_kind == "challenge_failed"

    async def test_list_posts_due_for_deep_refresh_filters_disabled_sources(
        self, repo_conn: psycopg.AsyncConnection
    ):
        repo = FacebookRepository()
        now = dt.datetime.now(dt.timezone.utc)
        # 1. Enabled FB source + item + revision
        cur = await repo_conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role, enabled)
            VALUES ('facebook', 'facebook_group', 'fb_enabled', 'https://fb.com/1', 'FB Enabled', 'community', true)
            RETURNING id
            """
        )
        s_enabled_id = (await cur.fetchone())[0]
        cur = await repo_conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at, published_at) VALUES (%s, 'facebook_post', 'post:1', %s, %s) RETURNING id",
            (s_enabled_id, now, now),
        )
        si_enabled_id = (await cur.fetchone())[0]
        await repo_conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h1', 'Post 1', %s)",
            (si_enabled_id, now),
        )

        # 2. Disabled FB source + item + revision
        cur = await repo_conn.execute(
            """
            INSERT INTO sources (platform, kind, external_id, url, name, role, enabled)
            VALUES ('facebook', 'facebook_group', 'fb_disabled', 'https://fb.com/2', 'FB Disabled', 'community', false)
            RETURNING id
            """
        )
        s_disabled_id = (await cur.fetchone())[0]
        cur = await repo_conn.execute(
            "INSERT INTO source_items (source_id, kind, external_id, first_collected_at, published_at) VALUES (%s, 'facebook_post', 'post:2', %s, %s) RETURNING id",
            (s_disabled_id, now, now),
        )
        si_disabled_id = (await cur.fetchone())[0]
        await repo_conn.execute(
            "INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content, collected_at) VALUES (%s, 1, 'h2', 'Post 2', %s)",
            (si_disabled_id, now),
        )

        # Query candidates for deep refresh
        candidates = await repo.list_posts_due_for_deep_refresh(repo_conn, scheduled_at=now)
        cand_item_ids = [c.source_item_id for c in candidates]
        assert si_enabled_id in cand_item_ids
        assert si_disabled_id not in cand_item_ids
