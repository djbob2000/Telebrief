"""Edition domain model."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Edition:
    """A publication edition (immutable snapshot of an `editions` row)."""

    id: int
    slug: str
    name: str
    timezone: str
    language: str
    profile: dict[str, Any]
    config: dict[str, Any]
    enabled: bool
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> Edition:
        return cls(
            id=row[0],
            slug=row[1],
            name=row[2],
            timezone=row[3],
            language=row[4],
            profile=row[5],
            config=row[6],
            enabled=row[7],
            created_at=row[8],
            updated_at=row[9],
        )


@dataclass(frozen=True)
class NewEdition:
    """Input for creating an edition; mirrors the `editions` table columns."""

    slug: str
    name: str
    timezone: str = "UTC"
    language: str = "ru"
    profile: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
