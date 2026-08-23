"""Place evidence domain models: places, aliases, claim mentions/entities,
and versioned place-resolution scaffolding (migrations/0007).

Immutable snapshots of Plan 3 schema rows. Every persisted dataclass exposes
a ``from_row`` mapper whose positional order matches the explicit SELECT
column lists used by ``src/repositories/places.py``.

This module also owns :func:`normalize_place_text` — THE single place-text
normalization contract shared by alias seeding, mention lookups, and entity
rows.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

# Locale-independent case folding mirrors src/repositories/story_candidates.py:
# C-locale databases fold neither lower() nor translate()-free expressions
# beyond ASCII, so the Cyrillic/Ukrainian map is spelled out explicitly.
_FOLD_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯЄІЇҐ"
_FOLD_LOWER = "abcdefghijklmnopqrstuvwxyzабвгдеёжзийклмнопрстуфхцчшщъыьэюяєіїґ"
_FOLD_MAP = str.maketrans(_FOLD_UPPER, _FOLD_LOWER)

_NON_WORD = re.compile(r"[^\w]+", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+", flags=re.UNICODE)


def normalize_place_text(text: str) -> str:
    """The documented place-text key: lowercase, punctuation→space, collapse.

    Word characters (letters AND digits) survive — numbered areas such as
    «Округ №32» normalize to ``округ 32`` so their alias variants collide
    on one deterministic key regardless of spelling or punctuation.
    """
    folded = text.strip().translate(_FOLD_MAP).lower()
    punctuated = _NON_WORD.sub(" ", folded)
    return _WHITESPACE.sub(" ", punctuated).strip()


@dataclass(frozen=True)
class Place:
    """A `places` row: one real-world location with optional hierarchy.

    ``parent_place_id`` is a self-reference (street within district within
    city); ``place_id`` resolution outcomes may legitimately stay NULL.
    """

    id: int
    canonical_name: str
    kind: str | None
    parent_place_id: int | None
    latitude: float | None
    longitude: float | None
    metadata: dict[str, Any]
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> Place:
        return cls(
            id=row[0],
            canonical_name=row[1],
            kind=row[2],
            parent_place_id=row[3],
            latitude=None if row[4] is None else float(row[4]),
            longitude=None if row[5] is None else float(row[5]),
            metadata=row[6],
            created_at=row[7],
        )


@dataclass(frozen=True)
class PlaceAlias:
    """A `place_aliases` row. ``normalized_alias`` is indexed but NOT unique:
    several places may share a colloquial label by design."""

    id: int
    place_id: int
    alias: str
    normalized_alias: str

    @classmethod
    def from_row(cls, row: Any) -> PlaceAlias:
        return cls(id=row[0], place_id=row[1], alias=row[2], normalized_alias=row[3])


@dataclass(frozen=True)
class ClaimPlaceMention:
    """A `claim_place_mentions` row: the verbatim unresolved mention text of
    one immutable claim. Resolution never mutates claims or mentions."""

    id: int
    claim_id: int
    role: str | None
    original_text: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> ClaimPlaceMention:
        return cls(
            id=row[0],
            claim_id=row[1],
            role=row[2],
            original_text=row[3],
            created_at=row[4],
        )


@dataclass(frozen=True)
class ClaimEntity:
    """A `claim_entities` row: one lightweight normalized entity string."""

    id: int
    claim_id: int
    normalized_text: str
    entity_kind: str | None
    metadata: dict[str, Any]
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> ClaimEntity:
        return cls(
            id=row[0],
            claim_id=row[1],
            normalized_text=row[2],
            entity_kind=row[3],
            metadata=row[4],
            created_at=row[5],
        )


@dataclass(frozen=True)
class PlaceResolutionPolicyVersion:
    """A `place_resolution_policy_versions` row: per-edition resolver identity."""

    id: int
    edition_id: int
    version: int
    config_hash: str
    prompt_version: str
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> PlaceResolutionPolicyVersion:
        return cls(
            id=row[0],
            edition_id=row[1],
            version=row[2],
            config_hash=row[3],
            prompt_version=row[4],
            created_at=row[5],
        )


@dataclass(frozen=True)
class PlaceResolutionRun:
    """A `place_resolution_runs` row; at most one per (mention, policy) ever
    reaches status 'succeeded' via the guarded transition."""

    id: int
    mention_id: int
    edition_id: int
    policy_id: int
    started_at: dt.datetime
    completed_at: dt.datetime | None
    status: str
    error_kind: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: Any) -> PlaceResolutionRun:
        return cls(
            id=row[0],
            mention_id=row[1],
            edition_id=row[2],
            policy_id=row[3],
            started_at=row[4],
            completed_at=row[5],
            status=row[6],
            error_kind=row[7],
            metadata=row[8],
        )


@dataclass(frozen=True)
class PlaceResolutionResult:
    """A `place_resolution_results` row. ``status`` is resolved|unresolved;
    ``place_id IS NULL`` with status 'unresolved' is a COMPLETED outcome."""

    id: int
    run_id: int
    mention_id: int
    policy_id: int
    place_id: int | None
    status: str
    confidence: float | None
    reason: str | None
    created_at: dt.datetime

    @classmethod
    def from_row(cls, row: Any) -> PlaceResolutionResult:
        return cls(
            id=row[0],
            run_id=row[1],
            mention_id=row[2],
            policy_id=row[3],
            place_id=row[4],
            status=row[5],
            confidence=None if row[6] is None else float(row[6]),
            reason=row[7],
            created_at=row[8],
        )


@dataclass(frozen=True)
class NewPlace:
    """Input for seeding one place; external identity enables idempotent imports."""

    canonical_name: str
    kind: str | None = None
    parent_place_id: int | None = None
    latitude: float | None = None
    longitude: float | None = None
    metadata: dict[str, Any] | None = None
    profile_key: str | None = None
