"""Centralized digest contracts, categories, and constants (Plan 5 Task 1)."""

from __future__ import annotations

DIGEST_PUBLICATION_TYPES: frozenset[str] = frozenset({"digest", "digest_grouped", "digest_channel"})

HARD_EXCLUSION_REASONS: frozenset[str] = frozenset({"commercial_classified"})

DIGEST_CATEGORY_IDS: frozenset[str] = frozenset(
    {
        "utilities",
        "security",
        "transport",
        "telecom",
        "health",
        "culture",
        "social",
        "contacts",
        "other",
    }
)

GENERIC_FALLBACK_TOPICS: frozenset[str] = frozenset({"Городские события", "Новости города"})
