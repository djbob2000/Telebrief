"""Centralized digest contracts, categories, and constants (Plan 5 Task 1)."""

from __future__ import annotations

DIGEST_PUBLICATION_TYPES: frozenset[str] = frozenset({"digest", "digest_grouped", "digest_channel"})

HARD_EXCLUSION_REASONS: frozenset[str] = frozenset(
    {
        "commercial_classified",
        "private_classified",
        "directory_payload",
        "obvious_noise",
    }
)

DIGEST_DISPOSITION_ELIGIBLE_PENDING_BUDGET = "eligible_pending_budget"
DIGEST_DISPOSITION_UNKNOWN_PRESERVED = "unknown_preserved"
DIGEST_ELIGIBILITY_VERSION = "v1"

GENERIC_FALLBACK_TOPICS: frozenset[str] = frozenset(
    {"Городские события", "Новости города", "Новости дня", "События дня", "Главные события"}
)
