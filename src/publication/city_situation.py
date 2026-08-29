"""CitySituationRollup domain models and renderer for digest operational summaries."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Sequence

from src.domain.operational_state import SubjectOperationalState


@dataclass(frozen=True)
class CitySituationItem:
    """A discrete municipal/operational subject and its latest resolved state."""

    subject_key: str
    subject_label: str
    dimension: str
    location: str
    entity: str
    state: str
    detail: str
    source_refs: tuple[str, ...]
    first_observed_at: dt.datetime
    last_observed_at: dt.datetime
    observation_count: int


@dataclass(frozen=True)
class CitySituationRollup:
    """Aggregated operational situation for municipal digests."""

    items: tuple[CitySituationItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [
                {
                    "subject_key": item.subject_key,
                    "subject_label": item.subject_label,
                    "dimension": item.dimension,
                    "location": item.location,
                    "entity": item.entity,
                    "state": item.state,
                    "detail": item.detail,
                    "source_refs": list(item.source_refs),
                    "first_observed_at": item.first_observed_at.isoformat(),
                    "last_observed_at": item.last_observed_at.isoformat(),
                    "observation_count": item.observation_count,
                }
                for item in self.items
            ]
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CitySituationRollup:
        items = []
        for d in data.get("items", []):
            f_ts = (
                dt.datetime.fromisoformat(d["first_observed_at"])
                if "first_observed_at" in d and d["first_observed_at"]
                else dt.datetime.now(dt.timezone.utc)
            )
            l_ts = (
                dt.datetime.fromisoformat(d["last_observed_at"])
                if "last_observed_at" in d and d["last_observed_at"]
                else dt.datetime.now(dt.timezone.utc)
            )
            items.append(
                CitySituationItem(
                    subject_key=str(d.get("subject_key", "")),
                    subject_label=str(d.get("subject_label", "")),
                    dimension=str(d.get("dimension", "")),
                    location=str(d.get("location", "")),
                    entity=str(d.get("entity", "")),
                    state=str(d.get("state", "")),
                    detail=str(d.get("detail", "")),
                    source_refs=tuple(str(r) for r in d.get("source_refs", [])),
                    first_observed_at=f_ts,
                    last_observed_at=l_ts,
                    observation_count=int(d.get("observation_count", 1)),
                )
            )
        return cls(items=tuple(items))


def build_city_situation_rollup(
    resolved_states: Sequence[SubjectOperationalState],
) -> CitySituationRollup:
    """Package resolved subject operational states into a CitySituationRollup."""
    items = tuple(
        CitySituationItem(
            subject_key=st.subject_key,
            subject_label=st.subject_label,
            dimension=st.dimension,
            location=st.location,
            entity=st.entity,
            state=st.current_state,
            detail=st.detail,
            source_refs=st.source_refs,
            first_observed_at=st.first_observed_at,
            last_observed_at=st.last_observed_at,
            observation_count=st.observation_count,
        )
        for st in resolved_states
    )
    return CitySituationRollup(items=items)


def render_city_situation_section(rollup: CitySituationRollup | None) -> str:
    """Render the city situation section formatted for Telegram HTML messages."""
    if not rollup or not rollup.items:
        return ""
    lines = ["<b>Городская обстановка:</b>"]
    for item in rollup.items:
        state_icon = (
            "🟢"
            if item.state == "AVAILABLE"
            else ("🔴" if item.state in ("UNAVAILABLE", "DISRUPTED") else "🟡")
        )
        loc_str = f" ({item.location})" if item.location else ""
        label = item.subject_label or item.subject_key
        lines.append(f"{state_icon} <b>{label}{loc_str}</b>: {item.detail}")
    return "\n".join(lines)
