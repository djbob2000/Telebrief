"""Repository tests for Source persistence against PostgreSQL."""

from __future__ import annotations

import psycopg
import psycopg.errors
import pytest

from src.domain.sources import NewSource
from src.repositories.sources import SourceRepository


async def test_create_persists_and_returns_source(repo_conn):
    repo = SourceRepository()
    created = await repo.create(
        repo_conn,
        NewSource(
            platform="telegram",
            kind="channel",
            external_id="-1001",
            url="https://t.me/news",
            name="News",
            role="community",
            collector_options={"limit": 5},
        ),
    )

    assert created.id > 0
    assert created.platform == "telegram"
    assert created.kind == "channel"
    assert created.external_id == "-1001"
    assert created.url == "https://t.me/news"
    assert created.name == "News"
    assert created.role == "community"
    assert created.enabled is True
    assert created.collector_options == {"limit": 5}
    assert created.created_at is not None
    assert created.updated_at is not None


async def test_get_returns_matching_source(repo_conn):
    repo = SourceRepository()
    created = await repo.create(
        repo_conn, NewSource(platform="telegram", kind="channel", name="News")
    )

    fetched = await repo.get(repo_conn, created.id)

    assert fetched == created


async def test_get_missing_source_returns_none(repo_conn):
    repo = SourceRepository()

    assert await repo.get(repo_conn, 987654) is None


async def test_duplicate_platform_kind_external_id_raises_unique_violation(repo_conn):
    repo = SourceRepository()
    await repo.create(
        repo_conn,
        NewSource(
            platform="telegram",
            kind="channel",
            external_id="-1001",
            name="News",
            role="community",
        ),
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        await repo.create(
            repo_conn,
            NewSource(
                platform="telegram",
                kind="channel",
                external_id="-1001",
                name="Duplicate",
                role="community",
            ),
        )


async def test_url_only_sources_are_exempt_from_uniqueness_index(repo_conn):
    repo = SourceRepository()

    first = await repo.create(
        repo_conn, NewSource(platform="web", kind="site", url="https://example.org", name="Example")
    )
    second = await repo.create(
        repo_conn,
        NewSource(platform="web", kind="site", url="https://example.org", name="Example mirror"),
    )

    assert first.id != second.id


async def test_invalid_role_rejected_by_check_constraint(repo_conn):
    repo = SourceRepository()

    with pytest.raises(psycopg.errors.CheckViolation):
        await repo.create(
            repo_conn,
            NewSource(platform="telegram", kind="channel", name="News", role="bogus"),
        )


async def test_list_enabled_filters_by_platform(repo_conn):
    repo = SourceRepository()
    tg_active = await repo.create(
        repo_conn, NewSource(platform="telegram", kind="channel", name="TG active")
    )
    vb_active = await repo.create(
        repo_conn, NewSource(platform="viber", kind="channel", name="VB active")
    )
    await repo.create(
        repo_conn,
        NewSource(platform="telegram", kind="group", name="TG off", enabled=False),
    )

    enabled_all = await repo.list_enabled(repo_conn)
    assert {s.id for s in enabled_all} == {tg_active.id, vb_active.id}
    assert all(s.enabled for s in enabled_all)

    telegram_only = await repo.list_enabled(repo_conn, platform="telegram")
    assert [s.id for s in telegram_only] == [tg_active.id]

    assert await repo.list_enabled(repo_conn, platform="whatsapp") == []
