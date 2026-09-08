"""Deterministic retry and quarantine policy for Event-First stages."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryDecision:
    """Disposition after one failed stage call."""

    retry: bool
    exhausted: bool
    delay_seconds: int
    next_retry_at: dt.datetime | None


def decide_retry(
    error_kind: str,
    *,
    attempt_count: int,
    max_attempts: int,
    base_backoff_seconds: int,
    max_backoff_seconds: int,
    now: dt.datetime | None = None,
) -> RetryDecision:
    """Return whether the next periodic cycle may retry this assignment."""
    if attempt_count >= max_attempts or error_kind == "auth":
        return RetryDecision(retry=False, exhausted=True, delay_seconds=0, next_retry_at=None)

    if error_kind in {"quota", "server", "timeout"}:
        delay = min(
            max(0, base_backoff_seconds) * (2 ** max(0, attempt_count - 1)),
            max_backoff_seconds,
        )
        current = now or dt.datetime.now(dt.timezone.utc)
        return RetryDecision(
            retry=True,
            exhausted=False,
            delay_seconds=delay,
            next_retry_at=current + dt.timedelta(seconds=delay),
        )

    return RetryDecision(retry=True, exhausted=False, delay_seconds=0, next_retry_at=now)
