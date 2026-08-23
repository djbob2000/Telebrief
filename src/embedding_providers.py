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
from typing import Literal, Protocol

import httpx
from openai import AsyncOpenAI

from src.ai_providers import GOOGLE_BASE_URL

__all__ = [
    "EMBEDDING_PURPOSES",
    "EmbeddingDimensionMismatch",
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "GoogleGeminiEmbeddingProvider",
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
    """Turn one whole semantic text into exactly ``dimensions`` floats."""

    async def embed(
        self,
        text: str,
        *,
        purpose: EmbeddingPurpose,
        model: str,
        dimensions: int,
    ) -> list[float]: ...


def validate_vector(vector: list[float], *, model: str, dimensions: int) -> list[float]:
    """Shared response-shape guard: exact dimensionality or typed failure."""
    if len(vector) != dimensions:
        raise EmbeddingDimensionMismatch(
            f"embedding model {model!r} returned {len(vector)} floats, expected {dimensions}"
        )
    return vector


class GoogleGeminiEmbeddingProvider:
    """Gemini embeddings through the repo's OpenAI-compatible Google slot."""

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
            # Same timeout shape as src.ai_providers.GoogleProvider; no
            # library-level retries — Procrastinate redelivery plus backfill
            # own retry policy.
            timeout=httpx.Timeout(timeout, connect=min(10.0, float(timeout))),
            max_retries=0,
        )

    @staticmethod
    def _require_purpose(purpose: str) -> None:
        if purpose not in EMBEDDING_PURPOSES:
            raise ValueError(
                f"embedding purpose must be one of {', '.join(EMBEDDING_PURPOSES)}, got {purpose!r}"
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
        self._require_purpose(purpose)
        try:
            response = await self.client.embeddings.create(
                model=model,
                input=text,
                dimensions=dimensions,
            )
        except Exception as exc:  # transport / HTTP / auth surface
            # Never include request credentials in the raised message.
            raise EmbeddingProviderError(
                f"Gemini embedding request failed for model {model!r}: {type(exc).__name__}"
            ) from exc
        data = getattr(response, "data", None)
        if not data:
            raise EmbeddingProviderError(
                f"Gemini embedding response for model {model!r} contained no vectors"
            )
        vector = list(getattr(data[0], "embedding", ()) or ())
        return validate_vector(vector, model=model, dimensions=dimensions)
