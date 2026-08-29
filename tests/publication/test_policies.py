"""Tests for publication policy resolution and version auditing (Plan 5 Task 7)."""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from src.config_loader import Config, Settings
from src.publication.editorializer import DIGEST_EDITORIALIZER_PROMPT_VERSION
from src.publication.policies import (
    DEFAULT_WRITER_PROMPT_VERSION,
    PublicationPolicyService,
)

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


@pytest.mark.postgres
async def test_digest_writer_policy_includes_editorializer_metadata(
    conn: psycopg.AsyncConnection, edition
):
    service = PublicationPolicyService()
    settings = Settings(
        schedule_time="09:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4o",
        openai_temperature=0.7,
        ai_provider="openai",
    )
    config = Config(
        channels=[],
        settings=settings,
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
    )

    policy_set = await service.ensure_current(
        conn,
        edition_id=edition.id,
        publication_type="digest_grouped",
        config=config,
    )

    assert policy_set.writer.prompt_version == DIGEST_EDITORIALIZER_PROMPT_VERSION
    assert (
        policy_set.writer.config.get("editorializer_prompt_version")
        == DIGEST_EDITORIALIZER_PROMPT_VERSION
    )
    assert policy_set.writer.config.get("ai_provider") == "openai"
    assert policy_set.writer.config.get("ai_model") == "gpt-4o"


@pytest.mark.postgres
async def test_article_writer_policy_preserves_default_article_prompt(
    conn: psycopg.AsyncConnection, edition
):
    service = PublicationPolicyService()
    policy_set = await service.ensure_current(
        conn,
        edition_id=edition.id,
        publication_type="daily_article",
    )

    assert policy_set.writer.prompt_version == DEFAULT_WRITER_PROMPT_VERSION
