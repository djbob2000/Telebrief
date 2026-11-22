# ruff: noqa: S101
"""Retry accounting of one authority batch, with and without the singleton re-send."""

from __future__ import annotations

import datetime as dt

import pytest
from authority_fakes import (
    BACKOFF_SECONDS,
    KEY,
    Retries,
    build_service,
    invalid_batch,
    keep_batch,
    now,
    retry_state,
    scope_patch,
    target,
)


@pytest.fixture(autouse=True)
def _edition_scope():
    with scope_patch():
        yield


def _past_attempt() -> Retries:
    return Retries(
        {
            KEY: retry_state(
                attempt_count=1,
                exhausted_at=None,
                next_retry_at=now() - dt.timedelta(minutes=1),
            )
        }
    )


@pytest.mark.asyncio
async def test_flag_off_invalid_story_is_not_resent_in_the_same_cycle():
    retries = Retries()
    service, triage = build_service(
        singleton_enabled=False, responses=[invalid_batch()], retries=retries
    )

    before = now()
    result = await service.process_batch([target()], mode="background")
    after = now()

    assert triage.story_counts == [1]  # only the batch call, no single-story re-send
    assert result.stats.singleton_calls == 0
    assert len(retries.failures) == 1
    failure = retries.failures[0]
    assert failure["exhausted"] is False
    assert before + dt.timedelta(seconds=BACKOFF_SECONDS) <= failure["next_retry_at"]
    assert failure["next_retry_at"] <= after + dt.timedelta(seconds=BACKOFF_SECONDS)
    assert retries.cleared == []
    assert result.stats.unresolved == 1
    assert result.stats.newly_exhausted == 0
    # The earliest moment the story can be sent again is reported for scheduling.
    assert result.stats.next_retry_at == failure["next_retry_at"]


@pytest.mark.asyncio
async def test_flag_off_second_invalid_attempt_exhausts_the_story():
    retries = _past_attempt()
    service, triage = build_service(
        singleton_enabled=False, responses=[invalid_batch()], retries=retries
    )

    result = await service.process_batch([target()], mode="background")

    assert triage.story_counts == [1]
    assert len(retries.failures) == 1
    failure = retries.failures[0]
    assert failure["exhausted"] is True
    assert failure["next_retry_at"] is None
    assert result.stats.retry_calls == 1
    assert result.stats.newly_exhausted == 1
    assert result.stats.next_retry_at is None  # nothing left to schedule


@pytest.mark.asyncio
async def test_story_inside_retry_wait_is_not_sent_and_spends_no_attempt():
    wait_until = now() + dt.timedelta(hours=1)
    retries = Retries(
        {KEY: retry_state(attempt_count=1, exhausted_at=None, next_retry_at=wait_until)}
    )
    service, triage = build_service(singleton_enabled=False, responses=[], retries=retries)

    result = await service.process_batch([target()], mode="background")

    assert triage.story_counts == []
    assert result.stats.retry_wait == 1
    assert result.stats.next_retry_at == wait_until
    assert retries.failures == []  # no attempt consumed while waiting
    assert retries.cleared == []


@pytest.mark.asyncio
async def test_exhausted_story_is_terminal_and_never_sent():
    retries = Retries(
        {
            KEY: retry_state(
                attempt_count=2,
                exhausted_at=now() - dt.timedelta(minutes=1),
                next_retry_at=None,
            )
        }
    )
    service, triage = build_service(singleton_enabled=False, responses=[], retries=retries)

    result = await service.process_batch([target()], mode="background")

    assert triage.story_counts == []
    assert result.stats.terminal == 1
    assert retries.failures == []


@pytest.mark.asyncio
async def test_flag_on_invalid_story_is_resent_alone_in_the_same_cycle():
    retries = Retries()
    service, triage = build_service(
        singleton_enabled=True,
        responses=[invalid_batch(), keep_batch()],
        retries=retries,
    )

    result = await service.process_batch([target()], mode="background")

    assert triage.story_counts == [1, 1]  # batch, then the single-story re-send
    assert triage.max_output_tokens[1] is not None  # singleton call is output-capped
    assert result.stats.singleton_calls == 1
    assert result.stats.singleton_recovered == 1
    assert result.stats.single_story_batches == 1  # the batch itself held one story
    assert result.stats.recovered_keep == 0  # no earlier attempt, so not a retry recovery
    assert result.stats.triaged == 1
    assert retries.failures == []  # recovered in the cycle: no attempt spent
    assert len(retries.cleared) == 1
    assert result.stats.unresolved == 0
    assert result.stats.next_retry_at is None


@pytest.mark.asyncio
async def test_flag_on_keep_after_earlier_failure_is_counted_as_recovered_keep():
    retries = _past_attempt()
    service, _triage = build_service(
        singleton_enabled=True,
        responses=[invalid_batch(), keep_batch()],
        retries=retries,
    )

    result = await service.process_batch([target()], mode="background")

    assert result.stats.retry_calls == 1
    assert result.stats.singleton_recovered == 1
    assert result.stats.recovered_after_retry == 1
    assert result.stats.recovered_keep == 1
    assert result.stats.unresolved == 0


@pytest.mark.asyncio
async def test_flag_off_keep_on_retry_batch_is_counted_without_singleton():
    retries = _past_attempt()
    service, triage = build_service(
        singleton_enabled=False, responses=[keep_batch()], retries=retries
    )

    result = await service.process_batch([target()], mode="background")

    assert triage.story_counts == [1]
    assert result.stats.singleton_calls == 0
    assert result.stats.singleton_recovered == 0
    assert result.stats.recovered_after_retry == 1
    assert result.stats.recovered_keep == 1
