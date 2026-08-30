"""Embedding provider abstraction (Plan 3 Task 5): one protocol, one adapter.

The protocol is intentionally narrow: a provider turns ONE complete semantic
text into exactly ``dimensions`` floats for a declared purpose. Chunking is
forbidden upstream (EmbeddingInputBuilder owns the whole-object input), so
providers never see fragments and never split anything.

Google adapter: the repo's established Gemini integration runs through the
OpenAI-compatible endpoint (``src.ai_providers.GOOGLE_BASE_URL``) with an
``AsyncOpenAI`` client — the same transport as :class:`GoogleProvider`, reusing
its key handling conventions and timeout shape. The embeddings route of that
endpoint accepts ``model``, ``input`` and ``dimensions``.

Purpose mapping: Gemini's native API distinguishes retrieval queries from
documents via ``taskType`` (claim_query -> "retrieval_query",
story_document -> "retrieval_document"). The OpenAI-compatible embeddings
route does NOT expose taskType, so the mapping below documents where the
purpose lands once a native genai client is introduced; until then both
purposes travel the same endpoint and Gemini applies its default document
task type, while purpose remains part of each row's immutable identity.
The key is never logged: it lives only in the client configuration.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Literal, Protocol

import httpx
from openai import AsyncOpenAI

from src.ai_providers import GOOGLE_BASE_URL, OPENROUTER_BASE_URL

__all__ = [
    "EMBEDDING_PURPOSES",
    "EmbeddingDimensionMismatch",
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "GoogleGeminiEmbeddingProvider",
    "OpenRouterEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "create_embedding_provider",
]

EmbeddingPurpose = Literal["claim_query", "story_document"]

EMBEDDING_PURPOSES: tuple[str, ...] = ("claim_query", "story_document")

# Gemini native-API taskType values keyed by our purpose vocabulary. The
# OpenAI-compatible endpoint used by this adapter cannot carry taskType; kept
# here so the native-client migration is a table lookup, not a redesign.
PURPOSE_TASK_TYPES: dict[str, str] = {
    "claim_query": "retrieval_query",
    "story_document": "retrieval_document",
}


class EmbeddingProviderError(RuntimeError):
    """Base class for embedding provider failures (transport/API/protocol)."""


class EmbeddingDimensionMismatch(EmbeddingProviderError):
    """The returned vector's length differs from the queued dimensions."""


class EmbeddingProvider(Protocol):
    """Turn semantic text into exactly ``dimensions`` floats."""

    async def embed(
        self,
        text: str,
        *,
        purpose: EmbeddingPurpose,
        model: str,
        dimensions: int,
    ) -> list[float]: ...

    async def embed_many(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose,
        model: str,
        dimensions: int,
    ) -> list[list[float]]: ...


def validate_vector(vector: list[float], *, model: str, dimensions: int) -> list[float]:
    """Shared response-shape guard: exact dimensionality or typed failure."""
    if len(vector) != dimensions:
        raise EmbeddingDimensionMismatch(
            f"embedding model {model!r} returned {len(vector)} floats, expected {dimensions}"
        )
    return vector


def _require_purpose(purpose: str) -> None:
    if purpose not in EMBEDDING_PURPOSES:
        raise ValueError(
            f"embedding purpose must be one of {', '.join(EMBEDDING_PURPOSES)}, got {purpose!r}"
        )


class GoogleGeminiEmbeddingProvider:
    """Gemini embeddings through the repo's OpenAI-compatible Google slot."""

    provider_label = "Gemini"

    def __init__(
        self,
        api_key: str,
        logger: logging.Logger,
        timeout: int = 45,
    ):
        if not api_key:
            raise ValueError("GoogleGeminiEmbeddingProvider requires a Gemini API key")
        self.logger = logger
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=GOOGLE_BASE_URL,
            timeout=httpx.Timeout(timeout, connect=min(10.0, float(timeout))),
            max_retries=0,
        )

    async def embed(
        self,
        text: str,
        *,
        purpose: EmbeddingPurpose,
        model: str,
        dimensions: int,
    ) -> list[float]:
        """One input -> one vector of exactly ``dimensions`` floats."""
        _require_purpose(purpose)
        try:
            response = await self.client.embeddings.create(
                model=model,
                input=text,
                dimensions=dimensions,
            )
        except Exception as exc:
            raise EmbeddingProviderError(
                f"{self.provider_label} embedding request failed for model {model!r}: {type(exc).__name__}"
            ) from exc
        data = getattr(response, "data", None)
        if not data:
            raise EmbeddingProviderError(
                f"{self.provider_label} embedding response for model {model!r} contained no vectors"
            )
        vector = list(getattr(data[0], "embedding", ()) or ())
        return validate_vector(vector, model=model, dimensions=dimensions)

    async def embed_many(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose,
        model: str,
        dimensions: int,
    ) -> list[list[float]]:
        if not texts:
            return []
        _require_purpose(purpose)
        try:
            response = await self.client.embeddings.create(
                model=model,
                input=list(texts),
                dimensions=dimensions,
            )
        except Exception as exc:
            raise EmbeddingProviderError(
                f"{self.provider_label} batch embedding request failed for model {model!r}: {type(exc).__name__}"
            ) from exc
        data = getattr(response, "data", None)
        if not data or len(data) != len(texts):
            raise EmbeddingProviderError(
                f"{self.provider_label} batch embedding response for model {model!r} returned {len(data) if data else 0} vectors, expected {len(texts)}"
            )
        sorted_data = sorted(data, key=lambda item: getattr(item, "index", 0))
        return [
            validate_vector(
                list(getattr(item, "embedding", ()) or ()), model=model, dimensions=dimensions
            )
            for item in sorted_data
        ]


