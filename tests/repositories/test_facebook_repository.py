"""Tests for Facebook repository, auth profile resolution, and circuit breaker."""

from __future__ import annotations

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
