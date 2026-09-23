"""Shared handling for IANA timezone identifiers used by Telebrief."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_LEGACY_TIMEZONE_ALIASES = {
    "Europe/Kiev": "Europe/Kyiv",
    "Europe/Zaporozhye": "Europe/Kyiv",
}


def normalize_timezone_name(timezone_name: str) -> str:
    """Return the canonical identifier for timezone names persisted by older editions."""
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError(f"Invalid timezone: {timezone_name!r}")
    normalized = timezone_name.strip()
    return _LEGACY_TIMEZONE_ALIASES.get(normalized, normalized)


def get_timezone(timezone_name: str) -> ZoneInfo:
    """Resolve a timezone, accepting known legacy aliases and rejecting unknown names."""
    normalized = normalize_timezone_name(timezone_name)
    try:
        return ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Invalid timezone: {timezone_name!r}") from exc
