"""Edition repository: explicit SQL over psycopg async connections.

Repositories never commit and never open their own connections; the caller
owns transaction boundaries.
"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from src.domain.editions import Edition, NewEdition


class EditionRepository:
    """Persistence for `editions` rows and `source_editions` bindings."""

    async def create(self, conn: psycopg.AsyncConnection, edition: NewEdition) -> Edition:
        cursor = await conn.execute(
            """INSERT INTO editions(slug, name, timezone, language, profile, config, enabled)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id, slug, name, timezone, language, profile, config, enabled,
                   created_at, updated_at""",
            (
                edition.slug,
                edition.name,
                edition.timezone,
                edition.language,
                Jsonb(edition.profile),
                Jsonb(edition.config),
                edition.enabled,
            ),
        )
        return Edition.from_row(await cursor.fetchone())

    async def get_by_slug(self, conn: psycopg.AsyncConnection, slug: str) -> Edition | None:
        cursor = await conn.execute(
            """SELECT id, slug, name, timezone, language, profile, config, enabled,
                   created_at, updated_at
               FROM editions WHERE slug = %s""",
            (slug,),
        )
        row = await cursor.fetchone()
        return None if row is None else Edition.from_row(row)

    async def bind_source(
        self, conn: psycopg.AsyncConnection, source_id: int, edition_id: int
    ) -> None:
        """Bind a source to an edition; rebinding the same pair is a no-op."""
        await conn.execute(
            """INSERT INTO source_editions(source_id, edition_id)
               VALUES (%s, %s)
               ON CONFLICT (source_id, edition_id) DO NOTHING""",
            (source_id, edition_id),
        )
