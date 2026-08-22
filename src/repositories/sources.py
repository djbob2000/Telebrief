"""Source repository: explicit SQL over psycopg async connections.

Repositories never commit and never open their own connections; the caller
owns transaction boundaries.
"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from src.domain.sources import NewSource, Source


class SourceRepository:
    """Persistence for `sources` rows."""

    async def create(self, conn: psycopg.AsyncConnection, source: NewSource) -> Source:
        cursor = await conn.execute(
            """INSERT INTO sources(platform, kind, external_id, url, name, role, enabled,
                   collector_options)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id, platform, kind, external_id, url, name, role, enabled,
                   collector_options, created_at, updated_at""",
            (
                source.platform,
                source.kind,
                source.external_id,
                source.url,
                source.name,
                source.role,
                source.enabled,
                Jsonb(source.collector_options),
            ),
        )
        return Source.from_row(await cursor.fetchone())

    async def get(self, conn: psycopg.AsyncConnection, source_id: int) -> Source | None:
        cursor = await conn.execute(
            """SELECT id, platform, kind, external_id, url, name, role, enabled,
                   collector_options, created_at, updated_at
               FROM sources WHERE id = %s""",
            (source_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else Source.from_row(row)

    async def list_enabled(
        self, conn: psycopg.AsyncConnection, *, platform: str | None = None
    ) -> list[Source]:
        if platform is None:
            cursor = await conn.execute(
                """SELECT id, platform, kind, external_id, url, name, role, enabled,
                       collector_options, created_at, updated_at
                   FROM sources WHERE enabled ORDER BY id""",
            )
        else:
            cursor = await conn.execute(
                """SELECT id, platform, kind, external_id, url, name, role, enabled,
                       collector_options, created_at, updated_at
                   FROM sources WHERE enabled AND platform = %s ORDER BY id""",
                (platform,),
            )
        rows = await cursor.fetchall()
        return [Source.from_row(row) for row in rows]
