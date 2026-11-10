# ruff: noqa: S101
"""The chronicle runs on the processing model, whatever model its caller passes."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from src.ai_providers import ProviderCascade
from src.processing.providers import resolve_processing_provider

_PROCESSING_MODEL = "deepseek/deepseek-v4.1-flash-1017:floor"
_EDITORIAL_DEFAULT = "gpt-5-nano"


class _RecordingProvider:
    def __init__(self) -> None:
        self.models: list[str] = []

    async def chat_completion(self, messages: Any, model: str, **_kwargs: Any) -> str:
        self.models.append(model)
        return "{}"


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(ai_provider="openrouter"),
        openrouter_processing_model=_PROCESSING_MODEL,
        openai_api_key="",
        anthropic_api_key="",
        gemini_api_key="",
        openrouter_api_key="test-key",
        openrouter_model="openai/gpt-6-luna",
        openrouter_model_2="",
        openrouter_models=None,
    )


def test_processing_provider_is_a_cascade_pinned_to_the_processing_model() -> None:
    provider = resolve_processing_provider(None, _config(), logging.getLogger("test"))

    assert isinstance(provider, ProviderCascade)
    assert [slot[2] for slot in provider.providers] == [_PROCESSING_MODEL]


def test_editorial_default_model_is_overridden_by_the_processing_slot() -> None:
    recorder = _RecordingProvider()
    cascade = ProviderCascade([("processing", recorder, _PROCESSING_MODEL)], logging.getLogger("t"))

    asyncio.run(
        cascade.chat_completion(
            messages=[{"role": "user", "content": "x"}],
            model=_EDITORIAL_DEFAULT,
        )
    )

    assert recorder.models == [_PROCESSING_MODEL]
