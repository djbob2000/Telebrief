# ruff: noqa: S101
"""A failed scheduled digest slot is retried a bounded number of times before its deadline."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

import src.jobs.publication as publication_jobs
from src.jobs.publication import (
    DIGEST_RETRY_MAX_ATTEMPTS,
    digest_retry_attempt,
    digest_retry_base_key,
    digest_retry_delay_seconds,
)

UTC = dt.timezone.utc
DEADLINE = dt.datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def test_attempt_number_comes_from_the_retry_prefix():
    assert digest_retry_attempt("scheduled:berdyansk:digest_grouped:x") == 1
    assert digest_retry_attempt("retry-2:scheduled:berdyansk:digest_grouped:x") == 2
    assert digest_retry_base_key("retry-2:scheduled:berdyansk:digest_grouped:x") == (
        "scheduled:berdyansk:digest_grouped:x"
    )
    assert digest_retry_base_key("scheduled:berdyansk:digest_grouped:x") == (
        "scheduled:berdyansk:digest_grouped:x"
    )


def test_retry_fits_well_before_the_deadline():
    now = dt.datetime(2026, 10, 20, 6, 5, tzinfo=UTC)
    assert digest_retry_delay_seconds(attempt=1, now=now, deadline_at=DEADLINE) == 1200.0


def test_retries_stop_after_the_last_attempt():
    now = dt.datetime(2026, 10, 20, 6, 5, tzinfo=UTC)
    assert (
        digest_retry_delay_seconds(attempt=DIGEST_RETRY_MAX_ATTEMPTS, now=now, deadline_at=DEADLINE)
        is None
    )


def test_no_retry_when_the_attempt_would_start_too_close_to_the_deadline():
    # 07:40 + 20 min = 08:00 is past the last start (07:50) for a deadline at 08:00.
    now = dt.datetime(2026, 10, 20, 7, 40, tzinfo=UTC)
    assert digest_retry_delay_seconds(attempt=1, now=now, deadline_at=DEADLINE) is None


class _FakeDeferrer:
    def __init__(self, calls: list[dict]) -> None:
        self.calls = calls

    def configure(self, **options):
        self.options = options
        return self

    async def defer_async(self, **kwargs):
        self.calls.append({"options": self.options, **kwargs})


def _run(**overrides):
    metadata = {
        "trigger": "scheduled",
        "scheduled_slot_at": overrides.pop("slot", None),
        "intent_request_key": "scheduled:berdyansk:digest_grouped:x",
        "refresh_run_id": 1,
    }
    base = {
        "edition_id": 1,
        "publication_type": "digest_grouped",
        "metadata": metadata,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def deferred(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(publication_jobs, "retry_digest_publication", _FakeDeferrer(calls))
    return calls


@pytest.mark.asyncio
async def test_failed_scheduled_digest_with_time_left_is_retried_once(deferred):
    slot = (dt.datetime.now(UTC) + dt.timedelta(minutes=30)).isoformat()
    scheduled = await publication_jobs._schedule_digest_retry(_run(slot=slot))
    assert scheduled is True
    assert len(deferred) == 1
    call = deferred[0]
    assert isinstance(call["options"]["schedule_at"], dt.datetime)
    assert call["attempt"] == 2
    assert call["edition_id"] == 1
    assert call["base_request_key"] == "scheduled:berdyansk:digest_grouped:x"


@pytest.mark.asyncio
async def test_no_retry_for_manual_or_article_runs(deferred):
    slot = (dt.datetime.now(UTC) + dt.timedelta(minutes=30)).isoformat()
    manual = _run(slot=slot)
    manual.metadata["trigger"] = "manual"
    article = _run(slot=slot, publication_type="daily_article")
    assert await publication_jobs._schedule_digest_retry(manual) is False
    assert await publication_jobs._schedule_digest_retry(article) is False
    assert deferred == []


@pytest.mark.asyncio
async def test_no_retry_once_the_slot_deadline_has_passed(deferred):
    past = (dt.datetime.now(UTC) - dt.timedelta(hours=3)).isoformat()
    assert await publication_jobs._schedule_digest_retry(_run(slot=past)) is False
    assert deferred == []
