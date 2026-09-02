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


@pytest.mark.postgres
async def test_publication_policy_service_stores_explicit_semantic_versions(
    conn: psycopg.AsyncConnection, edition
):
    """Test 5A: PublicationPolicyService stores explicit versions for selection and article writer."""
    service = PublicationPolicyService()
    policy_set = await service.ensure_current(
        conn,
        edition_id=edition.id,
        publication_type="daily_article",
    )

    # Selection versions
    assert policy_set.selection.config.get("selection_semantics_version") == "v2"
    assert policy_set.selection.config.get("selection_prompt_version") == "v2"

    # Writer versions
    assert policy_set.writer.config.get("article_writer_version") == "v2"
    assert policy_set.writer.config.get("article_coverage_plan_version") == "v2"
    assert policy_set.writer.config.get("article_recovery_version") == "v2"


@pytest.mark.postgres
async def test_editorial_selection_service_rejects_unsupported_semantic_version(
    conn: psycopg.AsyncConnection, pool, edition
):
    """Test 5B: EditorialSelectionService raises UnsupportedFrozenSemanticVersion for unknown semantics."""
    from src.db.uow import DatabaseUnitOfWork
    from src.publication.errors import UnsupportedFrozenSemanticVersion
    from src.publication.selection import EditorialSelectionService
    from src.publication.snapshot import PublicationSnapshotService

    uow = DatabaseUnitOfWork(pool)
    snap_service = PublicationSnapshotService(uow=uow)

    run = await snap_service.create_run(
        edition_id=edition.id,
        publication_type="daily_article",
        snapshot_at=_NOW,
        request_key="test-unsupported-sel-version",
    )
    await snap_service.seal_candidates(run.id)

    # Tamper selection policy with unsupported version
    await conn.execute(
        "UPDATE editorial_selection_policy_versions SET config = jsonb_set(config, '{selection_semantics_version}', '\"v999\"') WHERE id = %s",
        (run.selection_policy_id,),
    )

    service = EditorialSelectionService(uow=uow)
    with pytest.raises(UnsupportedFrozenSemanticVersion, match="v999"):
        await service.select(run.id)


@pytest.mark.postgres
async def test_publication_generation_service_rejects_unsupported_article_writer_version(
    conn: psycopg.AsyncConnection, pool, edition
):
    """Test 5C: PublicationGenerationService raises UnsupportedFrozenSemanticVersion for unknown writer versions."""
    from src.db.uow import DatabaseUnitOfWork
    from src.publication.errors import UnsupportedFrozenSemanticVersion
    from src.publication.generation import PublicationGenerationService
    from src.publication.selection import EditorialSelectionService
    from src.publication.snapshot import PublicationSnapshotService

    uow = DatabaseUnitOfWork(pool)
    snap_service = PublicationSnapshotService(uow=uow)

    run = await snap_service.create_run(
        edition_id=edition.id,
        publication_type="daily_article",
        snapshot_at=_NOW,
        request_key="test-unsupported-writer-version",
    )
    await snap_service.seal_candidates(run.id)
    await EditorialSelectionService(uow=uow).select(run.id)

    # Tamper writer policy with unsupported version
    await conn.execute(
        "UPDATE writer_policy_versions SET config = jsonb_set(config, '{article_writer_version}', '\"v999\"') WHERE id = %s",
        (run.writer_policy_id,),
    )

    gen_service = PublicationGenerationService(uow=uow)
    with pytest.raises(UnsupportedFrozenSemanticVersion, match="v999"):
        await gen_service.generate(run.id)
