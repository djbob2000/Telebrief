# ruff: noqa: S101
"""Per-call LLM usage: cost extraction, stage tagging and buffered persistence."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

import src.llm_usage as llm_usage
from src import image_generator
from src.ai_providers import GoogleProvider, OpenAIProvider
from src.embedding_providers import OpenRouterEmbeddingProvider
from src.image_generator import NewsImageGenerator
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


NON_CHAT_STAGES = ("image_generation", "embedding", "rubric_embedding", "claim_embedding")


def test_cost_report_shows_every_non_chat_stage():
    from scripts.llm_cost_report import format_daily_stages, format_summary, usage_row

    rows = [usage_row((stage, 3, 100, 0, 0, 0, Decimal("0.5"), 0)) for stage in NON_CHAT_STAGES]

    summary = format_summary(rows, "stage")
    daily = format_daily_stages(
        [("2026-11-19", stage, Decimal("0.5")) for stage in NON_CHAT_STAGES]
    )

    for stage in NON_CHAT_STAGES:
        assert stage in summary
        assert stage in daily


# --- Non-chat paid calls: embeddings, cover images and native Google responses ---

_REQUEST = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
EMBEDDING_MODEL = "qwen/qwen3-embedding-8b:floor"
COVER_JPEG = b"\xff\xd8\xff\xe0cover-jpeg"
COVER_B64 = base64.b64encode(COVER_JPEG).decode()
IMAGE_ONLY_404 = (
    '{"error":{"message":"meta/muse-image is an image generation model and cannot be used '
    'with the chat/completions endpoint. Use the /api/v1/images endpoint instead.","code":404}}'
)


async def _recorded(action, *, writer=None):
    """Run ``action`` with a recorder installed; return its result and the rows written."""
    written: list[LLMUsageRecord] = []

    async def default_writer(records):
        written.extend(records)

    recorder = LLMUsageRecorder(writer or default_writer, flush_delay=3600)
    install_llm_usage_recorder(recorder)
    try:
        result = await action()
    finally:
        await close_llm_usage_recorder(recorder)
    return result, written


async def _broken_writer(records):
    raise RuntimeError("db down")


class _Embeddings:
    def __init__(self, response):
        self._response = response

    async def create(self, **kwargs):
        return self._response


def _embedding_provider() -> OpenRouterEmbeddingProvider:
    usage = SimpleNamespace(prompt_tokens=40, total_tokens=40, model_extra={"cost": 0.0002})
    data = [SimpleNamespace(index=i, embedding=[0.1, 0.2]) for i in range(2)]
    provider = OpenRouterEmbeddingProvider(
        api_key="test-key", logger=logging.getLogger("test_llm_usage")
    )
    provider.client = SimpleNamespace(
        embeddings=_Embeddings(SimpleNamespace(data=data, usage=usage))
    )
    return provider


@pytest.mark.asyncio
async def test_embedding_batch_is_recorded_under_embedding_stage():
    provider = _embedding_provider()

    async def action():
        with llm_call_context(stage="embedding"):
            return await provider.embed_many(
                ["first fragment", "second fragment"],
                purpose="story_document",
                model=EMBEDDING_MODEL,
                dimensions=2,
            )

    vectors, written = await _recorded(action)

    assert vectors == [[0.1, 0.2], [0.1, 0.2]]
    [record] = written
    assert record.stage == "embedding"
    assert record.provider == "OpenRouter"
    assert record.model == EMBEDDING_MODEL
    assert record.prompt_tokens == 40
    assert record.completion_tokens is None
    assert record.cost_usd == pytest.approx(0.0002)
    assert record.elapsed_ms is not None


@pytest.mark.asyncio
async def test_embedding_batch_succeeds_when_usage_write_fails():
    provider = _embedding_provider()

    async def action():
        with llm_call_context(stage="embedding"):
            return await provider.embed_many(
                ["first fragment", "second fragment"],
                purpose="story_document",
                model=EMBEDDING_MODEL,
                dimensions=2,
            )

    vectors, written = await _recorded(action, writer=_broken_writer)

    assert vectors == [[0.1, 0.2], [0.1, 0.2]]
    assert written == []


def _http_routes(monkeypatch, routes: dict[str, httpx.Response]) -> list[str]:
    """Answer every httpx post whose URL contains a route key with that canned response."""
    calls: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        async def post(self, url, json, headers=None):
            calls.append(url)
            return routes[next(key for key in routes if key in url)]

    monkeypatch.setattr(image_generator.httpx, "AsyncClient", FakeClient)
    return calls


def _json_reply(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status, json=body, request=_REQUEST)


def _cover_generator(*, openrouter_key: str) -> NewsImageGenerator:
    config = SimpleNamespace(
        openrouter_api_key=openrouter_key,
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_model="",
        openrouter_image_model="meta/muse-image",
        google_api_keys=[] if openrouter_key else ["g1"],
        settings=SimpleNamespace(ai_model="m"),
    )
    return NewsImageGenerator(config, logging.getLogger("test"))  # type: ignore[arg-type]


def _chat_cover_reply() -> httpx.Response:
    body = {
        "id": "gen-cover-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "images": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{COVER_B64}"},
                        }
                    ],
                }
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 1290,
            "total_tokens": 1310,
            "cost": 0.0412,
        },
    }
    return _json_reply(200, body)


@pytest.mark.asyncio
async def test_cover_image_chat_call_is_recorded_with_cost(monkeypatch, tmp_path):
    _http_routes(monkeypatch, {"chat/completions": _chat_cover_reply()})
    generator = _cover_generator(openrouter_key="test-key")

    path, written = await _recorded(
        lambda: generator.generate_image("a street", output_dir=tmp_path)
    )

    assert path is not None and path.read_bytes() == COVER_JPEG
    [record] = written
    assert record.stage == "image_generation"
    assert record.provider == "OpenRouter"
    assert record.model == "meta/muse-image"
    assert record.response_id == "gen-cover-1"
    assert record.completion_tokens == 1290
    assert record.cost_usd == pytest.approx(0.0412)


@pytest.mark.asyncio
async def test_image_only_model_records_only_the_images_api_call(monkeypatch, tmp_path):
    images_body = {
        "created": 1700000000,
        "data": [{"b64_json": COVER_B64}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 0, "total_tokens": 20, "cost": 0.04},
    }
    calls = _http_routes(
        monkeypatch,
        {
            "chat/completions": httpx.Response(404, text=IMAGE_ONLY_404, request=_REQUEST),
            "/images": _json_reply(200, images_body),
        },
    )
    generator = _cover_generator(openrouter_key="test-key")

    path, written = await _recorded(
        lambda: generator.generate_image("a street", output_dir=tmp_path)
    )

    assert path is not None and path.read_bytes() == COVER_JPEG
    assert [url.rsplit("/", 1)[1] for url in calls] == ["completions", "images"]
    [record] = written  # the rejected chat attempt is not billed, so it is not a row
    assert record.stage == "image_generation"
    assert record.model == "meta/muse-image"
    assert record.cost_usd == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_cover_image_succeeds_when_usage_write_fails(monkeypatch, tmp_path):
    _http_routes(monkeypatch, {"chat/completions": _chat_cover_reply()})
    generator = _cover_generator(openrouter_key="test-key")

    path, written = await _recorded(
        lambda: generator.generate_image("a street", output_dir=tmp_path),
        writer=_broken_writer,
    )

    assert path is not None and path.read_bytes() == COVER_JPEG
    assert written == []


@pytest.mark.asyncio
async def test_native_gemini_cover_is_recorded_with_tokens_and_no_cost(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_IMAGE_MODEL", "google/gemini-3.1-flash-lite-image:floor")
    body = {
        "candidates": [{"content": {"parts": [{"inlineData": {"data": COVER_B64}}]}}],
        "usageMetadata": {
            "promptTokenCount": 15,
            "candidatesTokenCount": 1290,
            "totalTokenCount": 1305,
        },
    }
    _http_routes(monkeypatch, {"generateContent": _json_reply(200, body)})
    generator = _cover_generator(openrouter_key="")

    path, written = await _recorded(
        lambda: generator.generate_image("a street", output_dir=tmp_path)
    )

    assert path is not None and path.read_bytes() == COVER_JPEG
    [record] = written
    assert record.stage == "image_generation"
    assert record.provider == "Google Gemini"
    assert record.model == "google/gemini-3.1-flash-lite-image:floor"
    assert record.prompt_tokens == 15
    assert record.completion_tokens == 1290
    assert record.cost_usd is None


class _GoogleCompletions:
    async def create(self, **kwargs):
        usage = SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10)
        return SimpleNamespace(
            id="g-1",
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=usage,
        )


@pytest.mark.asyncio
async def test_google_chat_call_is_recorded_without_reported_cost(monkeypatch):
    monkeypatch.setattr("src.ai_providers.validate_model_allowed", lambda model: None)
    provider = GoogleProvider(api_key="test-key", logger=logging.getLogger("test_llm_usage"))
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=_GoogleCompletions()))

    async def action():
        with llm_call_context(stage="image_prompt"):
            return await provider.chat_completion(
                messages=[{"role": "user", "content": "hi"}],
                model="gemini-3.7-flash",
            )

    text, written = await _recorded(action)

    assert text == "ok"
    [record] = written
    assert record.stage == "image_prompt"
    assert record.provider == "Google Gemini"
    assert record.model == "gemini-3.7-flash"
    assert record.prompt_tokens == 7
    assert record.cost_usd is None
