"""Repository for source fragments."""

from __future__ import annotations

from collections.abc import Sequence

import psycopg

from src.domain.event_pipeline import NewSourceFragment, SourceFragment


class FragmentRepository:
    """Repository for source_fragments table."""

    async def create_fragments(
        self,
        conn: psycopg.AsyncConnection,
        revision_id: int,
        fragments: Sequence[NewSourceFragment],
    ) -> list[SourceFragment]:
        """Persist new fragments idempotently, returning all fragments for the revision."""
        if not fragments:
            return []

        for f in fragments:
            await conn.execute(
                """
                INSERT INTO source_fragments (
                    source_item_revision_id, ordinal, text_content,
                    normalized_hash, fragmenter_version, is_candidate, drop_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_item_revision_id, ordinal, fragmenter_version)
                DO UPDATE SET
                    text_content = EXCLUDED.text_content,
                    normalized_hash = EXCLUDED.normalized_hash,
                    is_candidate = EXCLUDED.is_candidate,
                    drop_reason = EXCLUDED.drop_reason
                """,
                (
                    revision_id,
                    f.ordinal,
                    f.text_content,
                    f.normalized_hash,
                    f.fragmenter_version,
                    f.is_candidate,
                    f.drop_reason,
                ),
            )

        cursor = await conn.execute(
            """
            SELECT id, source_item_revision_id, ordinal, text_content,
                   normalized_hash, fragmenter_version, is_candidate, drop_reason, created_at
            FROM source_fragments
            WHERE source_item_revision_id = %s
            ORDER BY ordinal ASC
            """,
            (revision_id,),
        )
        return [SourceFragment.from_row(row) async for row in cursor]

    async def list_candidates_for_revisions(
        self,
        conn: psycopg.AsyncConnection,
        revision_ids: Sequence[int],
        *,
        fragmenter_version: str | None = None,
    ) -> list[SourceFragment]:
        """List all candidate fragments for a sequence of revision IDs."""
        unique_revs = sorted(set(revision_ids))
        if not unique_revs:
            return []
        if fragmenter_version is not None:
            cursor = await conn.execute(
                """
                SELECT id, source_item_revision_id, ordinal, text_content,
                       normalized_hash, fragmenter_version, is_candidate, drop_reason, created_at
                FROM source_fragments
                WHERE source_item_revision_id = ANY(%s)
                  AND is_candidate = TRUE
                  AND fragmenter_version = %s
                ORDER BY source_item_revision_id ASC, ordinal ASC
                """,
                (unique_revs, fragmenter_version),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT id, source_item_revision_id, ordinal, text_content,
                       normalized_hash, fragmenter_version, is_candidate, drop_reason, created_at
                FROM source_fragments
                WHERE source_item_revision_id = ANY(%s)
                  AND is_candidate = TRUE
                ORDER BY source_item_revision_id ASC, ordinal ASC
                """,
                (unique_revs,),
            )
        return [SourceFragment.from_row(row) async for row in cursor]

    async def get_by_id(
        self, conn: psycopg.AsyncConnection, fragment_id: int
    ) -> SourceFragment | None:
        cursor = await conn.execute(
            """
            SELECT id, source_item_revision_id, ordinal, text_content,
                   normalized_hash, fragmenter_version, is_candidate, drop_reason, created_at
            FROM source_fragments
            WHERE id = %s
            """,
            (fragment_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else SourceFragment.from_row(row)

    async def get_by_ids(
        self, conn: psycopg.AsyncConnection, fragment_ids: Sequence[int]
    ) -> list[SourceFragment]:
        unique_ids = sorted(set(fragment_ids))
        if not unique_ids:
            return []
        cursor = await conn.execute(
            """
            SELECT id, source_item_revision_id, ordinal, text_content,
                   normalized_hash, fragmenter_version, is_candidate, drop_reason, created_at
            FROM source_fragments
            WHERE id = ANY(%s)
            ORDER BY id ASC
            """,
            (unique_ids,),
        )
        return [SourceFragment.from_row(row) async for row in cursor]

    async def list_fragments_for_story(
        self, conn: psycopg.AsyncConnection, story_id: int
    ) -> list[SourceFragment]:
        cursor = await conn.execute(
            """
            SELECT f.id, f.source_item_revision_id, f.ordinal, f.text_content,
                   f.normalized_hash, f.fragmenter_version, f.is_candidate, f.drop_reason, f.created_at
            FROM source_fragments f
            JOIN story_fragments sf ON sf.fragment_id = f.id
            WHERE sf.story_id = %s
            ORDER BY sf.id ASC
            """,
            (story_id,),
        )
        return [SourceFragment.from_row(row) async for row in cursor]
