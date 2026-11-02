# ruff: noqa: S101
"""OpenRouter Anthropic models must not receive a structured-output response_format."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.ai_providers import OpenAIProvider

OPENROUTER_URL = "https://openrouter.ai/api/v1"
SCHEMA = {"type": "json_schema", "json_schema": {"name": "gate", "schema": {"type": "object"}}}


class _RecordingCompletions:
    def __init__(self) -> None:
        self.kwargs: list[dict] = []

    async def create(self, **kwargs):
        self.kwargs.append(kwargs)
        message = SimpleNamespace(content='{"results": []}')
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=None,
        )


def _provider() -> tuple[OpenAIProvider, _RecordingCompletions]:
    provider = OpenAIProvider(
        api_key="test-key",
        logger=logging.getLogger("test_response_format"),
        base_url=OPENROUTER_URL,
    )
    completions = _RecordingCompletions()
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return provider, completions


@pytest.mark.asyncio
async def test_anthropic_model_on_openrouter_drops_response_format():
    provider, completions = _provider()

    await provider.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="anthropic/claude-haiku-5.5",
        response_format=SCHEMA,
    )

    assert "response_format" not in completions.kwargs[0]


@pytest.mark.asyncio
async def test_other_openrouter_model_keeps_response_format():
    provider, completions = _provider()

    await provider.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="deepseek/deepseek-v4.1-flash-1017:floor",
        response_format=SCHEMA,
    )

    assert completions.kwargs[0]["response_format"] == SCHEMA


@pytest.mark.asyncio
async def test_floor_model_gets_fp8_quantization_filter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_FLOOR_QUANTIZATIONS", "fp8")
    provider, completions = _provider()

    await provider.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="deepseek/deepseek-v4.1-flash-1017:floor",
    )

    assert completions.kwargs[0]["extra_body"]["provider"] == {"quantizations": ["fp8"]}


@pytest.mark.asyncio
async def test_non_floor_model_gets_no_quantization_filter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_FLOOR_QUANTIZATIONS", "fp8")
    provider, completions = _provider()

    await provider.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/gpt-6-luna",
    )

    assert "provider" not in completions.kwargs[0].get("extra_body", {})


@pytest.mark.asyncio
async def test_floor_model_without_setting_gets_no_filter(monkeypatch):
    monkeypatch.delenv("OPENROUTER_FLOOR_QUANTIZATIONS", raising=False)
    provider, completions = _provider()

    await provider.chat_completion(
        messages=[{"role": "user", "content": "hi"}],
        model="deepseek/deepseek-v4.1-flash-1017:floor",
    )

    assert "provider" not in completions.kwargs[0].get("extra_body", {})
