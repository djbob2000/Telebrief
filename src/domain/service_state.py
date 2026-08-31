"""Canonical service state evidence model."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

SERVICE_STATE_VALUES = frozenset(
    {"AVAILABLE", "UNAVAILABLE", "DEGRADED", "RESTRICTED", "UNKNOWN", "SCHEDULED"}
)

SERVICE_STATE_BASES = frozenset(
    {
        "normal_operation",
        "direct_failure",
        "degraded_access",
        "explicit_restriction",
        "scheduled_change",
    }
)

ServiceStateBasis = Literal[
    "normal_operation",
    "direct_failure",
    "degraded_access",
    "explicit_restriction",
    "scheduled_change",
]


def _parse_optional_iso(value: Any, *, field_name: str) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    return text


@dataclass(frozen=True)
class ServiceStatePayload:
    """Structured operational service state embedded in service_access evidence."""

    subject_key: str
    subject_label: str
    dimension: str
    state: str
    location: str = ""
    entity: str = ""
    expected_now: bool | None = None
    basis: ServiceStateBasis = "normal_operation"
    effective_from: str | None = None
    effective_until: str | None = None

    def __post_init__(self) -> None:
        if not self.subject_key.strip():
            raise ValueError("service_state.subject_key cannot be empty")
        if not self.subject_label.strip():
            raise ValueError("service_state.subject_label cannot be empty")
        if not self.dimension.strip():
            raise ValueError("service_state.dimension cannot be empty")
        if self.state.upper() not in SERVICE_STATE_VALUES:
            raise ValueError(f"invalid service_state.state: {self.state}")
        if self.basis not in SERVICE_STATE_BASES:
            raise ValueError(f"invalid service_state.basis: {self.basis}")

        if self.effective_from is not None:
            _parse_optional_iso(self.effective_from, field_name="effective_from")
        if self.effective_until is not None:
            _parse_optional_iso(self.effective_until, field_name="effective_until")

        if self.effective_from is not None and self.effective_until is not None:
            dt_from = dt.datetime.fromisoformat(self.effective_from)
            dt_until = dt.datetime.fromisoformat(self.effective_until)
            if dt_until < dt_from:
                raise ValueError(
                    f"effective_until ({self.effective_until}) cannot be earlier than effective_from ({self.effective_from})"
                )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "subject_key": self.subject_key,
            "subject_label": self.subject_label,
            "dimension": self.dimension,
            "state": self.state.upper(),
            "location": self.location,
            "entity": self.entity,
            "expected_now": self.expected_now,
            "basis": self.basis,
        }
        if self.effective_from is not None:
            data["effective_from"] = self.effective_from
        if self.effective_until is not None:
            data["effective_until"] = self.effective_until
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ServiceStatePayload:
        raw_expected = data.get("expected_now")
        if raw_expected is not None and not isinstance(raw_expected, bool):
            raise ValueError("service_state.expected_now must be boolean or null")

        raw_basis = str(data.get("basis", "normal_operation")).strip()
        if raw_basis not in SERVICE_STATE_BASES:
            raise ValueError(f"invalid service_state.basis: {raw_basis}")

        return cls(
            subject_key=str(data.get("subject_key", "")).strip(),
            subject_label=str(data.get("subject_label", "")).strip(),
            dimension=str(data.get("dimension", "availability")).strip(),
            state=str(data.get("state", "")).strip().upper(),
            location=str(data.get("location", "")).strip(),
            entity=str(data.get("entity", "")).strip(),
            expected_now=raw_expected,
            basis=cast(ServiceStateBasis, raw_basis),
            effective_from=_parse_optional_iso(
                data.get("effective_from"), field_name="effective_from"
            ),
            effective_until=_parse_optional_iso(
                data.get("effective_until"), field_name="effective_until"
            ),
        )
