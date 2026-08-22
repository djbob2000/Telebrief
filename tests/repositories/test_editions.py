"""Repository tests for Edition persistence and source-edition binding."""

from __future__ import annotations

import psycopg
import psycopg.errors
import pytest

from src.domain.editions import NewEdition
from src.domain.sources import NewSource
from src.repositories.editions import EditionRepository
from src.repositories.sources import SourceRepository


async def _create_edition(conn, slug: str, **overrides):
    payload = {"slug": slug, "name": f"Edition {slug}"}
    payload.update(overrides)
    return await EditionRepository().create(conn, NewEdition(**payload))


async def test_create_persists_and_get_by_slug_roundtrip(repo_conn):
    created = await _create_edition(
        repo_conn,
        "kyiv-daily",
        timezone="Europe/Kyiv",
        language="uk",
        profile={"tone": "neutral"},
        config={"max_items": 10},
    )

    assert created.id > 0
    assert created.slug == "kyiv-daily"
    assert created.name == "Edition kyiv-daily"
    assert created.timezone == "Europe/Kyiv"
    assert created.language == "uk"
    assert created.profile == {"tone": "neutral"}
    assert created.config == {"max_items": 10}
    assert created.enabled is True
    assert created.created_at is not None
    assert created.updated_at is not None

    fetched = await EditionRepository().get_by_slug(repo_conn, "kyiv-daily")

    assert fetched == created


async def test_get_by_slug_applies_column_defaults(repo_conn):
    await _create_edition(repo_conn, "default-edition")

    fetched = await EditionRepository().get_by_slug(repo_conn, "default-edition")

    assert fetched is not None
    assert fetched.timezone == "UTC"
    assert fetched.language == "ru"
    assert fetched.profile == {}
    assert fetched.config == {}
    assert fetched.enabled is True


async def test_get_by_slug_missing_edition_returns_none(repo_conn):
    assert await EditionRepository().get_by_slug(repo_conn, "no-such-slug") is None


async def test_duplicate_slug_raises_unique_violation(repo_conn):
    await _create_edition(repo_conn, "twice")

    with pytest.raises(psycopg.errors.UniqueViolation):
        await _create_edition(repo_conn, "twice")


async def test_bind_source_links_many_to_many(repo_conn):
    source_repo = SourceRepository()
    edition_repo = EditionRepository()
    first_source = await source_repo.create(
        repo_conn, NewSource(platform="telegram", kind="channel", name="S1")
    )
    second_source = await source_repo.create(
        repo_conn, NewSource(platform="telegram", kind="channel", name="S2")
    )
    kyiv = await _create_edition(repo_conn, "kyiv")
    odesa = await _create_edition(repo_conn, "odesa")

    await edition_repo.bind_source(repo_conn, first_source.id, kyiv.id)
    await edition_repo.bind_source(repo_conn, first_source.id, odesa.id)
    await edition_repo.bind_source(repo_conn, second_source.id, kyiv.id)

    cursor = await repo_conn.execute(
        "SELECT source_id, edition_id FROM source_editions ORDER BY source_id, edition_id"
    )
    rows = await cursor.fetchall()
    assert set(rows) == {
        (first_source.id, kyiv.id),
        (first_source.id, odesa.id),
        (second_source.id, kyiv.id),
    }


async def test_bind_source_is_idempotent_on_rebinding(repo_conn):
    """Rebinding the same pair is a no-op (ON CONFLICT DO NOTHING)."""
    source_repo = SourceRepository()
    edition_repo = EditionRepository()
    source = await source_repo.create(
        repo_conn, NewSource(platform="telegram", kind="channel", name="S1")
    )
    edition = await _create_edition(repo_conn, "kyiv")

    await edition_repo.bind_source(repo_conn, source.id, edition.id)
    await edition_repo.bind_source(repo_conn, source.id, edition.id)

    cursor = await repo_conn.execute("SELECT count(*) FROM source_editions")
    assert (await cursor.fetchone())[0] == 1


async def test_bind_unknown_source_raises_foreign_key_violation(repo_conn):
    edition = await _create_edition(repo_conn, "kyiv")

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await EditionRepository().bind_source(repo_conn, 987654, edition.id)
