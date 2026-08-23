"""Tests for Facebook background jobs and runtime kill switch."""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

from src.domain.sources import Source
from src.ingestion.models import CollectionTrigger
from src.jobs.facebook import (
    dispatch_facebook_deep_sweep,
    refresh_facebook_comments,
)
from src.providers.facebook.collector import FacebookCollector
from src.providers.facebook.comments import FacebookCommentCollector
from src.providers.facebook.runtime_policy import is_facebook_enabled

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


def test_is_facebook_enabled_policy():
    assert is_facebook_enabled({}) is False
    assert is_facebook_enabled({"facebook": {"enabled": False}}) is False
    assert is_facebook_enabled({"facebook": {"enabled": True}}) is True
    with patch("src.config_loader.load_config", return_value={"facebook": {"enabled": True}}):
        assert is_facebook_enabled(None) is True
    with patch("src.config_loader.load_config", return_value={"facebook": {"enabled": False}}):
        assert is_facebook_enabled(None) is False


def test_production_registry_registers_facebook_when_enabled():
    from src.config_loader import Config, FacebookConfig, Settings
    from src.ingestion.registry import build_default_collector_registry

    fb_cfg = FacebookConfig(enabled=True)
    cfg = Config(
        channels=[],
        settings=Settings(
            schedule_time="08:00",
            timezone="UTC",
            lookback_hours=24,
            openai_model="gpt-5-nano",
            openai_temperature=0.7,
        ),
        telegram_api_id=123456,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
        facebook=fb_cfg,
    )
    registry = build_default_collector_registry(cfg)
    assert "facebook" in registry.registered_platforms()
    assert "telegram" in registry.registered_platforms()


async def test_facebook_collector_bypasses_when_disabled():
    source = Source(
        id=1,
        platform="facebook",
        kind="facebook_group",
        external_id="https://www.facebook.com/groups/test",
        url="https://www.facebook.com/groups/test",
        name="Test Group",
        role="community",
        enabled=True,
        collector_options={},
        created_at=_NOW,
        updated_at=_NOW,
    )
    collector = FacebookCollector(auth_root="/tmp")

    with patch("src.providers.facebook.runtime_policy.is_facebook_enabled", return_value=False):
        batch = await collector.collect(source, None, trigger=CollectionTrigger.SCHEDULED)
        assert batch.items == ()
        assert batch.assets == ()


async def test_facebook_comment_collector_bypasses_when_disabled():
    collector = FacebookCommentCollector(auth_root="/tmp")
    source = Source(
        id=1,
        platform="facebook",
        kind="facebook_group",
        external_id="https://www.facebook.com/groups/test",
        url="https://www.facebook.com/groups/test",
        name="Test Group",
        role="community",
        enabled=True,
        collector_options={},
        created_at=_NOW,
        updated_at=_NOW,
    )
    with patch("src.providers.facebook.runtime_policy.is_facebook_enabled", return_value=False):
        batch = await collector.scan_post_with_page(
            source=source,
            post_item_id=10,
            post_external_id="post:123",
            page=None,
        )
        assert batch.items == []
        assert batch.stop_reason == "disabled"


async def test_facebook_jobs_bypass_when_disabled():
    with patch("src.providers.facebook.runtime_policy.is_facebook_enabled", return_value=False):
        with patch("src.providers.facebook.browser.FacebookBrowserSession") as mock_browser:
            await refresh_facebook_comments(source_item_revision_id=1, post_item_id=1)
            await dispatch_facebook_deep_sweep(timestamp=int(_NOW.timestamp()))
            mock_browser.assert_not_called()
