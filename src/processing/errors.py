"""Error taxonomy for Event-First revision processing."""

from __future__ import annotations

import psycopg
from psycopg_pool import PoolTimeout

from src.ai_providers import classify_provider_failure


def classify_processing_failure(exc: BaseException) -> str:
    """Classify database contention separately from provider failures."""
    if isinstance(exc, PoolTimeout):
        return "db_pool_timeout"
    if isinstance(exc, psycopg.errors.DeadlockDetected):
        return "db_deadlock"
    if isinstance(exc, psycopg.errors.SerializationFailure):
        return "db_serialization_failure"

    provider_kind = classify_provider_failure(exc)
    if provider_kind == "timeout":
        return "provider_timeout"
    if provider_kind == "context_size":
        return "provider_context_size"
    if provider_kind == "token_budget":
        return "provider_token_budget"
    if provider_kind == "quota":
        return "provider_quota"
    if provider_kind == "auth":
        return "provider_auth"
    if provider_kind == "server":
        return "provider_server"
    return "provider_failure"
