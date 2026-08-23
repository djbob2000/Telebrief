"""Semantic embedding service (Plan 3 Task 5): input ownership, immutable
vector rows, and the claim-pipeline post-success handoff.

Rulings implemented here:

* :class:`EmbeddingInputBuilder` is THE single owner of embedding text.
  A Claim embeds its complete ``normalized_assertion``; a StoryRevision
  embeds its complete compact ``semantic_text``. Raw SourceItemRevision
  text is never read here and there is no token/character/sentence
  chunking anywhere in this module — if a future provider rejects an
  oversized semantic object, that is an upstream representation problem,
  never something to split at this layer.
* One immutable embedding row per (semantic object, model, dimensions,
  purpose, content_hash). Reuse short-circuits before any provider call;
  a changed model/dimensions schedules new work instead of touching old
  rows.
* The provider call runs OUTSIDE any transaction so a slow embedding API
  never holds a pooled connection; persistence happens in its own single
  transaction with ON CONFLICT DO NOTHING + winner re-read for races.

The atomic pipeline handoff lives in
:class:`src.processing.claims.ClaimExtractionService._persist_success`:
claims, canonical run success, and one ``embed_claim`` defer per newly
created claim commit together on the same connection.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable

from src.db.uow import DatabaseUnitOfWork
from src.domain.claims import Claim
from src.embedding_providers import EmbeddingPurpose, validate_vector
from src.repositories.claims import ClaimRepository
from src.repositories.embeddings import (
    PURPOSE_CLAIM_QUERY,
    PURPOSE_STORY_DOCUMENT,
    EmbeddingRepository,
)

logger = logging.getLogger(__name__)


def content_hash(text: str) -> str:
    """Stable identity of one embedding input."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EmbeddingInputBuilder:
    """Sole owner of what becomes an embedding input — whole objects only."""

    @staticmethod
    def for_claim(claim: Claim) -> str:
        """The complete self-contained normalized assertion; nothing else."""
        normalized = claim.normalized_assertion
        if not isinstance(normalized, str) or not normalized.strip():
            raise ValueError(f"claim {claim.id} has no normalized_assertion")
        return normalized

    @staticmethod
    def for_story_revision(revision) -> str:
        """The complete compact semantic text of ONE current story meaning.

        Accepts any object exposing ``semantic_text`` (the full StoryRevision
        domain model lands with the story task and satisfies this shape).
        """
        semantic_text = getattr(revision, "semantic_text", None)
        if not isinstance(semantic_text, str) or not semantic_text.strip():
            raise ValueError(f"story revision {getattr(revision, 'id', '?')} has no semantic_text")
        return semantic_text


class EmbeddingService:
    """Embed semantic objects into one exact model/dimension vector space."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        provider,
        repo: EmbeddingRepository | None = None,
        claim_repo: ClaimRepository | None = None,
    ) -> None:
        self.uow = uow
        self.provider = provider
        self._repo = repo or EmbeddingRepository()
        self._claim_repo = claim_repo or ClaimRepository()

    async def _embed_object(
        self,
        *,
        text: str,
        purpose: EmbeddingPurpose,
        model: str,
        dimensions: int,
        insert: Callable[..., Awaitable[int | None]],
        reuse_lookup: Callable[..., Awaitable[int | None]],
    ) -> int | None:
        """Shared shape: reuse check -> provider outside txn -> one insert txn.

        ``reuse_lookup`` / ``insert`` are repository callables bound to the
        concrete object id; both receive an open connection. The UNIQUE
        identity constraint makes concurrent duplicates converge on one
        immutable row.
        """
        digest = content_hash(text)
        async with self.uow.transaction() as conn:
            existing = await reuse_lookup(
                conn, purpose=purpose, content_hash=digest, model=model, dimensions=dimensions
            )
            if existing is not None:
                logger.debug(
                    "embedding reused (%s dims=%d hash=%s...)", model, dimensions, digest[:12]
                )
                return existing
        vector = validate_vector(
            await self.provider.embed(text, purpose=purpose, model=model, dimensions=dimensions),
            model=model,
            dimensions=dimensions,
        )
        async with self.uow.transaction() as conn:
            inserted = await insert(
                conn,
                embedding=vector,
                model=model,
                dimensions=dimensions,
                purpose=purpose,
                content_hash=digest,
            )
            if inserted is not None:
                return inserted
            # A concurrent writer stored the identical row first: converge.
            return await reuse_lookup(
                conn, purpose=purpose, content_hash=digest, model=model, dimensions=dimensions
            )

    async def embed_claim(self, claim_id: int, *, model: str, dimensions: int) -> int | None:
        """Ensure the claim_query vector exists for this exact assertion.

        Returns the immutable embedding row id (the existing row on reuse).
        """
        async with self.uow.transaction() as conn:
            claims = await self._claim_repo.get_many(conn, [claim_id])
        if not claims:
            raise ValueError(f"claim {claim_id} does not exist")
        return await self._embed_object(
            text=EmbeddingInputBuilder.for_claim(claims[0]),
            purpose=PURPOSE_CLAIM_QUERY,
            model=model,
            dimensions=dimensions,
            insert=lambda conn, **kw: self._repo.insert_claim_embedding(
                conn, claim_id=claim_id, **kw
            ),
            reuse_lookup=lambda conn, **kw: self._repo.get_claim_embedding(
                conn, claim_id=claim_id, **kw
            ),
        )

    async def embed_story_revision(
        self, story_revision_id: int, *, model: str, dimensions: int
    ) -> int | None:
        """Ensure the story_document vector exists for this exact semantic text.

        Returns the immutable embedding row id (the existing row on reuse).
        """
        async with self.uow.transaction() as conn:
            revision = await self._repo.get_story_revision(conn, story_revision_id)
        if revision is None:
            raise ValueError(f"story revision {story_revision_id} does not exist")
        return await self._embed_object(
            text=EmbeddingInputBuilder.for_story_revision(revision),
            purpose=PURPOSE_STORY_DOCUMENT,
            model=model,
            dimensions=dimensions,
            insert=lambda conn, **kw: self._repo.insert_story_revision_embedding(
                conn, story_revision_id=story_revision_id, **kw
            ),
            reuse_lookup=lambda conn, **kw: self._repo.get_story_revision_embedding(
                conn, story_revision_id=story_revision_id, **kw
            ),
        )
