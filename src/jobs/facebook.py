"""Procrastinate task definitions and execution lock resolvers for Facebook (Plan 5 Task 3)."""

from __future__ import annotations

import logging

from src.domain.sources import Source

logger = logging.getLogger(__name__)


def resolve_facebook_execution_lock(source: Source) -> str:
    """Resolve Facebook profile lock for serialization across sources sharing the profile."""
    options = source.collector_options or {}
    auth_profile = options.get("auth_profile") or options.get("auth_profile_id") or "default"
    return f"facebook-auth-profile:{auth_profile}"
