"""Unit tests for embedding providers (Google Gemini, OpenRouter, OpenAI)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.config_loader import EmbeddingConfig
from src.embedding_providers import (
    EmbeddingDimensionMismatch,
    GoogleGeminiEmbeddingProvider,
    OpenAIEmbeddingProvider,
    OpenRouterEmbeddingProvider,
    create_embedding_provider,
)

logger = logging.getLogger(__name__)


class _StubCompatEmbeddingsApi:
    """Duck-typed stand-in for AsyncOpenAI(...).embeddings."""

    def __init__(self, embedding: list[float]):
        self._embedding = embedding
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(embedding=self._embedding)])


class _StubEmptyResponseApi:
    async def create(self, **kwargs):
        del kwargs
        return SimpleNamespace(data=[])


@pytest.mark.unit
class TestGoogleGeminiEmbeddingProvider:
    async def test_embed_success(self):
        provider = GoogleGeminiEmbeddingProvider(api_key="gem-key", logger=logger, timeout=45)
        api = _StubCompatEmbeddingsApi([0.5] * 1536)
        provider.client = SimpleNamespace(embeddings=api)

        vector = await provider.embed(
            "тестовый текст",
            purpose="claim_query",
            model="gemini-embedding-2",
            dimensions=1536,
        )
        assert vector == [0.5] * 1536
        assert api.calls == [
            {"model": "gemini-embedding-2", "input": "тестовый текст", "dimensions": 1536}
        ]

    async def test_rejects_missing_key(self):
        with pytest.raises(ValueError, match="requires a Gemini API key"):
            GoogleGeminiEmbeddingProvider(api_key="", logger=logger)

    async def test_rejects_dimension_mismatch(self):
        provider = GoogleGeminiEmbeddingProvider(api_key="gem-key", logger=logger, timeout=45)
        provider.client = SimpleNamespace(embeddings=_StubCompatEmbeddingsApi([0.5] * 10))

        with pytest.raises(EmbeddingDimensionMismatch):
            await provider.embed(
                "текст", purpose="claim_query", model="gemini-embedding-2", dimensions=1536
            )


@pytest.mark.unit
class TestOpenRouterEmbeddingProvider:
    async def test_embed_sends_model_input_dimensions(self):
        provider = OpenRouterEmbeddingProvider(api_key="or-key", logger=logger, timeout=45)
        api = _StubCompatEmbeddingsApi([0.1] * 1536)
        provider.client = SimpleNamespace(embeddings=api)

        vector = await provider.embed(
            "тестовый текст",
            purpose="claim_query",
            model="qwen/qwen3-embedding-8b",
            dimensions=1536,
        )
        assert vector == [0.1] * 1536
        assert api.calls == [{"model": "qwen/qwen3-embedding-8b", "input": "тестовый текст"}]

    async def test_rejects_missing_key(self):
        with pytest.raises(
            ValueError, match="OpenRouterEmbeddingProvider requires an OpenRouter API key"
        ):
            OpenRouterEmbeddingProvider(api_key="", logger=logger)


@pytest.mark.unit
class TestOpenAIEmbeddingProvider:
    async def test_embed_sends_model_input_dimensions(self):
        provider = OpenAIEmbeddingProvider(api_key="oa-key", logger=logger, timeout=45)
        api = _StubCompatEmbeddingsApi([0.2] * 1536)
        provider.client = SimpleNamespace(embeddings=api)

        vector = await provider.embed(
            "тестовый текст",
            purpose="claim_query",
            model="text-embedding-3-small",
            dimensions=1536,
        )
        assert vector == [0.2] * 1536
        assert api.calls == [
            {"model": "text-embedding-3-small", "input": "тестовый текст", "dimensions": 1536}
        ]

    async def test_rejects_missing_key(self):
        with pytest.raises(ValueError, match="OpenAIEmbeddingProvider requires an OpenAI API key"):
            OpenAIEmbeddingProvider(api_key="", logger=logger)


@pytest.mark.unit
class TestCreateEmbeddingProvider:
    def test_creates_google_gemini(self):
        cfg = EmbeddingConfig(provider="google", api_key="gem-key")
        provider = create_embedding_provider(cfg, logger=logger)
        assert isinstance(provider, GoogleGeminiEmbeddingProvider)

    def test_creates_openrouter(self):
        cfg = EmbeddingConfig(provider="openrouter", api_key="or-key")
        provider = create_embedding_provider(cfg, logger=logger)
        assert isinstance(provider, OpenRouterEmbeddingProvider)

    def test_creates_openai(self):
        cfg = EmbeddingConfig(provider="openai", api_key="oa-key")
        provider = create_embedding_provider(cfg, logger=logger)
        assert isinstance(provider, OpenAIEmbeddingProvider)

    def test_rejects_unsupported(self):
        cfg = EmbeddingConfig(provider="unknown", api_key="key")
        with pytest.raises(ValueError, match="Unsupported embedding provider"):
            create_embedding_provider(cfg, logger=logger)
