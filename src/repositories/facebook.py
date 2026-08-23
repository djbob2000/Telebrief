"""Repository for Facebook auth profiles, source configs, comment states, and collector artifacts (Plan 5 Task 1)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from src.providers.facebook.models import (
    CollectorArtifact,
    FacebookAuthProfile,
    FacebookCommentState,
    FacebookSourceConfig,
)


class FacebookRepository:
    """Repository managing Facebook tables."""

    async def get_or_create_auth_profile(
        self,
        conn: psycopg.AsyncConnection,
        *,
        name: str,
        storage_ref: str,
        metadata: dict[str, Any] | None = None,
    ) -> FacebookAuthProfile:
        cursor = await conn.execute(
            """
            SELECT id, name, storage_ref, status, last_verified_at, error_kind,
                   error_message, metadata, created_at, updated_at
            FROM facebook_auth_profiles
            WHERE name = %s
            """,
            (name,),
        )
        row = await cursor.fetchone()
        if row is not None:
            return FacebookAuthProfile.from_row(row)

        cursor = await conn.execute(
            """
            INSERT INTO facebook_auth_profiles (name, storage_ref, metadata)
            VALUES (%s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                storage_ref = EXCLUDED.storage_ref,
                updated_at = now()
            RETURNING id, name, storage_ref, status, last_verified_at, error_kind,
                      error_message, metadata, created_at, updated_at
            """,
            (name, storage_ref, Jsonb(metadata or {})),
        )
        return FacebookAuthProfile.from_row(await cursor.fetchone())

    async def get_auth_profile_by_id(
        self, conn: psycopg.AsyncConnection, profile_id: int
    ) -> FacebookAuthProfile | None:
        cursor = await conn.execute(
            """
            SELECT id, name, storage_ref, status, last_verified_at, error_kind,
                   error_message, metadata, created_at, updated_at
            FROM facebook_auth_profiles
            WHERE id = %s
            """,
            (profile_id,),
        )
        row = await cursor.fetchone()
        return FacebookAuthProfile.from_row(row) if row is not None else None

    async def get_auth_profile_by_name(
        self, conn: psycopg.AsyncConnection, name: str
    ) -> FacebookAuthProfile | None:
        cursor = await conn.execute(
            """
            SELECT id, name, storage_ref, status, last_verified_at, error_kind,
                   error_message, metadata, created_at, updated_at
            FROM facebook_auth_profiles
            WHERE name = %s
            """,
            (name,),
        )
        row = await cursor.fetchone()
        return FacebookAuthProfile.from_row(row) if row is not None else None

    async def list_auth_profiles(self, conn: psycopg.AsyncConnection) -> list[FacebookAuthProfile]:
        cursor = await conn.execute(
            """
            SELECT id, name, storage_ref, status, last_verified_at, error_kind,
                   error_message, metadata, created_at, updated_at
            FROM facebook_auth_profiles
            ORDER BY id ASC
            """
        )
        return [FacebookAuthProfile.from_row(row) for row in await cursor.fetchall()]

    async def update_auth_profile_status(
        self,
        conn: psycopg.AsyncConnection,
        profile_id: int,
        *,
        status: str,
        error_kind: str | None = None,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        verified_at: dt.datetime | None = None,
    ) -> None:
        await conn.execute(
            """
            UPDATE facebook_auth_profiles
            SET status = %s,
                error_kind = %s,
                error_message = %s,
                last_verified_at = COALESCE(%s, last_verified_at),
                metadata = CASE WHEN %s::jsonb IS NOT NULL THEN metadata || %s::jsonb ELSE metadata END,
                updated_at = now()
            WHERE id = %s
            """,
            (
                status,
                error_kind,
                error_message,
                verified_at,
                Jsonb(metadata) if metadata is not None else None,
                Jsonb(metadata) if metadata is not None else None,
                profile_id,
            ),
        )

    async def get_or_create_source_config(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_id: int,
        auth_profile_id: int,
        url: str,
        group_or_page_id: str | None = None,
        scan_times: list[str] | None = None,
        timezone: str = "UTC",
        collector_options: dict[str, Any] | None = None,
    ) -> FacebookSourceConfig:
        times = scan_times or ["08:00", "12:00", "16:00", "19:30"]
        cursor = await conn.execute(
            """
            INSERT INTO facebook_source_configs (
                source_id, auth_profile_id, group_or_page_id, url,
                scan_times, timezone, collector_options
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE SET
                auth_profile_id = EXCLUDED.auth_profile_id,
                group_or_page_id = EXCLUDED.group_or_page_id,
                url = EXCLUDED.url,
                scan_times = EXCLUDED.scan_times,
                timezone = EXCLUDED.timezone,
                collector_options = EXCLUDED.collector_options,
                updated_at = now()
            RETURNING id, source_id, auth_profile_id, group_or_page_id, url,
                      scan_times, timezone, collector_options, created_at, updated_at
            """,
            (
                source_id,
                auth_profile_id,
                group_or_page_id,
                url,
                times,
                timezone,
                Jsonb(collector_options or {}),
            ),
        )
        return FacebookSourceConfig.from_row(await cursor.fetchone())

    async def get_source_config_by_source_id(
        self, conn: psycopg.AsyncConnection, source_id: int
    ) -> FacebookSourceConfig | None:
        cursor = await conn.execute(
            """
            SELECT id, source_id, auth_profile_id, group_or_page_id, url,
                   scan_times, timezone, collector_options, created_at, updated_at
            FROM facebook_source_configs
            WHERE source_id = %s
            """,
            (source_id,),
        )
        row = await cursor.fetchone()
        return FacebookSourceConfig.from_row(row) if row is not None else None

    async def get_or_create_comment_state(
        self, conn: psycopg.AsyncConnection, *, source_item_id: int
    ) -> FacebookCommentState:
        cursor = await conn.execute(
            """
            INSERT INTO facebook_comment_collection_state (source_item_id)
            VALUES (%s)
            ON CONFLICT (source_item_id) DO NOTHING
            RETURNING id, source_item_id, last_scanned_at, oldest_comment_published_at,
                      newest_comment_published_at, total_comments_observed, completeness,
                      continuation_state, metadata, created_at, updated_at
            """,
            (source_item_id,),
        )
        row = await cursor.fetchone()
        if row is not None:
            return FacebookCommentState.from_row(row)

        cursor = await conn.execute(
            """
            SELECT id, source_item_id, last_scanned_at, oldest_comment_published_at,
                   newest_comment_published_at, total_comments_observed, completeness,
                   continuation_state, metadata, created_at, updated_at
            FROM facebook_comment_collection_state
            WHERE source_item_id = %s
            """,
            (source_item_id,),
        )
        return FacebookCommentState.from_row(await cursor.fetchone())

    async def update_comment_state(
        self,
        conn: psycopg.AsyncConnection,
        source_item_id: int,
        *,
        last_scanned_at: dt.datetime | None = None,
        oldest_comment_published_at: dt.datetime | None = None,
        newest_comment_published_at: dt.datetime | None = None,
        total_comments_observed: int | None = None,
        completeness: str | None = None,
        continuation_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await conn.execute(
            """
            UPDATE facebook_comment_collection_state
            SET last_scanned_at = COALESCE(%s, last_scanned_at),
                oldest_comment_published_at = COALESCE(%s, oldest_comment_published_at),
                newest_comment_published_at = COALESCE(%s, newest_comment_published_at),
                total_comments_observed = COALESCE(%s, total_comments_observed),
                completeness = COALESCE(%s, completeness),
                continuation_state = CASE WHEN %s::jsonb IS NOT NULL THEN %s::jsonb ELSE continuation_state END,
                metadata = CASE WHEN %s::jsonb IS NOT NULL THEN metadata || %s::jsonb ELSE metadata END,
                updated_at = now()
            WHERE source_item_id = %s
            """,
            (
                last_scanned_at,
                oldest_comment_published_at,
                newest_comment_published_at,
                total_comments_observed,
                completeness,
                Jsonb(continuation_state) if continuation_state is not None else None,
                Jsonb(continuation_state) if continuation_state is not None else None,
                Jsonb(metadata) if metadata is not None else None,
                Jsonb(metadata) if metadata is not None else None,
                source_item_id,
            ),
        )

    async def list_posts_due_for_deep_refresh(
        self,
        conn: psycopg.AsyncConnection,
        *,
        scheduled_at: dt.datetime,
        lookback_days: int = 7,
        max_posts: int = 50,
    ) -> list[Any]:
        cutoff = scheduled_at - dt.timedelta(days=lookback_days)
        cursor = await conn.execute(
            """
            SELECT si.id, si.source_id, si.external_id, r.id AS current_revision_id,
                   cs.last_scanned_at, cs.completeness
            FROM source_items si
            JOIN sources s ON s.id = si.source_id AND s.platform = 'facebook'
            JOIN LATERAL (
                SELECT id FROM source_item_revisions
                WHERE source_item_id = si.id
                ORDER BY revision_no DESC LIMIT 1
            ) r ON true
            LEFT JOIN facebook_comment_collection_state cs ON cs.source_item_id = si.id
            WHERE si.kind = 'facebook_post'
              AND (si.published_at >= %s OR si.first_collected_at >= %s)
            ORDER BY cs.last_scanned_at ASC NULLS FIRST, si.published_at DESC
            LIMIT %s
            """,
            (cutoff, cutoff, max_posts),
        )
        from types import SimpleNamespace

        rows = await cursor.fetchall()
        return [
            SimpleNamespace(
                source_item_id=r[0],
                source_id=r[1],
                external_id=r[2],
                current_revision_id=r[3],
                last_scanned_at=r[4],
                completeness=r[5],
            )
            for r in rows
        ]

    async def list_recent_active_posts(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_id: int,
        limit: int = 10,
    ) -> list[Any]:
        cursor = await conn.execute(
            """
            SELECT si.id, si.source_id, si.external_id, r.id AS current_revision_id
            FROM source_items si
            JOIN LATERAL (
                SELECT id FROM source_item_revisions
                WHERE source_item_id = si.id
                ORDER BY revision_no DESC LIMIT 1
            ) r ON true
            WHERE si.source_id = %s AND si.kind = 'facebook_post'
            ORDER BY si.published_at DESC NULLS LAST, si.id DESC
            LIMIT %s
            """,
            (source_id, limit),
        )
        from types import SimpleNamespace

        rows = await cursor.fetchall()
        return [
            SimpleNamespace(
                source_item_id=r[0],
                source_id=r[1],
                external_id=r[2],
                current_revision_id=r[3],
            )
            for r in rows
        ]

    async def insert_artifact(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_id: int,
        artifact_type: str,
        storage_path: str,
        expires_at: dt.datetime,
        metadata: dict[str, Any] | None = None,
    ) -> CollectorArtifact:
        cursor = await conn.execute(
            """
            INSERT INTO collector_artifacts (source_id, artifact_type, storage_path, expires_at, metadata)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, source_id, artifact_type, storage_path, expires_at, metadata, created_at
            """,
            (source_id, artifact_type, storage_path, expires_at, Jsonb(metadata or {})),
        )
        return CollectorArtifact.from_row(await cursor.fetchone())

    async def list_expired_artifacts(
        self, conn: psycopg.AsyncConnection, cutoff: dt.datetime
    ) -> list[CollectorArtifact]:
        cursor = await conn.execute(
            """
            SELECT id, source_id, artifact_type, storage_path, expires_at, metadata, created_at
            FROM collector_artifacts
            WHERE expires_at <= %s
            ORDER BY id ASC
            """,
            (cutoff,),
        )
        return [CollectorArtifact.from_row(row) for row in await cursor.fetchall()]

    async def delete_artifact(self, conn: psycopg.AsyncConnection, artifact_id: int) -> None:
        await conn.execute("DELETE FROM collector_artifacts WHERE id = %s", (artifact_id,))
