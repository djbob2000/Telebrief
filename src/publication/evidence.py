"""Publication-facing evidence models and fragment provenance mapping."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

SERVICE_SUBJECT_FAMILY_BY_KEY: dict[str, str] = {
    "water_supply": "water",
    "power_supply": "power",
    "gas_supply": "gas",
    "heating": "heating",
    "connectivity": "telecom",
}


@dataclass(frozen=True)
class ServiceSubjectHint:
    """Optional advisory topic copied from structured service-state evidence."""

    subject_key: str
    subject_label: str
    family: str | None


@dataclass(frozen=True)
class PublicationEvidence:
    """A single factual evidence unit bound to an exact source fragment."""

    evidence_id: str
    story_id: int
    text: str
    source_text: str
    kind: str
    publication_use: Literal["PUBLISH", "CONTEXT", "EXCLUDE"]
    fragment_id: int
    source_ref: str
    source_id: int
    source_item_id: int
    source_role: str
    observed_at: dt.datetime
    # Explicit reply-parent context is retained separately from the reply fact.
    # It may explain a subject/place, but must never ground the reply's claim.
    reply_parent_context_text: str = ""
    reply_parent_item_id: int | None = None
    service_subject_hint: ServiceSubjectHint | None = None
