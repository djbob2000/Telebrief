import logging
import os
from unittest.mock import AsyncMock, patch

import pytest

from src.ai_providers import (
    OpenAIProvider,
    ProviderCascade,
    create_provider,
    get_allowed_ai_models,
    validate_model_allowed,
)


def test_get_allowed_ai_models_splits_comma_separated():
    env = {
        "OPENROUTER_MODEL": "minimax/minimax-m3:free, minimax/minimax-m2.7:free , thinkingmachines/inkling:free",
    }
    with patch.dict(os.environ, env, clear=True):
        allowed = get_allowed_ai_models()
        assert "minimax/minimax-m3:free" in allowed
        assert "minimax/minimax-m2.7:free" in allowed
        assert "thinkingmachines/inkling:free" in allowed
        assert len(allowed) == 3


def test_validate_model_allowed_with_fallback_chain():
    env = {
        "OPENROUTER_MODEL": "model-a,model-b:free",
    }
    with patch.dict(os.environ, env, clear=True):
        # Should not raise
        validate_model_allowed("model-a", force=True)
        validate_model_allowed("model-b:free", force=True)

        with pytest.raises(ValueError, match="is not allowed"):
            validate_model_allowed("unauthorized-model", force=True)


def test_create_provider_single_model():
    logger = logging.getLogger("test")
    env = {"OPENROUTER_MODEL": "single-model"}
    with patch.dict(os.environ, env, clear=True):
        provider = create_provider(
            "openrouter",
            logger,
            openrouter_api_key="sk-test",
            openrouter_models=["single-model"],
        )
        assert isinstance(provider, OpenAIProvider)


def test_create_provider_multiple_models_returns_cascade():
    logger = logging.getLogger("test")
    models = ["model-1", "model-2", "model-3"]
    env = {"OPENROUTER_MODEL": ",".join(models)}
    with patch.dict(os.environ, env, clear=True):
        provider = create_provider(
            "openrouter",
            logger,
            openrouter_api_key="sk-test",
            openrouter_models=models,
        )
        assert isinstance(provider, ProviderCascade)
        assert len(provider.providers) == 3
        assert provider.providers[0][0] == "openrouter-primary"
        assert provider.providers[0][2] == "model-1"
        assert provider.providers[1][0] == "openrouter-secondary"
        assert provider.providers[1][2] == "model-2"
        assert provider.providers[2][0] == "openrouter-fallback-3"
        assert provider.providers[2][2] == "model-3"


def test_create_provider_parses_comma_in_openrouter_model_when_models_not_passed():
    logger = logging.getLogger("test")
    raw = "model-x, model-y"
    env = {"OPENROUTER_MODEL": raw}
    with patch.dict(os.environ, env, clear=True):
        provider = create_provider(
            "openrouter",
            logger,
            openrouter_api_key="sk-test",
            openrouter_model=raw,
        )
        assert isinstance(provider, ProviderCascade)
        assert len(provider.providers) == 2
        assert provider.providers[0][0] == "openrouter-primary"
        assert provider.providers[0][2] == "model-x"
        assert provider.providers[1][0] == "openrouter-secondary"
        assert provider.providers[1][2] == "model-y"


@pytest.mark.asyncio
async def test_cascade_fails_over_to_fallback_on_error():
    logger = logging.getLogger("test")
    models = ["model-1", "model-2"]
    env = {"OPENROUTER_MODEL": ",".join(models)}
    with patch.dict(os.environ, env, clear=True):
        provider = create_provider(
            "openrouter",
            logger,
            openrouter_api_key="sk-test",
            openrouter_models=models,
        )
        assert isinstance(provider, ProviderCascade)

        # Mock chat_completion on both slots
        provider.providers[0][1].chat_completion = AsyncMock(
            side_effect=RuntimeError("429 Too Many Requests")
        )
        provider.providers[1][1].chat_completion = AsyncMock(return_value="Success from fallback 2")

        res = await provider.chat_completion([{"role": "user", "content": "hi"}], model="ignored")
        assert res == "Success from fallback 2"
        assert provider.providers[0][1].chat_completion.called
        assert provider.providers[1][1].chat_completion.called
