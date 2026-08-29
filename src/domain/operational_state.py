"""Pure temporal resolution of operational states for recurring municipal subjects."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

from src.domain.event_payload import OPERATIONAL_STATES, OperationalObservationPayload


@dataclass(frozen=True)
class SubjectOperationalState:
    """Resolved point-in-time or latest operational state for a discrete subject."""

    subject_key: str
    subject_label: str
    dimension: str
    location: str
    entity: str
    current_state: str  # e.g. "AVAILABLE", "UNAVAILABLE", "RESTRICTED", "DEGRADED", "RESOLVED"
    detail: str
    source_refs: tuple[str, ...]
    first_observed_at: dt.datetime
    last_observed_at: dt.datetime
    observation_count: int
    history: tuple[OperationalObservationPayload, ...]


def resolve_operational_states(
    observations_with_time: Sequence[
        tuple[OperationalObservationPayload, dt.datetime, Sequence[str]]
    ],
) -> list[SubjectOperationalState]:
    """Deterministically aggregate observations into current operational states ordered by subject key."""
    if not observations_with_time:
        return []

    # Group by (subject_key, dimension, location, entity)
    # Using case-folded canonical tuple for grouping, but retaining original casing from latest
    groups: dict[
        tuple[str, str, str, str],
        list[tuple[OperationalObservationPayload, dt.datetime, Sequence[str]]],
    ] = {}

    for obs, timestamp, refs in observations_with_time:
        key = (
            obs.subject_key.strip().casefold(),
            obs.dimension.strip().casefold(),
            obs.location.strip().casefold(),
            obs.entity.strip().casefold(),
        )
        if key not in groups:
            groups[key] = []
        groups[key].append((obs, timestamp, refs))

    resolved: list[SubjectOperationalState] = []

    # Deterministic processing order
    for key in sorted(groups.keys()):
        items = groups[key]
        # Sort chronologically
        items.sort(key=lambda item: item[1])

        first_ts = items[0][1]
        last_item = items[-1]
        last_obs, last_ts, _ = last_item

        # Aggregate unique source refs across history in order of appearance
        seen_refs: set[str] = set()
        all_refs: list[str] = []
        history_obs: list[OperationalObservationPayload] = []

        for item_obs, _, item_refs in items:
            history_obs.append(item_obs)
            for r in item_refs:
                if r not in seen_refs:
                    seen_refs.add(r)
                    all_refs.append(r)

        current_st = (
            last_obs.state.upper() if last_obs.state.upper() in OPERATIONAL_STATES else "UNKNOWN"
        )

        resolved.append(
            SubjectOperationalState(
                subject_key=last_obs.subject_key,
                subject_label=last_obs.subject_label,
                dimension=last_obs.dimension,
                location=last_obs.location,
                entity=last_obs.entity,
                current_state=current_st,
                detail=last_obs.detail,
                source_refs=tuple(all_refs),
                first_observed_at=first_ts,
                last_observed_at=last_ts,
                observation_count=len(items),
                history=tuple(history_obs),
            )
        )

    return resolved
