"""Domain models for Event-First pipeline (fragments, embeddings, clusters, triage, analysis)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourceFragment:
    """A deterministic text fragment extracted from a source_item_revision."""

    id: int
    source_item_revision_id: int
    ordinal: int
    text_content: str
    normalized_hash: str
    fragmenter_version: str
    is_candidate: bool
    drop_reason: str | None
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> SourceFragment:
        return cls(
            id=row[0],
            source_item_revision_id=row[1],
            ordinal=row[2],
            text_content=row[3],
            normalized_hash=row[4],
            fragmenter_version=row[5],
            is_candidate=row[6],
            drop_reason=row[7],
            created_at=row[8],
        )


@dataclass(frozen=True)
class NewSourceFragment:
    """Unpersisted fragment ready for insertion."""

    ordinal: int
    text_content: str
    normalized_hash: str
    fragmenter_version: str
    is_candidate: bool
    drop_reason: str | None = None
