"""Provider-neutral observation DTOs shared by every collector.

Collectors translate provider-specific payloads into these frozen types and
never touch the database; the ingestion service persists them inside one
transaction. ``ObservedAsset`` and ``ObservedStateEvent`` address their parent
item by stable ``item_external_id`` — the service resolves that identity to the
exact SourceItem/Revision inside its transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

type JSONValue = str | int | float | bool | None | dict[str, JSONValue] | list[JSONValue]


class CollectionOutcome(str, Enum):
    """Terminal scan outcomes; values match the collection_runs.status CHECK."""

    SUCCESS = "success"
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    AUTH_REQUIRED = "auth_required"
    ACCOUNT_ACTION_REQUIRED = "account_action_required"
    ACCESS_DENIED = "access_denied"
    SOURCE_NOT_FOUND = "source_not_found"
    LAYOUT_CHANGED = "layout_changed"
    PERMANENT = "permanent"


class CollectionTrigger(str, Enum):
    """What started the collection run; values match the trigger CHECK."""

    SCHEDULED = "scheduled"
    PRE_PUBLISH = "pre_publish"
    MANUAL = "manual"
    BACKFILL = "backfill"


@dataclass(frozen=True)
class ObservedItem:
    """One observed source item (message/post) in provider-neutral form."""

    kind: str
    external_id: str
    text: str
    author_name: str | None
    published_at: datetime | None
    canonical_url: str | None
    metadata: dict[str, JSONValue]
    observed_at: datetime
    parent_external_id: str | None = None
    root_external_id: str | None = None

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("external_id must be a non-empty stable identifier")


@dataclass(frozen=True)
class ObservedAsset:
    """An attachment observed on an item, bound by its stable external id."""

    item_external_id: str
    kind: str
    external_url: str | None
    mime_type: str | None
    content_hash: str | None
    metadata: dict[str, JSONValue]


@dataclass(frozen=True)
class ObservedStateEvent:
    """Append-only state observation (`deleted_at_source`, `inaccessible`,
    `restored`, ...) anchored to an item's stable external id."""

    item_external_id: str
    type: str
    observed_at: datetime
    reason: str
    evidence: dict[str, JSONValue]


@dataclass(frozen=True)
class CollectionBatch:
    """The complete result of one collector scan."""

    outcome: CollectionOutcome
    items: tuple[ObservedItem, ...]
    assets: tuple[ObservedAsset, ...]
    state_events: tuple[ObservedStateEvent, ...]
    adapter_state: dict[str, JSONValue]
    started_at: datetime
    completed_at: datetime
    error_kind: str | None = None


@dataclass(frozen=True)
class CollectionCheckpoint:
    """Resume point handed back to collectors; mirrors collection_checkpoints."""

    adapter_state: dict[str, JSONValue] = field(default_factory=dict)
    last_success_at: datetime | None = None
    last_scan_at: datetime | None = None
    cursor: JSONValue | None = None
    backoff_until: datetime | None = None
    consecutive_failures: int = 0
