"""Source domain model."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Source:
    """A registered content source (immutable snapshot of a `sources` row)."""

    id: int
    platform: str
    kind: str
    external_id: str | None
    url: str | None
    name: str
    role: str
    enabled: bool
    collector_options: dict[str, Any]
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> Source:
        return cls(
            id=row[0],
            platform=row[1],
            kind=row[2],
            external_id=row[3],
            url=row[4],
            name=row[5],
            role=row[6],
            enabled=row[7],
            collector_options=row[8],
            created_at=row[9],
            updated_at=row[10],
        )


@dataclass(frozen=True)
class NewSource:
    """Input for creating a source; mirrors the `sources` table columns."""

    platform: str
    kind: str
    name: str
    external_id: str | None = None
    url: str | None = None
    role: str = "other"
    enabled: bool = True
    collector_options: dict[str, Any] = field(default_factory=dict)
