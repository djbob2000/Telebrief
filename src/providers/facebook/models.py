"""Facebook provider models and domain data structures (Plan 5 Task 1)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class FacebookAuthProfile:
    id: int
    name: str
    storage_ref: str
    status: str
    last_verified_at: dt.datetime | None = None
    error_kind: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> FacebookAuthProfile:
        if row is None:
            raise ValueError("cannot construct FacebookAuthProfile from None row")
        return cls(
            id=row[0],
            name=row[1],
            storage_ref=row[2],
            status=row[3],
            last_verified_at=row[4],
            error_kind=row[5],
            error_message=row[6],
            metadata=row[7] if isinstance(row[7], dict) else {},
            created_at=row[8] if len(row) > 8 else None,
            updated_at=row[9] if len(row) > 9 else None,
        )


@dataclass(slots=True, frozen=True)
class FacebookSourceConfig:
    id: int
    source_id: int
    auth_profile_id: int
    group_or_page_id: str | None
    url: str
    scan_times: list[str]
    timezone: str
    collector_options: dict[str, Any] = field(default_factory=dict)
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> FacebookSourceConfig:
        if row is None:
            raise ValueError("cannot construct FacebookSourceConfig from None row")
        return cls(
            id=row[0],
            source_id=row[1],
            auth_profile_id=row[2],
            group_or_page_id=row[3],
            url=row[4],
            scan_times=list(row[5]) if row[5] is not None else ["08:00", "12:00", "16:00", "19:30"],
            timezone=row[6] or "UTC",
            collector_options=row[7] if isinstance(row[7], dict) else {},
            created_at=row[8] if len(row) > 8 else None,
            updated_at=row[9] if len(row) > 9 else None,
        )


@dataclass(slots=True, frozen=True)
class FacebookCommentState:
    id: int
    source_item_id: int
    last_scanned_at: dt.datetime | None = None
    oldest_comment_published_at: dt.datetime | None = None
    newest_comment_published_at: dt.datetime | None = None
    total_comments_observed: int = 0
    completeness: str = "partial"
    continuation_state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> FacebookCommentState:
        if row is None:
            raise ValueError("cannot construct FacebookCommentState from None row")
        return cls(
            id=row[0],
            source_item_id=row[1],
            last_scanned_at=row[2],
            oldest_comment_published_at=row[3],
            newest_comment_published_at=row[4],
            total_comments_observed=row[5] or 0,
            completeness=row[6] or "partial",
            continuation_state=row[7] if isinstance(row[7], dict) else {},
            metadata=row[8] if isinstance(row[8], dict) else {},
            created_at=row[9] if len(row) > 9 else None,
            updated_at=row[10] if len(row) > 10 else None,
        )


@dataclass(slots=True, frozen=True)
class CollectorArtifact:
    id: int
    source_id: int
    artifact_type: str
    storage_path: str
    expires_at: dt.datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: dt.datetime | None = None

    @classmethod
    def from_row(cls, row: Any) -> CollectorArtifact:
        if row is None:
            raise ValueError("cannot construct CollectorArtifact from None row")
        return cls(
            id=row[0],
            source_id=row[1],
            artifact_type=row[2],
            storage_path=row[3],
            expires_at=row[4],
            metadata=row[5] if isinstance(row[5], dict) else {},
            created_at=row[6] if len(row) > 6 else None,
        )
