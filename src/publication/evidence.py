"""Publication-facing evidence models and fragment provenance mapping."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PublicationEvidence:
    """A single factual evidence unit bound to an exact source fragment."""

    evidence_id: str
    story_id: int
    text: str
    kind: str
    publication_use: Literal["PUBLISH", "CONTEXT", "EXCLUDE"]
    fragment_id: int
    source_ref: str
    source_id: int
    source_item_id: int
    source_role: str
    observed_at: dt.datetime
