# ruff: noqa: S101
"""Per-call LLM usage: cost extraction, stage tagging and buffered persistence."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

import src.llm_usage as llm_usage
from src.ai_providers import OpenAIProvider
from src.llm_telemetry import llm_call_context
from src.llm_usage import (
    UNTAGGED_STAGE,
    LLMUsageRecord,
    LLMUsageRecorder,
    close_llm_usage_recorder,
    install_llm_usage_recorder,
    usage_record_from_response,
)


def _openrouter_response(**usage_extra):
    usage = SimpleNamespace(
        prompt_tokens=12000,
        completion_tokens=1500,
        total_tokens=13500,
        prompt_tokens_details=SimpleNamespace(cached_tokens=7000),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=300),
        cost=0.00321,
        **usage_extra,
    )
    message = SimpleNamespace(content='{"results": []}')
    return SimpleNamespace(
        id="gen-1",
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=usage,
    )


def _record(stage: str = "event_triage") -> LLMUsageRecord:
    return LLMUsageRecord(
        created_at=dt.datetime(2026, 11, 19, tzinfo=dt.UTC),
        stage=stage,
        provider="OpenRouter",
        model="deepseek/deepseek-v4.1-flash-1017:floor",
        origin="worker.py",
        response_id="gen-1",
        prompt_tokens=100,
        cached_tokens=40,
        completion_tokens=10,
        reasoning_tokens=0,
        cost_usd=0.0001,
        story_count=None,
        finish_reason="stop",
        elapsed_ms=50,
    )


def test_record_reads_openrouter_cost_cached_tokens_and_stage():
    with llm_call_context(stage="event_triage", story_count=10):
        record = usage_record_from_response(
            _openrouter_response(),
            provider="OpenRouter",
            model="m",
            elapsed_seconds=1.25,
            finish_reason="stop",
        )

    assert record.stage == "event_triage"
    assert record.story_count == 10
    assert record.prompt_tokens == 12000
    assert record.cached_tokens == 7000
    assert record.completion_tokens == 1500
    assert record.reasoning_tokens == 300
    assert record.cost_usd == pytest.approx(0.00321)
    assert record.response_id == "gen-1"
    assert record.elapsed_ms == 1250


def test_record_without_context_or_usage_is_untagged_and_unpriced():
    response = SimpleNamespace(id=None, choices=[], usage=None)

    record = usage_record_from_response(
        response, provider="OpenAI", model="m", elapsed_seconds=None, finish_reason=None
    )

    assert record.stage == UNTAGGED_STAGE
    assert record.cost_usd is None
    assert record.prompt_tokens is None
    assert record.cached_tokens is None


def test_record_reads_cost_from_pydantic_extra_fields():
    usage = SimpleNamespace(
        prompt_tokens=5,
        completion_tokens=1,
        model_extra={"cost": 0.5, "prompt_tokens_details": {"cached_tokens": 2}},
    )
    response = SimpleNamespace(id="x", usage=usage)

    record = usage_record_from_response(
        response, provider="OpenRouter", model="m", elapsed_seconds=0.0, finish_reason="stop"
    )

    assert record.cost_usd == 0.5
    assert record.cached_tokens == 2


@pytest.mark.asyncio
async def test_recorder_writes_records_of_one_window_in_one_batch():
    batches: list[list[LLMUsageRecord]] = []

    async def writer(records):
        batches.append(list(records))

    recorder = LLMUsageRecorder(writer, flush_delay=0.01)
    recorder.record(_record("a"))
    recorder.record(_record("b"))
    await asyncio.sleep(0.05)

    assert [[r.stage for r in batch] for batch in batches] == [["a", "b"]]


@pytest.mark.asyncio
async def test_recorder_close_writes_pending_records_without_waiting():
    batches: list[list[LLMUsageRecord]] = []

    async def writer(records):
        batches.append(list(records))

    recorder = LLMUsageRecorder(writer, flush_delay=3600)
    recorder.record(_record())
    await recorder.close()

    assert len(batches) == 1


@pytest.mark.asyncio
async def test_recorder_write_failure_is_logged_not_raised(caplog):
    async def writer(records):
        raise RuntimeError("db down")

    recorder = LLMUsageRecorder(writer, flush_delay=3600)
    recorder.record(_record())
    with caplog.at_level(logging.WARNING, logger="src.llm_usage"):
        await recorder.close()

    assert recorder.dropped == 1
    assert "could not write 1 LLM usage records" in caplog.text


class _Completions:
    async def create(self, **kwargs):
        return _openrouter_response()


@pytest.mark.asyncio
async def test_provider_call_records_tagged_usage():
    batches: list[list[LLMUsageRecord]] = []

    async def writer(records):
        batches.append(list(records))

    recorder = LLMUsageRecorder(writer, flush_delay=3600)
    install_llm_usage_recorder(recorder)
    try:
        provider = OpenAIProvider(
            api_key="test-key",
            logger=logging.getLogger("test_llm_usage"),
            base_url="https://openrouter.ai/api/v1",
        )
        provider.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        with llm_call_context(stage="chronicle_week"):
            await provider.chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                model="deepseek/deepseek-v4.1-flash-1017:floor",
            )
    finally:
        await close_llm_usage_recorder(recorder)

    assert llm_usage._recorder is None
    [batch] = batches
    [record] = batch
    assert record.stage == "chronicle_week"
    assert record.cost_usd == pytest.approx(0.00321)
    assert provider.last_metadata["cost_usd"] == pytest.approx(0.00321)
    assert provider.last_metadata["cached_tokens"] == 7000


@pytest.mark.asyncio
async def test_provider_call_succeeds_when_recording_fails(monkeypatch):
    def broken(record):
        raise RuntimeError("boom")

    monkeypatch.setattr("src.ai_providers.record_llm_usage", broken)
    provider = OpenAIProvider(
        api_key="test-key",
        logger=logging.getLogger("test_llm_usage"),
        base_url="https://openrouter.ai/api/v1",
    )
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    result = await provider.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="deepseek/deepseek-v4.1-flash-1017:floor",
    )

    assert result == '{"results": []}'


def test_cost_report_formats_shares_and_unpriced_calls():
    from scripts.llm_cost_report import format_daily_stages, format_summary, usage_row

    rows = [
        usage_row(("event_triage", 300, 4_000_000, 2_000_000, 400_000, 0, Decimal("3.0"), 0)),
        usage_row(("untagged", 10, 50_000, 0, 5_000, 0, Decimal("1.0"), 2)),
    ]

    summary = format_summary(rows, "stage")

    assert "event_triage" in summary
    assert "75.0%" in summary  # 3.0 of 4.0
    assert "50.0%" in summary  # cached share of triage prompts
    assert "total: 310 calls, $4.0000" in summary
    assert "2 calls carry no reported cost" in summary

    daily = format_daily_stages(
        [("2026-11-18", "event_triage", Decimal("1.5")), ("2026-11-19", "untagged", 1)]
    )
    assert "2026-11-18" in daily and "1.5000" in daily
