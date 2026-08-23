"""Tests for Facebook auth profiles, source configurations, and config parsing (Plan 5 Task 1)."""

import psycopg
import pytest

from src.config_loader import (
    Config,
    FacebookAuthProfileBootstrap,
    FacebookConfig,
    FacebookSourceBootstrap,
    Settings,
    _parse_facebook_config,
)
from src.ingestion.registry import SourceRegistry
from src.repositories.facebook import FacebookRepository


def _make_dummy_config(fb_config: FacebookConfig | None = None) -> Config:
    settings = Settings(
        schedule_time="09:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4",
        openai_temperature=0.7,
        ai_provider="openai",
    )
    return Config(
        channels=[],
        settings=settings,
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
        facebook=fb_config or FacebookConfig(),
    )


class TestFacebookConfigParsing:
    """Tests config parsing for Facebook profiles, sources, and comments."""

    def test_comments_config_validates_positive_bounds(self):
        with pytest.raises(ValueError, match="positive int"):
            _parse_facebook_config({"facebook": {"comments": {"max_comments_per_post": 0}}})

        with pytest.raises(ValueError, match="positive int"):
            _parse_facebook_config(
                {"facebook": {"comments": {"max_duration_per_post_seconds": -5}}}
            )

    def test_auth_profile_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="without '..'"):
            _parse_facebook_config(
                {"facebook": {"auth_profiles": [{"name": "bad", "storage_ref": "../../../etc"}]}}
            )

    def test_sources_validate_scan_times_format(self):
        with pytest.raises(ValueError, match="HH:MM"):
            _parse_facebook_config(
                {
                    "facebook": {
                        "sources": [
                            {
                                "name": "fb-1",
                                "kind": "group",
                                "url": "https://facebook.com/groups/123",
                                "scan_times": ["8am", "12:00"],
                            }
                        ]
                    }
                }
            )


@pytest.mark.postgres
class TestFacebookDatabaseSchemaAndBootstrap:
    """Tests database constraints and idempotent bootstrapping."""

    async def test_auth_profiles_table_has_no_credential_columns(
        self, conn: psycopg.AsyncConnection
    ):
        cursor = await conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'facebook_auth_profiles'
            """
        )
        cols = [r[0] for r in await cursor.fetchall()]
        forbidden = {
            "cookie",
            "cookies",
            "password",
            "local_storage",
            "session_storage",
            "auth_token",
        }
        for f in forbidden:
            assert f not in cols, f"Forbidden credential column {f} found in facebook_auth_profiles"

    async def test_bootstrap_facebook_sources_idempotent_and_respects_database_management_mode(
        self, conn: psycopg.AsyncConnection, edition
    ):
        fb_profile = FacebookAuthProfileBootstrap(name="primary_profile", storage_ref="fb_primary")
        fb_source = FacebookSourceBootstrap(
            name="Бердянск Группа",
            kind="group",
            url="https://facebook.com/groups/berdyansk_news",
            role="community",
            auth_profile="primary_profile",
            enabled=True,
            scan_times=["09:00", "15:00", "21:00"],
            timezone="Europe/Kyiv",
        )
        fb_cfg = FacebookConfig(
            enabled=True,
            auth_profiles=[fb_profile],
            sources=[fb_source],
        )
        config = _make_dummy_config(fb_cfg)

        registry = SourceRegistry()
        fb_repo = FacebookRepository()

        # First bootstrap
        res1 = await registry.bootstrap_from_config(conn, config)
        assert res1.sources_created == 1
        assert res1.sources_skipped_db_managed == 0

        # Check DB state
        prof = await fb_repo.get_auth_profile_by_name(conn, "primary_profile")
        assert prof is not None
        assert prof.status == "unknown"

        cur = await conn.execute(
            "SELECT id, role, management_mode FROM sources WHERE platform = 'facebook'"
        )
        s_row = await cur.fetchone()
        source_id, initial_role, initial_mode = s_row[0], s_row[1], s_row[2]
        assert initial_role == "community"
        assert initial_mode == "bootstrap"

        src_cfg = await fb_repo.get_source_config_by_source_id(conn, source_id)
        assert src_cfg is not None
        assert src_cfg.scan_times == ["09:00", "15:00", "21:00"]
        assert src_cfg.timezone == "Europe/Kyiv"

        # Second bootstrap without changes -> unchanged
        res2 = await registry.bootstrap_from_config(conn, config)
        assert res2.sources_created == 0
        assert res2.sources_unchanged == 1

        # Switch management_mode to 'database' and change YAML role
        await conn.execute(
            "UPDATE sources SET management_mode = 'database', role = 'official' WHERE id = %s",
            (source_id,),
        )

        # Third bootstrap with YAML having role='community'
        res3 = await registry.bootstrap_from_config(conn, config)
        assert res3.sources_skipped_db_managed == 1

        # Assert role in DB was NOT overwritten
        cur = await conn.execute("SELECT role FROM sources WHERE id = %s", (source_id,))
        assert (await cur.fetchone())[0] == "official"
