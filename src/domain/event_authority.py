"""Immutable targets used by Event-First authority processing."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthorityTarget:
    """One Story/assignment and the authority policy that must satisfy it."""

    story_id: int
    assignment_id: int
    edition_id: int
    triage_version: str
    scope_version: str
    scope_config_hash: str
    source_cutoff_at: dt.datetime | None = None
    snapshot_at: dt.datetime | None = None


@dataclass(frozen=True)
class AuthorityBarrierState:
    """Exact authority evidence and the durable reasons a gap remains."""

    gap_targets: tuple[AuthorityTarget, ...]
    completed_at: dt.datetime | None
    terminal_count: int
    retry_exceeds_deadline_count: int
    earliest_retry_at: dt.datetime | None
    processable_now_count: int
    in_flight_count: int
