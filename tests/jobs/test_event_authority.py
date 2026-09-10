"""Publication authority job wrapper regressions."""

from __future__ import annotations

import logging

import pytest

from src.jobs.event_authority import (
    AuthorityCoordinationBusy,
    authority_block_reason,
    log_authority_complete,
)
from src.processing.event_authority import AuthorityBatchStats


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stats", "remaining", "expected"),
    [
        (AuthorityBatchStats(), 0, None),
        (AuthorityBatchStats(terminal=1), 4, "terminal"),
        (AuthorityBatchStats(provider_failures=1), 4, "provider_failure"),
        (AuthorityBatchStats(retry_wait=1), 4, "retry_wait"),
        (AuthorityBatchStats(busy=1), 4, "coordination_busy"),
        (AuthorityBatchStats(triaged=1), 4, "pending"),
    ],
)
def test_authority_block_reason_has_stable_precedence(stats, remaining, expected):
    assert authority_block_reason(stats, remaining) == expected


@pytest.mark.unit
def test_authority_completion_log_contains_semantic_counters(caplog):
    stats = AuthorityBatchStats(requested=2, claimed=2, triaged=1, busy=1)
    with caplog.at_level(logging.INFO, logger="src.jobs.event_authority"):
        log_authority_complete(
            mode="publication",
            intent_id=68,
            edition_id=1,
            stats=stats,
            remaining_gap=1,
            duration_ms=42,
        )
    record = caplog.records[-1]
    assert record.intent_id == 68
    assert record.remaining_gap == 1
    assert record.duration_ms == 42


@pytest.mark.unit
def test_contention_error_is_distinct_from_provider_failure():
    assert issubclass(AuthorityCoordinationBusy, RuntimeError)
