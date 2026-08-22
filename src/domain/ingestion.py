"""Ingestion domain models: immutable snapshots of generic source-history rows."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourceItem:
    """A persisted `source_items` row; identity is UNIQUE(source_id, external_id)."""

    id: int
    source_id: int
    kind: str
    external_id: str
    parent_item_id: int | None
    root_item_id: int | None
    author_name: str | None
    author_external_id: str | None
    canonical_url: str | None
    published_at: dt.datetime | None
    first_collected_at: dt.datetime
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> SourceItem:
        return cls(
            id=row[0],
            source_id=row[1],
            kind=row[2],
            external_id=row[3],
            parent_item_id=row[4],
            root_item_id=row[5],
            author_name=row[6],
            author_external_id=row[7],
            canonical_url=row[8],
            published_at=row[9],
            first_collected_at=row[10],
            metadata=row[11],
        )


@dataclass(frozen=True)
class SourceItemRevision:
    """An immutable `source_item_revisions` row (append-only by convention)."""

    id: int
    source_item_id: int
    revision_no: int
    collected_at: dt.datetime
    content_hash: str
    text_content: str | None
    payload: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> SourceItemRevision:
        return cls(
            id=row[0],
            source_item_id=row[1],
            revision_no=row[2],
            collected_at=row[3],
            content_hash=row[4],
            text_content=row[5],
            payload=row[6],
        )


@dataclass(frozen=True)
class CollectionRun:
    """A `collection_runs` bookkeeping row as opened by ``start_run``."""

    id: int
    source_id: int
    trigger: str
    started_at: dt.datetime
    status: str

    @classmethod
    def from_row(cls, row: Any) -> CollectionRun:
        return cls(
            id=row[0],
            source_id=row[1],
            trigger=row[2],
            started_at=row[3],
            status=row[4],
        )
