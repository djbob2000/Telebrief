"""Reporting window that continues where the previous issue of the same type ended.

A lookback-based issue (daily article, digest) covers ``lookback_hours`` before its
source cutoff, but never begins before the source cutoff of the previous succeeded
issue of the same edition and publication type (AGENTS.md §0.7). A late run after a
redeploy therefore cannot re-report material the previous issue already covered.
Calendar period reviews keep their own windows and never use this module.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.publication.digest_contracts import DIGEST_PUBLICATION_TYPES

WINDOW_METADATA_KEY = "publication_window"

# Lookback-based issues that chain their windows. Calendar reviews and the rolling
# weekly/legacy article types keep their existing windows.
WINDOW_CHAIN_PUBLICATION_TYPES: frozenset[str] = frozenset({"daily_article"}) | (
    DIGEST_PUBLICATION_TYPES
)

# Only the daily article treats previously published material as already known.
# The digest is a separate morning product and keeps its full window.
PREVIOUSLY_PUBLISHED_EXCLUSION_TYPES: frozenset[str] = frozenset({"daily_article"})

DEFAULT_LOOKBACK_HOURS = 24


@dataclass(frozen=True)
class PublicationWindowSpec:
    """Frozen reporting window of one publication run."""

    window_start: dt.datetime
    source_cutoff_at: dt.datetime
    lookback_hours: int
    previous_run_id: int | None = None
    previous_source_cutoff_at: dt.datetime | None = None


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def is_window_chain_type(publication_type: str | None) -> bool:
    return publication_type in WINDOW_CHAIN_PUBLICATION_TYPES


def compute_window_start(
    *,
    source_cutoff_at: dt.datetime,
    lookback_hours: int,
    previous_source_cutoff_at: dt.datetime | None = None,
) -> dt.datetime:
    """Start of the window: the lookback, but not before the previous issue's cutoff.

    The result is never earlier than the nominal lookback start, so the window is
    never longer than ``lookback_hours``. Without a previous cutoff, or when the
    previous cutoff is not earlier than this one, the nominal lookback applies.
    """
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    cutoff = _utc(source_cutoff_at)
    nominal_start = cutoff - dt.timedelta(hours=lookback_hours)
    if previous_source_cutoff_at is None:
        return nominal_start
    previous = _utc(previous_source_cutoff_at)
    if previous >= cutoff:
        return nominal_start
    return max(nominal_start, previous)


def build_window_spec(
    *,
    source_cutoff_at: dt.datetime,
    lookback_hours: int,
    previous_run_id: int | None = None,
    previous_source_cutoff_at: dt.datetime | None = None,
) -> PublicationWindowSpec:
    cutoff = _utc(source_cutoff_at)
    previous = _utc(previous_source_cutoff_at) if previous_source_cutoff_at is not None else None
    return PublicationWindowSpec(
        window_start=compute_window_start(
            source_cutoff_at=cutoff,
            lookback_hours=lookback_hours,
            previous_source_cutoff_at=previous,
        ),
        source_cutoff_at=cutoff,
        lookback_hours=lookback_hours,
        previous_run_id=previous_run_id if previous is not None else None,
        previous_source_cutoff_at=previous,
    )


def window_spec_to_metadata(spec: PublicationWindowSpec) -> dict[str, Any]:
    """JSON-safe value stored under ``publication_runs.metadata``."""
    return {
        WINDOW_METADATA_KEY: {
            "window_start": spec.window_start.isoformat(),
            "source_cutoff_at": spec.source_cutoff_at.isoformat(),
            "lookback_hours": spec.lookback_hours,
            "previous_run_id": spec.previous_run_id,
            "previous_source_cutoff_at": (
                spec.previous_source_cutoff_at.isoformat()
                if spec.previous_source_cutoff_at is not None
                else None
            ),
        }
    }


def window_spec_from_metadata(metadata: Mapping[str, Any] | None) -> PublicationWindowSpec | None:
    """Return the stored window of a sealed run, or None when it was never stored."""
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(WINDOW_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        previous_raw = raw.get("previous_source_cutoff_at")
        previous_run_raw = raw.get("previous_run_id")
        return PublicationWindowSpec(
            window_start=_utc(dt.datetime.fromisoformat(str(raw["window_start"]))),
            source_cutoff_at=_utc(dt.datetime.fromisoformat(str(raw["source_cutoff_at"]))),
            lookback_hours=int(raw["lookback_hours"]),
            previous_run_id=int(previous_run_raw) if previous_run_raw is not None else None,
            previous_source_cutoff_at=(
                _utc(dt.datetime.fromisoformat(str(previous_raw)))
                if previous_raw is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None
