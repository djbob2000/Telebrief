"""Pure temporal resolution of operational states for recurring municipal subjects."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

from src.domain.event_payload import OPERATIONAL_STATES, OperationalObservationPayload


@dataclass(frozen=True)
class OperationalKey:
    """Canonical grouping key for operational subjects."""

    subject_key: str
    dimension: str
    location: str
    entity: str


@dataclass(frozen=True)
class ResolvedObservation:
    """Discrete operational observation with resolved effective timestamps and provenance."""

    observation: OperationalObservationPayload
    observed_at: dt.datetime
    source_refs: tuple[str, ...]
    effective_from: dt.datetime | None = None
    effective_until: dt.datetime | None = None


@dataclass(frozen=True)
class SubjectOperationalState:
    """Resolved point-in-time or latest operational state for a discrete subject."""

    subject_key: str
    subject_label: str
    dimension: str
    location: str
    entity: str
    current_state: str  # "AVAILABLE", "UNAVAILABLE", "RESTRICTED", "DEGRADED", "UNKNOWN", "CONFLICTING", "SCHEDULED"
    detail: str
    source_refs: tuple[str, ...]
    first_observed_at: dt.datetime
    last_observed_at: dt.datetime
    observation_count: int
    history: tuple[OperationalObservationPayload, ...]
    key: OperationalKey | None = None
    current_observations: tuple[ResolvedObservation, ...] = ()
    resolved_history: tuple[ResolvedObservation, ...] = ()
    next_scheduled_change: ResolvedObservation | None = None

    @property
    def status(self) -> str:
        return self.current_state


ResolvedOperationalState = SubjectOperationalState


def _parse_iso_ts(ts_val: str | dt.datetime | None) -> dt.datetime | None:
    if ts_val is None:
        return None
    if isinstance(ts_val, dt.datetime):
        return ts_val if ts_val.tzinfo is not None else ts_val.replace(tzinfo=dt.timezone.utc)
    if isinstance(ts_val, str) and ts_val.strip():
        try:
            parsed = dt.datetime.fromisoformat(ts_val.strip())
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)
        except Exception:
            return None
    return None


def _calculate_effective_state(item: ResolvedObservation, snap_ts: dt.datetime) -> str:
    raw_st = item.observation.state.upper()
    if item.effective_from is not None and item.effective_from <= snap_ts:
        # In effective window
        if item.effective_until is not None and snap_ts > item.effective_until:
            # Effective window has ended
            return "RESOLVED"
        # During effective window of scheduled outage, state is UNAVAILABLE
        if raw_st == "SCHEDULED":
            return "UNAVAILABLE"
    return raw_st if raw_st in OPERATIONAL_STATES else "UNKNOWN"


def resolve_operational_states(
    observations_with_time: Sequence[
        tuple[OperationalObservationPayload, dt.datetime, Sequence[str]]
    ],
    snapshot_at: dt.datetime | None = None,
    conflict_window_minutes: int = 90,
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
        raw_items = groups[key]

        # Convert to ResolvedObservation items
        resolved_items: list[ResolvedObservation] = []
        for obs, timestamp, refs in raw_items:
            obs_ts = (
                timestamp
                if timestamp.tzinfo is not None
                else timestamp.replace(tzinfo=dt.timezone.utc)
            )
            eff_from = _parse_iso_ts(obs.effective_from)
            eff_until = _parse_iso_ts(obs.effective_until)
            resolved_items.append(
                ResolvedObservation(
                    observation=obs,
                    observed_at=obs_ts,
                    source_refs=tuple(refs),
                    effective_from=eff_from,
                    effective_until=eff_until,
                )
            )

        # Sort chronologically by observed_at
        resolved_items.sort(key=lambda item: item.observed_at)

        first_ts = resolved_items[0].observed_at
        last_ts = resolved_items[-1].observed_at

        # Reference snapshot timestamp
        snap_ts = (
            snapshot_at
            if snapshot_at is not None
            else max(item.observed_at for item in resolved_items)
        )
        if snap_ts.tzinfo is None:
            snap_ts = snap_ts.replace(tzinfo=dt.timezone.utc)

        # Classify observations into active/past vs future scheduled
        future_scheduled: list[ResolvedObservation] = []
        active_or_past: list[ResolvedObservation] = []

        for item in resolved_items:
            if item.effective_from is not None and item.effective_from > snap_ts:
                future_scheduled.append(item)
            else:
                active_or_past.append(item)

        # Find next scheduled change if any
        next_scheduled: ResolvedObservation | None = None
        if future_scheduled:
            future_scheduled.sort(
                key=lambda x: x.effective_from or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
            )
            next_scheduled = future_scheduled[0]

        # Aggregate unique source refs across history in order of appearance
        seen_refs: set[str] = set()
        all_refs: list[str] = []
        history_obs: list[OperationalObservationPayload] = []

        for item in resolved_items:
            history_obs.append(item.observation)
            for r in item.source_refs:
                if r not in seen_refs:
                    seen_refs.add(r)
                    all_refs.append(r)

        current_obs: tuple[ResolvedObservation, ...]
        if not active_or_past:
            # Only future scheduled events exist
            current_st = "SCHEDULED"
            current_obs = (next_scheduled,) if next_scheduled else ()
            rep_obs = (
                next_scheduled.observation if next_scheduled else resolved_items[-1].observation
            )
            detail = rep_obs.detail
        else:
            latest_item = active_or_past[-1]
            rep_obs = latest_item.observation
            detail = rep_obs.detail

            # Check for conflicting observations within conflict window
            window_threshold = latest_item.observed_at - dt.timedelta(
                minutes=conflict_window_minutes
            )
            conflict_window_items = [
                item for item in active_or_past if item.observed_at >= window_threshold
            ]

            states_in_window = {
                _calculate_effective_state(item, snap_ts) for item in conflict_window_items
            }
            states_in_window.discard("UNKNOWN")

            # Check if there is a true conflict between contradictory operational states
            # (e.g. AVAILABLE vs UNAVAILABLE, AVAILABLE vs DEGRADED, UNAVAILABLE vs RESTRICTED)
            contradictory = (
                len(
                    states_in_window.intersection(
                        {"AVAILABLE", "UNAVAILABLE", "DEGRADED", "RESTRICTED"}
                    )
                )
                > 1
            )

            if contradictory:
                current_st = "CONFLICTING"
                current_obs = tuple(conflict_window_items)
            else:
                current_st = _calculate_effective_state(latest_item, snap_ts)
                current_obs = (latest_item,)

        op_key = OperationalKey(
            subject_key=rep_obs.subject_key,
            dimension=rep_obs.dimension,
            location=rep_obs.location,
            entity=rep_obs.entity,
        )

        resolved.append(
            SubjectOperationalState(
                subject_key=rep_obs.subject_key,
                subject_label=rep_obs.subject_label,
                dimension=rep_obs.dimension,
                location=rep_obs.location,
                entity=rep_obs.entity,
                current_state=current_st,
                detail=detail,
                source_refs=tuple(all_refs),
                first_observed_at=first_ts,
                last_observed_at=last_ts,
                observation_count=len(resolved_items),
                history=tuple(history_obs),
                key=op_key,
                current_observations=current_obs,
                resolved_history=tuple(resolved_items),
                next_scheduled_change=next_scheduled,
            )
        )

    return resolved
