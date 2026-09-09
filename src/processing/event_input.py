"""Versioned semantic identity for Event-First processing input."""

from __future__ import annotations

import hashlib
import json

from src.processing.fragments import FRAGMENTER_VERSION, split_into_fragments

EVENT_INPUT_VERSION = "text-v1"
DEFAULT_FRAGMENTER_VERSION = FRAGMENTER_VERSION


def build_event_processing_fingerprint(
    text: str | None,
    *,
    fragmenter_version: str = DEFAULT_FRAGMENTER_VERSION,
    event_input_version: str = EVENT_INPUT_VERSION,
) -> str:
    """Return a stable fingerprint for the text Event-First actually consumes.

    The fingerprint deliberately excludes media and source metadata.  It is
    based on the existing deterministic fragmenter, and includes explicit
    version fields so changing either semantic contract invalidates reuse.
    """
    fragments = split_into_fragments(
        text or "",
        fragmenter_version=fragmenter_version,
    )
    semantic_fragments = [
        {
            "ordinal": fragment.ordinal,
            "normalized_hash": fragment.normalized_hash,
            "is_candidate": fragment.is_candidate,
            "drop_reason": fragment.drop_reason,
        }
        for fragment in fragments
    ]
    payload = {
        "event_input_version": event_input_version,
        "fragmenter_version": fragmenter_version,
        "fragments": semantic_fragments,
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
