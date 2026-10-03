# ruff: noqa: S101
"""Routing behavior for Event-First processing providers."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import patch

from src.ai_providers import ProviderCascade
from src.processing.event_authority import EventAuthorityService
from src.processing.providers import resolve_processing_provider


class RecordingProvider:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.models: list[str] = []

    async def chat_completion(self, *, model: str, **kwargs: object) -> str:
        self.models.append(model)
        if self.fail:
            raise RuntimeError("provider unavailable")
        return f"response from {model}"


def _config(provider: str = "openrouter", *, processing_model: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(ai_provider=provider, ai_model="editorial/request-model"),
        openai_api_key="openai-key",
        anthropic_api_key="anthropic-key",
        gemini_api_key="google-key",
        openrouter_api_key="router-key",
        openrouter_model="editorial/primary",
        openrouter_model_2="editorial/backup",
        openrouter_models=["editorial/primary", "editorial/backup"],
        openrouter_processing_model=processing_model,
    )


def test_processing_override_is_the_model_sent_even_with_editorial_runtime_cascade() -> None:
    editorial_provider = RecordingProvider()
    processing_transport = RecordingProvider()
    runtime = SimpleNamespace(provider_cascade=editorial_provider)
    config = _config(processing_model="processing/dedicated")
    ProviderCascade.reset_global_state()

    with patch("src.ai_providers.OpenAIProvider", return_value=processing_transport):
        provider = resolve_processing_provider(runtime, config, logging.getLogger("test"))
        response = asyncio.run(
            provider.chat_completion(
                messages=[{"role": "user", "content": "test"}],
                model=config.settings.ai_model,
            )
        )

    assert response == "response from processing/dedicated"
    assert processing_transport.models == ["processing/dedicated"]
    assert editorial_provider.models == []


def test_openrouter_without_processing_override_keeps_configured_fallback_slots() -> None:
    editorial_provider = RecordingProvider()
    primary = RecordingProvider(fail=True)
    fallback = RecordingProvider()
    runtime = SimpleNamespace(provider_cascade=editorial_provider)
    config = _config()
    ProviderCascade.reset_global_state()

    with patch("src.ai_providers.OpenAIProvider", side_effect=[primary, fallback]):
        provider = resolve_processing_provider(runtime, config, logging.getLogger("test"))
        response = asyncio.run(
            provider.chat_completion(
                messages=[{"role": "user", "content": "test"}],
                model=config.settings.ai_model,
            )
        )

    assert response == "response from editorial/backup"
    assert primary.models == ["editorial/primary"]
    assert fallback.models == ["editorial/backup"]
    assert editorial_provider.models == []


def test_non_openrouter_processing_keeps_an_injected_provider() -> None:
    injected_provider = RecordingProvider()
    runtime = SimpleNamespace(provider_cascade=injected_provider)
    config = _config("google", processing_model="processing/dedicated")

    provider = resolve_processing_provider(runtime, config, logging.getLogger("test"))

    assert provider is injected_provider


def test_openrouter_single_model_uses_configured_model_when_request_model_is_raw() -> None:
    transport = RecordingProvider()
    runtime = SimpleNamespace()
    config = _config()
    config.settings.ai_model = "configured/only,configured/second"
    config.openrouter_model = "configured/only"
    config.openrouter_model_2 = ""
    config.openrouter_models = ["configured/only"]
    ProviderCascade.reset_global_state()

    with patch("src.ai_providers.OpenAIProvider", return_value=transport):
        provider = resolve_processing_provider(runtime, config, logging.getLogger("test"))
        response = asyncio.run(
            provider.chat_completion(
                messages=[{"role": "user", "content": "test"}],
                model=config.settings.ai_model,
            )
        )

    assert response == "response from configured/only"
    assert transport.models == ["configured/only"]


def test_explicit_processing_provider_injection_has_its_own_scope() -> None:
    editorial_provider = RecordingProvider()
    processing_provider = RecordingProvider()
    runtime = SimpleNamespace(
        provider_cascade=editorial_provider,
        processing_provider_cascade=processing_provider,
    )
    config = _config()

    provider = resolve_processing_provider(runtime, config, logging.getLogger("test"))

    assert provider is processing_provider


def test_non_openrouter_without_injection_builds_the_configured_provider() -> None:
    runtime = SimpleNamespace()
    config = _config("google")
    constructed_provider = RecordingProvider()
    logger = logging.getLogger("test")

    with patch(
        "src.processing.providers.create_provider", return_value=constructed_provider
    ) as create:
        provider = resolve_processing_provider(runtime, config, logger)

    assert provider is constructed_provider
    create.assert_called_once_with(
        "google",
        logger,
        openai_api_key="openai-key",
        anthropic_api_key="anthropic-key",
        google_api_key="google-key",
        openrouter_api_key="router-key",
        openrouter_model="editorial/primary",
        openrouter_model_2="editorial/backup",
        openrouter_models=["editorial/primary", "editorial/backup"],
    )


def test_event_authority_from_runtime_uses_the_shared_processing_resolver() -> None:
    processing_provider = RecordingProvider()
    runtime = SimpleNamespace(uow=object())
    config = _config("google")
    config.settings.event_pipeline = SimpleNamespace(
        triage_max_output_tokens=100,
        triage_reasoning_effort="low",
    )
    cluster_repository = object()

    with (
        patch(
            "src.processing.event_authority.resolve_processing_provider",
            return_value=processing_provider,
        ) as resolver,
        patch(
            "src.processing.event_authority.EventClusterRepository",
            return_value=cluster_repository,
        ),
        patch("src.processing.event_authority.EventAuthorityRepository"),
        patch("src.processing.event_authority.EventProcessingRetryRepository"),
        patch("src.processing.event_authority.EventProcessingClaimRepository"),
    ):
        service = EventAuthorityService.from_runtime(runtime, config)

    resolver.assert_called_once_with(
        runtime, config, logging.getLogger("src.processing.event_authority")
    )
    assert service.triage_service.ai is processing_provider
