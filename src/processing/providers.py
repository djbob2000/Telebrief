"""Provider resolution for Event-First processing work."""

from __future__ import annotations

import logging
from typing import Any

from src.ai_providers import ProviderCascade, create_provider


def resolve_processing_provider(runtime: Any, config: Any, logger: logging.Logger) -> Any:
    """Resolve a provider without letting the editorial cascade replace processing routing.

    OpenRouter processing uses its dedicated model as a one-slot cascade override when
    configured. Without that override, provider slots come from the complete configured
    OpenRouter model list. Other providers retain the existing runtime injection behavior.
    """
    provider_name = str(config.settings.ai_provider).lower()
    processing_model = str(getattr(config, "openrouter_processing_model", "") or "").strip()
    if provider_name == "openrouter":
        if processing_model:
            provider = create_provider(
                provider_name,
                logger,
                openai_api_key=config.openai_api_key,
                anthropic_api_key=config.anthropic_api_key,
                google_api_key=config.gemini_api_key,
                openrouter_api_key=config.openrouter_api_key,
                openrouter_model=processing_model,
                openrouter_model_2="",
                openrouter_models=[processing_model],
            )
            # A single OpenAIProvider accepts the caller's model parameter directly. Keep
            # the configured processing model in a cascade slot so it overrides that input.
            return ProviderCascade([("openrouter-processing", provider, processing_model)], logger)

        processing_provider = getattr(runtime, "processing_provider_cascade", None)
        if processing_provider is not None:
            return processing_provider

        configured_models = getattr(config, "openrouter_models", None)
        configured_model_2 = getattr(config, "openrouter_model_2", "")
        provider = create_provider(
            provider_name,
            logger,
            openai_api_key=config.openai_api_key,
            anthropic_api_key=config.anthropic_api_key,
            google_api_key=config.gemini_api_key,
            openrouter_api_key=config.openrouter_api_key,
            openrouter_model=config.openrouter_model,
            openrouter_model_2=configured_model_2,
            openrouter_models=configured_models,
        )
        if isinstance(provider, ProviderCascade):
            return provider

        model_slots = _configured_openrouter_models(
            config.openrouter_model, configured_model_2, configured_models
        )
        return ProviderCascade([("openrouter-primary", provider, model_slots[0])], logger)

    injected_provider = getattr(runtime, "processing_provider_cascade", None) or getattr(
        runtime, "provider_cascade", None
    )
    if injected_provider is not None:
        return injected_provider

    return create_provider(
        provider_name,
        logger,
        openai_api_key=config.openai_api_key,
        anthropic_api_key=config.anthropic_api_key,
        google_api_key=config.gemini_api_key,
        openrouter_api_key=config.openrouter_api_key,
        openrouter_model=config.openrouter_model,
        openrouter_model_2=getattr(config, "openrouter_model_2", ""),
        openrouter_models=getattr(config, "openrouter_models", None),
    )


def _configured_openrouter_models(
    openrouter_model: str,
    openrouter_model_2: str,
    openrouter_models: list[str] | tuple[str, ...] | None,
) -> list[str]:
    """Mirror create_provider's model-slot normalization for a plain provider."""
    if openrouter_models is not None:
        models = [model.strip() for model in openrouter_models if model and model.strip()]
    else:
        models = []
        for part in (openrouter_model or "").split(","):
            model = part.strip()
            if model and model not in models:
                models.append(model)
        model_2 = (openrouter_model_2 or "").strip()
        if model_2 and model_2 not in models:
            models.append(model_2)
    return models or ["openrouter/free"]
