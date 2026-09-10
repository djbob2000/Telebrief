"""Immutable targets used by Event-First authority processing."""

from __future__ import annotations

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
