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
* With ``matching_handoff`` enabled (Plan 3 Task 7), the transaction that
  makes a claim embedding visible — BOTH the insert path and the reuse
  path — atomically resolves/creates the edition's current story-matching
  policy and defers ``match_claim(claim_id, policy_id, claim_embedding_id)``
  on the SAME connection. Retries never silently switch policy version:
  the resolved policy id is frozen into the task arguments. Duplicate
  defers (a retried embed_claim re-hits the reuse path) converge
  downstream on the canonical succeeded run. The per-edition execution
  lock serializes matching without ever dropping a queued claim.

The atomic pipeline handoff lives in
:class:`src.processing.claims.ClaimExtractionService._persist_success`:
claims, canonical run success, and one ``embed_claim`` defer per newly
created claim commit together on the same connection.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable

import psycopg

from src.db.uow import DatabaseUnitOfWork
from src.domain.claims import Claim
from src.embedding_providers import EmbeddingPurpose, validate_vector
from src.processing.story_matching import (
    StoryMatchingPolicyService,
    story_matching_execution_lock,
)
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
        matching_handoff: bool = False,
        matching_policy_service: StoryMatchingPolicyService | None = None,
    ) -> None:
        self.uow = uow
        self.provider = provider
        self._repo = repo or EmbeddingRepository()
        self._claim_repo = claim_repo or ClaimRepository()
        # Story-matching handoff is opt-in at the wiring layer: when enabled,
        # every visible claim embedding defers match_claim atomically.
        self._matching_handoff = matching_handoff
        self._matching_policy_service = matching_policy_service or StoryMatchingPolicyService()

    async def _embed_object(
        self,
        *,
        text: str,
        purpose: EmbeddingPurpose,
        model: str,
        dimensions: int,
        insert: Callable[..., Awaitable[int | None]],
        reuse_lookup: Callable[..., Awaitable[int | None]],
        on_visible: Callable[..., Awaitable[None]] | None = None,
    ) -> int | None:
        """Shared shape: reuse check -> provider outside txn -> one insert txn.

        ``reuse_lookup`` / ``insert`` are repository callables bound to the
        concrete object id; both receive an open connection. The UNIQUE
        identity constraint makes concurrent duplicates converge on one
        immutable row. ``on_visible`` fires INSIDE whichever transaction
        makes the row visible (reuse or insert) so post-success handoffs —
        e.g. the match_claim defer — commit or roll back with it.
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
                if on_visible is not None:
                    await on_visible(conn, embedding_id=existing)
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
                visible_id: int | None = inserted
            else:
                # A concurrent writer stored the identical row first: converge.
                visible_id = await reuse_lookup(
                    conn, purpose=purpose, content_hash=digest, model=model, dimensions=dimensions
                )
            if visible_id is not None and on_visible is not None:
                await on_visible(conn, embedding_id=visible_id)
            return visible_id

    async def embed_claim(self, claim_id: int, *, model: str, dimensions: int) -> int | None:
        """Ensure the claim_query vector exists for this exact assertion.

        Returns the immutable embedding row id (the existing row on reuse).
        With ``matching_handoff`` enabled, whichever transaction makes the
        row visible also resolves/creates the edition's current story-
        matching policy and defers ``match_claim`` on the same connection —
        so an embedding can never be visible without its queued matching
        job (and vice versa on rollback).
        """
        async with self.uow.transaction() as conn:
            claims = await self._claim_repo.get_many(conn, [claim_id])
        if not claims:
            raise ValueError(f"claim {claim_id} does not exist")
        claim = claims[0]
        # Bound OUTSIDE the lambda: the conditional must pick the callback
        # itself, not return None from inside it (await None would explode).
        on_visible = (
            (
                lambda conn, *, embedding_id: self._defer_match_claim(
                    conn,
                    claim=claim,
                    embedding_id=embedding_id,
                    model=model,
                    dimensions=dimensions,
                )
            )
            if self._matching_handoff
            else None
        )
        return await self._embed_object(
            text=EmbeddingInputBuilder.for_claim(claim),
            purpose=PURPOSE_CLAIM_QUERY,
            model=model,
            dimensions=dimensions,
            insert=lambda conn, **kw: self._repo.insert_claim_embedding(
                conn, claim_id=claim_id, **kw
            ),
            reuse_lookup=lambda conn, **kw: self._repo.get_claim_embedding(
                conn, claim_id=claim_id, **kw
            ),
            on_visible=on_visible,
        )

    async def _defer_match_claim(
        self,
        conn: psycopg.AsyncConnection,
        *,
        claim: Claim,
        embedding_id: int,
        model: str,
        dimensions: int,
    ) -> None:
        """Resolve-or-create the current matching policy and queue matching.

        Runs INSIDE the visibility transaction; the resolved policy id is
        frozen into the task arguments, so a retried job keeps its exact
        prompt/config identity. The per-edition execution lock is set at
        defer time (procrastinate ``lock``, verified against 3.9): jobs
        sharing it never run simultaneously, while queueing_lock would
        abort this very transaction via AlreadyEnqueued.
        """
        policy = await self._matching_policy_service.ensure_current(
            conn,
            edition_id=claim.edition_id,
            embedding_model=model,
            embedding_dimensions=dimensions,
        )
        # Lazy on purpose: src.jobs.processing imports this module at top level.
        from src.jobs.processing import match_claim

        await match_claim.configure(
            connection=conn,
            lock=story_matching_execution_lock(claim.edition_id),
        ).defer_async(
            claim_id=claim.id,
            policy_id=policy.id,
            claim_embedding_id=embedding_id,
        )
        logger.info(
            "deferred match_claim claim=%s policy=%s embedding=%s",
            claim.id,
            policy.id,
            embedding_id,
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