class OpenRouterEmbeddingProvider:
    """OpenRouter embeddings provider using OpenRouter's OpenAI-compatible endpoint."""

    provider_label = "OpenRouter"

    def __init__(
        self,
        api_key: str,
        logger: logging.Logger,
        base_url: str = OPENROUTER_BASE_URL,
        timeout: int = 45,
    ):
        if not api_key:
            raise ValueError("OpenRouterEmbeddingProvider requires an OpenRouter API key")
        self.logger = logger
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=min(10.0, float(timeout))),
            max_retries=0,
        )

    async def embed(
        self,
        text: str,
        *,
        purpose: EmbeddingPurpose,
        model: str,
        dimensions: int,
    ) -> list[float]:
        """One input -> one vector of exactly ``dimensions`` floats."""
        _require_purpose(purpose)
        try:
            response = await self.client.embeddings.create(
                model=model,
                input=text,
                dimensions=dimensions,
            )
        except Exception as exc:
            raise EmbeddingProviderError(
                f"{self.provider_label} embedding request failed for model {model!r}: {type(exc).__name__}"
            ) from exc
        data = getattr(response, "data", None)
        if not data:
            raise EmbeddingProviderError(
                f"{self.provider_label} embedding response for model {model!r} contained no vectors"
            )
        vector = list(getattr(data[0], "embedding", ()) or ())
        return validate_vector(vector, model=model, dimensions=dimensions)

    async def embed_many(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose,
        model: str,
        dimensions: int,
    ) -> list[list[float]]:
        if not texts:
            return []
        _require_purpose(purpose)
        try:
            response = await self.client.embeddings.create(
                model=model,
                input=list(texts),
                dimensions=dimensions,
            )
        except Exception as exc:
            raise EmbeddingProviderError(
                f"{self.provider_label} batch embedding request failed for model {model!r}: {type(exc).__name__}"
            ) from exc
        data = getattr(response, "data", None)
        if not data or len(data) != len(texts):
            raise EmbeddingProviderError(
                f"{self.provider_label} batch embedding response for model {model!r} returned {len(data) if data else 0} vectors, expected {len(texts)}"
            )
        sorted_data = sorted(data, key=lambda item: getattr(item, "index", 0))
        return [
            validate_vector(
                list(getattr(item, "embedding", ()) or ()), model=model, dimensions=dimensions
            )
            for item in sorted_data
        ]


class OpenAIEmbeddingProvider:
    """OpenAI embeddings provider."""

    provider_label = "OpenAI"

    def __init__(
        self,
        api_key: str,
        logger: logging.Logger,
        base_url: str | None = None,
        timeout: int = 45,
    ):
        if not api_key:
            raise ValueError("OpenAIEmbeddingProvider requires an OpenAI API key")
        self.logger = logger
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=min(10.0, float(timeout))),
            max_retries=0,
        )

    async def embed(
        self,
        text: str,
        *,
        purpose: EmbeddingPurpose,
        model: str,
        dimensions: int,
    ) -> list[float]:
        """One input -> one vector of exactly ``dimensions`` floats."""
        _require_purpose(purpose)
        try:
            response = await self.client.embeddings.create(
                model=model,
                input=text,
                dimensions=dimensions,
            )
        except Exception as exc:
            raise EmbeddingProviderError(
                f"{self.provider_label} embedding request failed for model {model!r}: {type(exc).__name__}"
            ) from exc
        data = getattr(response, "data", None)
        if not data:
            raise EmbeddingProviderError(
                f"{self.provider_label} embedding response for model {model!r} contained no vectors"
            )
        vector = list(getattr(data[0], "embedding", ()) or ())
        return validate_vector(vector, model=model, dimensions=dimensions)

    async def embed_many(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose,
        model: str,
        dimensions: int,
    ) -> list[list[float]]:
        if not texts:
            return []
        _require_purpose(purpose)
        try:
            response = await self.client.embeddings.create(
                model=model,
                input=list(texts),
                dimensions=dimensions,
            )
        except Exception as exc:
            raise EmbeddingProviderError(
                f"{self.provider_label} batch embedding request failed for model {model!r}: {type(exc).__name__}"
            ) from exc
        data = getattr(response, "data", None)
        if not data or len(data) != len(texts):
            raise EmbeddingProviderError(
                f"{self.provider_label} batch embedding response for model {model!r} returned {len(data) if data else 0} vectors, expected {len(texts)}"
            )
        sorted_data = sorted(data, key=lambda item: getattr(item, "index", 0))
        return [
            validate_vector(
                list(getattr(item, "embedding", ()) or ()), model=model, dimensions=dimensions
            )
            for item in sorted_data
        ]


def create_embedding_provider(
    config: Any,
    logger: logging.Logger,
) -> EmbeddingProvider:
    """Assemble an EmbeddingProvider instance from config."""
    embedding_config = getattr(config, "embedding", config)
    provider_name = (getattr(embedding_config, "provider", "google") or "google").lower()
    api_key = getattr(embedding_config, "api_key", "")
    timeout = getattr(embedding_config, "timeout", 45)

    if provider_name == "google":
        return GoogleGeminiEmbeddingProvider(
            api_key=api_key,
            logger=logger,
            timeout=timeout,
        )
    if provider_name == "openrouter":
        return OpenRouterEmbeddingProvider(
            api_key=api_key,
            logger=logger,
            timeout=timeout,
        )
    if provider_name == "openai":
        return OpenAIEmbeddingProvider(
            api_key=api_key,
            logger=logger,
            timeout=timeout,
        )
    raise ValueError(f"Unsupported embedding provider: {provider_name!r}")
