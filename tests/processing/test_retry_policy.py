"""Unit tests for deterministic Event-First retry dispositions."""

from __future__ import annotations

import datetime as dt

import pytest

from src.processing.retry_policy import decide_retry


@pytest.mark.parametrize("kind", ["auth"])
def test_auth_is_exhausted_immediately(kind: str):
    decision = decide_retry(
        kind,
        attempt_count=1,
        max_attempts=2,
        base_backoff_seconds=300,
        max_backoff_seconds=3600,
    )
    assert decision.exhausted is True
    assert decision.retry is False
    assert decision.next_retry_at is None


@pytest.mark.parametrize("kind", ["token_budget", "context_size", "other"])
def test_non_transient_errors_retry_then_exhaust(kind: str):
    first = decide_retry(
        kind,
        attempt_count=1,
        max_attempts=2,
        base_backoff_seconds=300,
        max_backoff_seconds=3600,
    )
    second = decide_retry(
        kind,
        attempt_count=2,
        max_attempts=2,
        base_backoff_seconds=300,
        max_backoff_seconds=3600,
    )
    assert first.retry is True and first.exhausted is False
    assert second.retry is False and second.exhausted is True


def test_transient_errors_use_capped_exponential_backoff():
    now = dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc)
    first = decide_retry(
        "server",
        attempt_count=1,
        max_attempts=3,
        base_backoff_seconds=300,
        max_backoff_seconds=3600,
        now=now,
    )
    third = decide_retry(
        "timeout",
        attempt_count=3,
        max_attempts=4,
        base_backoff_seconds=300,
        max_backoff_seconds=3600,
        now=now,
    )
    assert first.delay_seconds == 300
    assert first.next_retry_at == now + dt.timedelta(seconds=300)
    assert third.delay_seconds == 1200


def test_attempt_limit_exhausts_without_same_cycle_retry():
    decision = decide_retry(
        "quota",
        attempt_count=2,
        max_attempts=2,
        base_backoff_seconds=300,
        max_backoff_seconds=3600,
    )
    assert decision.exhausted is True
    assert decision.next_retry_at is None
