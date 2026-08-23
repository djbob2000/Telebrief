"""Ingestion repository: explicit SQL over caller-owned connections.

Repositories never commit and never open their own connections; the caller
owns transaction boundaries. Item identity is UNIQUE(source_id, external_id);
revisions are immutable and dedup against the latest revision's content hash
only; state events are append-only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING

import psycopg
from psycopg.types.json import Jsonb

from src.domain.ingestion import CollectionRun, SourceItem, SourceItemRevision
from src.domain.sources import Source
from src.ingestion.models import (
    CollectionCheckpoint,
    JSONValue,
    ObservedAsset,
    ObservedItem,
    ObservedStateEvent,
)

if TYPE_CHECKING:
    from src.processing.vision import AssetDescriptor


class IngestionRepository:
    """Persistence for source items, revisions, assets, events, runs."""

    async def get_item(
        self, conn: psycopg.AsyncConnection, *, source_id: int, external_id: str
    ) -> SourceItem | None:
        cursor = await conn.execute(
            """
            SELECT id, source_id, kind, external_id, parent_item_id, root_item_id,
                   author_name, author_external_id, canonical_url, published_at,
                   first_collected_at, metadata
            FROM source_items
            WHERE source_id = %s AND external_id = %s
            """,
            (source_id, external_id),
        )
        row = await cursor.fetchone()
        return None if row is None else SourceItem.from_row(row)

    async def get_or_create_item_shell(
        self, conn: psycopg.AsyncConnection, source_id: int, observation: ObservedItem
    ) -> tuple[SourceItem, bool]:
        """Create the item identity row if missing; never overwrites it.

        The shell carries only identity plus first-observation fields;
        relationships are resolved separately by ``ensure_relationships`` so
        out-of-order batches still link once every shell exists.
        """
        cursor = await conn.execute(
            """
            INSERT INTO source_items (
                source_id, kind, external_id, author_name, canonical_url,
                published_at, first_collected_at, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id, external_id) DO NOTHING
            RETURNING id, source_id, kind, external_id, parent_item_id,
                root_item_id, author_name, author_external_id, canonical_url,
                published_at, first_collected_at, metadata
            """,
            (
                source_id,
                observation.kind,
                observation.external_id,
                observation.author_name,
                observation.canonical_url,
                observation.published_at,
                observation.observed_at,
                Jsonb(observation.metadata),
            ),
        )
        row = await cursor.fetchone()
        if row is not None:
            return SourceItem.from_row(row), True

        item = await self.get_item(conn, source_id=source_id, external_id=observation.external_id)
        if item is None:
            raise RuntimeError(f"source item {observation.external_id!r} vanished during upsert")
        return item, False

    async def ensure_relationships(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_id: int,
        item_id: int,
        parent_external_id: str | None,
        root_external_id: str | None,
    ) -> None:
        """Resolve parent/root stable ids to item ids within this source.

        Unresolved references are left untouched (NULL on a fresh shell) so a
        later batch containing the missing shell can complete the linkage.
        """
        parent_id = await self._resolve_external_id(conn, source_id, parent_external_id)
        root_id = await self._resolve_external_id(conn, source_id, root_external_id)
        if parent_id is None and root_id is None:
            return
        await conn.execute(
            """
            UPDATE source_items
            SET parent_item_id = COALESCE(%s, parent_item_id),
                root_item_id = COALESCE(%s, root_item_id)
            WHERE id = %s
            """,
            (parent_id, root_id, item_id),
        )

    async def insert_revision_if_changed(
        self,
        conn: psycopg.AsyncConnection,
        item_id: int,
        observation: ObservedItem,
        *,
        collected_at: datetime,
    ) -> SourceItemRevision | None:
        """Append a revision unless it matches the latest revision's hash.

        Comparison is deliberately against the latest revision only: an edit
        back to a historical text (A -> B -> A) legitimately creates revision 3.
        """
        content_hash = _content_hash(observation)
        latest = await self.get_latest_revision(conn, item_id)
        if latest is not None and latest.content_hash == content_hash:
            return None
        revision_no = 1 if latest is None else latest.revision_no + 1
        cursor = await conn.execute(
            """
            INSERT INTO source_item_revisions (
                source_item_id, revision_no, collected_at, content_hash,
                text_content, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, source_item_id, revision_no, collected_at,
                content_hash, text_content, payload
            """,
            (
                item_id,
                revision_no,
                collected_at,
                content_hash,
                observation.text,
                Jsonb(observation.metadata),
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("revision insert returned no row")
        return SourceItemRevision.from_row(row)

    async def get_latest_revision(
        self, conn: psycopg.AsyncConnection, item_id: int
    ) -> SourceItemRevision | None:
        cursor = await conn.execute(
            """
            SELECT id, source_item_id, revision_no, collected_at, content_hash,
                   text_content, payload
            FROM source_item_revisions
            WHERE source_item_id = %s
            ORDER BY revision_no DESC
            LIMIT 1
            """,
            (item_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else SourceItemRevision.from_row(row)

    async def get_revision(
        self, conn: psycopg.AsyncConnection, revision_id: int
    ) -> SourceItemRevision | None:
        """Fetch one immutable revision by id (relevance processing input)."""
        cursor = await conn.execute(
            """
            SELECT id, source_item_id, revision_no, collected_at, content_hash,
                   text_content, payload
            FROM source_item_revisions
            WHERE id = %s
            """,
            (revision_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else SourceItemRevision.from_row(row)

    async def list_asset_summaries(
        self, conn: psycopg.AsyncConnection, revision_id: int
    ) -> list[dict]:
        """Light asset descriptors for one revision in insertion order.

        Relevance prompts include attachment kinds so the model can answer
        ``needs_media`` when the decisive evidence lives inside unseen media.
        """
        cursor = await conn.execute(
            """
            SELECT kind, metadata
            FROM source_assets
            WHERE source_item_revision_id = %s
            ORDER BY id
            """,
            (revision_id,),
        )
        rows = await cursor.fetchall()
        return [{"kind": row[0], "metadata": row[1]} for row in rows]

    async def list_asset_descriptors(
        self, conn: psycopg.AsyncConnection, revision_id: int
    ) -> list["AssetDescriptor"]:
        """Vision-input asset descriptors (ids + size/URL facts) in id order.

        No pixel bytes are loaded: Plan 2 ingestion stores metadata and the
        provider URL only, so vision operates on descriptors until a media
        downloader exists.
        """
        cursor = await conn.execute(
            """
            SELECT id, kind, external_url, mime_type, width, height, duration
            FROM source_assets
            WHERE source_item_revision_id = %s
            ORDER BY id
            """,
            (revision_id,),
        )
        rows = await cursor.fetchall()
        from src.processing.vision import AssetDescriptor

        return [
            AssetDescriptor(
                asset_id=row[0],
                kind=row[1],
                external_url=row[2],
                mime_type=row[3],
                width=row[4],
                height=row[5],
                duration=row[6],
            )
            for row in rows
        ]

    async def get_edition_name(self, conn: psycopg.AsyncConnection, edition_id: int) -> str | None:
        """Display name of one edition (relevance prompt context)."""
        cursor = await conn.execute("SELECT name FROM editions WHERE id = %s", (edition_id,))
        row = await cursor.fetchone()
        return None if row is None else str(row[0])

    async def list_source_edition_ids(
        self, conn: psycopg.AsyncConnection, source_id: int
    ) -> list[int]:
        """Edition ids the source is bound to, in stable id order.

        The relevance wiring fans every new revision out to one exact-policy
        job per bound edition.
        """
        cursor = await conn.execute(
            "SELECT edition_id FROM source_editions WHERE source_id = %s ORDER BY edition_id",
            (source_id,),
        )
        rows = await cursor.fetchall()
        return [int(row[0]) for row in rows]

    async def upsert_asset_for_revision(
        self, conn: psycopg.AsyncConnection, revision_id: int, asset: ObservedAsset
    ) -> None:
        """Insert or refresh the asset keyed by uq_source_assets_revision_identity.

        Identity is (revision, kind, external_url, content_hash) with NULLs
        coalesced, so album photos without per-photo URLs stay distinct rows;
        re-ingesting an identical asset refreshes its descriptor columns and
        inserts nothing, making duplicate batches idempotent at the DB level.
        """
        await conn.execute(
            """
            INSERT INTO source_assets (
                source_item_revision_id, kind, external_url, local_storage_ref,
                mime_type, content_hash, width, height, duration, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (
                source_item_revision_id,
                kind,
                COALESCE(external_url, ''),
                COALESCE(content_hash, '')
            )
            DO UPDATE SET
                local_storage_ref = EXCLUDED.local_storage_ref,
                mime_type = EXCLUDED.mime_type,
                width = EXCLUDED.width,
                height = EXCLUDED.height,
                duration = EXCLUDED.duration,
                metadata = EXCLUDED.metadata
            """,
            (
                revision_id,
                asset.kind,
                asset.external_url,
                None,
                asset.mime_type,
                asset.content_hash,
                None,
                None,
                None,
                Jsonb(asset.metadata),
            ),
        )

    async def insert_state_event(
        self, conn: psycopg.AsyncConnection, source_id: int, event: ObservedStateEvent
    ) -> int | None:
        """Append one state event; events for uncollected items are skipped.

        There is nothing to anchor history to when the item shell was never
        persisted, and state events stay append-only by convention.
        """
        item_id = await self._resolve_external_id(conn, source_id, event.item_external_id)
        if item_id is None:
            return None
        cursor = await conn.execute(
            """
            INSERT INTO source_item_state_events (
                source_item_id, type, observed_at, reason, evidence
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (item_id, event.type, event.observed_at, event.reason, Jsonb(event.evidence)),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("state event insert returned no row")
        event_id: int = row[0]
        return event_id

    async def start_run(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_id: int,
        trigger: str,
        started_at: datetime,
    ) -> CollectionRun:
        cursor = await conn.execute(
            """
            INSERT INTO collection_runs (source_id, trigger, started_at)
            VALUES (%s, %s, %s)
            RETURNING id, source_id, trigger, started_at, status
            """,
            (source_id, trigger, started_at),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("run insert returned no row")
        return CollectionRun.from_row(row)

    async def finish_run(
        self,
        conn: psycopg.AsyncConnection,
        *,
        run_id: int,
        outcome: str,
        completed_at: datetime,
        error_kind: str | None = None,
    ) -> None:
        await conn.execute(
            """
            UPDATE collection_runs
            SET status = %s, completed_at = %s, error_kind = %s
            WHERE id = %s
            """,
            (outcome, completed_at, error_kind, run_id),
        )

    async def update_checkpoint(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_id: int,
        adapter_state: dict[str, JSONValue],
        last_scan_at: datetime,
        last_success_at: datetime | None,
    ) -> None:
        """Upsert the checkpoint; every scan stamps last_scan_at, failed scans
        keep the previous success time."""
        await conn.execute(
            """
            INSERT INTO collection_checkpoints (
                source_id, last_scan_at, last_success_at, adapter_state
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE SET
                last_scan_at = EXCLUDED.last_scan_at,
                adapter_state = EXCLUDED.adapter_state,
                last_success_at = COALESCE(
                    EXCLUDED.last_success_at, collection_checkpoints.last_success_at
                )
            """,
            (source_id, last_scan_at, last_success_at, Jsonb(adapter_state)),
        )

    async def get_checkpoint(
        self, conn: psycopg.AsyncConnection, source_id: int
    ) -> CollectionCheckpoint | None:
        cursor = await conn.execute(
            """
            SELECT adapter_state, last_success_at, last_scan_at, cursor,
                   backoff_until, consecutive_failures
            FROM collection_checkpoints WHERE source_id = %s
            """,
            (source_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return CollectionCheckpoint(
            adapter_state=row[0],
            last_success_at=row[1],
            last_scan_at=row[2],
            cursor=row[3],
            backoff_until=row[4],
            consecutive_failures=row[5],
        )

    async def list_collection_candidates(
        self, conn: psycopg.AsyncConnection
    ) -> list[tuple[Source, CollectionCheckpoint | None]]:
        """Every enabled source joined with its checkpoint (None when absent).

        Feeds the periodic dispatcher: the schedule policy decides due-ness,
        this query only narrows to enabled sources in stable id order.
        """
        cursor = await conn.execute(
            """
            SELECT s.id, s.platform, s.kind, s.external_id, s.url, s.name,
                   s.role, s.enabled, s.collector_options, s.created_at, s.updated_at,
                   c.adapter_state, c.last_success_at, c.last_scan_at, c.cursor,
                   c.backoff_until, c.consecutive_failures
            FROM sources s
            LEFT JOIN collection_checkpoints c ON c.source_id = s.id
            WHERE s.enabled
            ORDER BY s.id
            """
        )
        rows = await cursor.fetchall()
        return [(Source.from_row(row[:11]), _checkpoint_from_row(row[11:])) for row in rows]

    async def apply_backoff(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_id: int,
        backoff_until: datetime,
    ) -> None:
        """Stamp the rate-limit resume time; the dispatcher honors it."""
        await conn.execute(
            "UPDATE collection_checkpoints SET backoff_until = %s WHERE source_id = %s",
            (backoff_until, source_id),
        )

    async def _resolve_external_id(
        self, conn: psycopg.AsyncConnection, source_id: int, external_id: str | None
    ) -> int | None:
        if external_id is None:
            return None
        cursor = await conn.execute(
            "SELECT id FROM source_items WHERE source_id = %s AND external_id = %s",
            (source_id, external_id),
        )
        row = await cursor.fetchone()
        return None if row is None else row[0]


def _checkpoint_from_row(row: tuple) -> CollectionCheckpoint | None:
    """Build a checkpoint from the joined columns; NULL row means no entry."""
    if row[0] is None:
        return None
    return CollectionCheckpoint(
        adapter_state=row[0],
        last_success_at=row[1],
        last_scan_at=row[2],
        cursor=row[3],
        backoff_until=row[4],
        consecutive_failures=row[5],
    )


def _content_hash(observation: ObservedItem) -> str:
    canonical = json.dumps(
        {"metadata": observation.metadata, "text": observation.text},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
