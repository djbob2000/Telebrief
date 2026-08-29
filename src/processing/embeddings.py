"""Semantic embedding services for both legacy claim matching and event-first fragments."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import psycopg

from src.db.uow import DatabaseUnitOfWork
from src.domain.claims import Claim
from src.domain.event_pipeline import SourceFragment
from src.embedding_providers import EmbeddingProvider, EmbeddingPurpose, validate_vector
from src.processing.story_matching import StoryMatchingPrerequisiteService
from src.repositories.claims import ClaimRepository
from src.repositories.embeddings import (
    PURPOSE_CLAIM_QUERY,
    PURPOSE_STORY_DOCUMENT,
    EmbeddingRepository,
    FragmentEmbeddingRepository,
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
    def for_story_revision(revision: Any) -> str:
        """The complete compact semantic text of ONE current story meaning."""
        semantic_text = getattr(revision, "semantic_text", None)
        if not isinstance(semantic_text, str) or not semantic_text.strip():
            raise ValueError(f"story revision {getattr(revision, 'id', '?')} has no semantic_text")
        return semantic_text


class FragmentEmbeddingService:
    """Service to ensure all candidate fragments are embedded with batching and deduplication."""

    def __init__(
        self,
        repository: FragmentEmbeddingRepository | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        self.repo = repository or FragmentEmbeddingRepository()
        self.logger = logger_instance or logger

    async def ensure_fragment_embeddings(
        self,
        conn: psycopg.AsyncConnection,
        fragments: Sequence[SourceFragment],
        *,
        provider: EmbeddingProvider,
        provider_name: str,
        model: str,
        dimensions: int,
        batch_size: int = 128,
    ) -> dict[int, tuple[int, list[float]]]:
        """Ensure all given candidate fragments have embeddings, reusing deduplicated hashes.

        Returns {fragment_id: (fragment_embedding_id, vector_floats)}.
        """
        candidates = [f for f in fragments if f.is_candidate]
        if not candidates:
            return {}

        # 1. Check which fragments are already fully linked
        existing_map = await self.repo.get_fragment_embeddings_map(conn, [f.id for f in candidates])
        missing_frags = [f for f in candidates if f.id not in existing_map]
        if not missing_frags:
            return existing_map

        # 2. Check existing vector cache by normalized_hash
        unique_hashes = list({f.normalized_hash for f in missing_frags})
        cached_vectors = await self.repo.get_vectors_by_hashes(
            conn, unique_hashes, model=model, dimensions=dimensions
        )

        # 3. Find hashes that need provider embedding
        hashes_to_embed = [h for h in unique_hashes if h not in cached_vectors]
        hash_to_sample_text: dict[str, str] = {}
        for f in missing_frags:
            if (
                f.normalized_hash in hashes_to_embed
                and f.normalized_hash not in hash_to_sample_text
            ):
                hash_to_sample_text[f.normalized_hash] = f.text_content

        # 4. Process missing hashes in batches
        if hashes_to_embed:
            for i in range(0, len(hashes_to_embed), batch_size):
                batch_hashes = hashes_to_embed[i : i + batch_size]
                batch_texts = [hash_to_sample_text[h] for h in batch_hashes]
                total_chars = sum(len(t) for t in batch_texts)

                batch_id = await self.repo.record_batch_start(
                    conn,
                    provider=provider_name,
                    model=model,
                    dimensions=dimensions,
                    item_count=len(batch_texts),
                    input_chars=total_chars,
                )
                try:
                    vectors = await provider.embed_many(
                        batch_texts,
                        purpose="story_document",
                        model=model,
                        dimensions=dimensions,
                    )
                    await self.repo.record_batch_completion(conn, batch_id, status="succeeded")
                except Exception as exc:
                    await self.repo.record_batch_completion(
                        conn, batch_id, status="failed", error_kind=type(exc).__name__
                    )
                    raise

                # Persist new vectors
                items_to_insert = list(zip(batch_hashes, vectors, strict=True))
                inserted_map = await self.repo.insert_vectors_batch(
                    conn,
                    model=model,
                    dimensions=dimensions,
                    items=items_to_insert,
                )
                for h, v in zip(batch_hashes, vectors, strict=True):
                    if h in inserted_map:
                        cached_vectors[h] = (inserted_map[h], v)

        # 5. Link all missing fragments to fragment_embeddings
        links_to_insert = [
            (f.id, cached_vectors[f.normalized_hash][0])
            for f in missing_frags
            if f.normalized_hash in cached_vectors
        ]
        if links_to_insert:
            await self.repo.link_fragment_embeddings_batch(conn, links_to_insert)

        return await self.repo.get_fragment_embeddings_map(conn, [f.id for f in candidates])


class EmbeddingService:
    """Embed semantic objects into one exact model/dimension vector space."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork | None = None,
        provider: Any = None,
        repo: EmbeddingRepository | None = None,
        claim_repo: ClaimRepository | None = None,
        matching_handoff: bool = False,
        matching_prerequisites: StoryMatchingPrerequisiteService | None = None,
        fragment_repo: FragmentEmbeddingRepository | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        self.uow = uow
        self.provider = provider
        self._repo = repo or EmbeddingRepository()
        self._claim_repo = claim_repo or ClaimRepository()
        self._matching_handoff = matching_handoff
        self._prerequisites = matching_prerequisites or StoryMatchingPrerequisiteService()
        self._fragment_service = FragmentEmbeddingService(
            repository=fragment_repo, logger_instance=logger_instance
        )

    async def ensure_fragment_embeddings(
        self,
        conn: psycopg.AsyncConnection,
        fragments: Sequence[SourceFragment],
        *,
        provider: EmbeddingProvider,
        provider_name: str,
        model: str,
        dimensions: int,
        batch_size: int = 128,
    ) -> dict[int, tuple[int, list[float]]]:
        return await self._fragment_service.ensure_fragment_embeddings(
            conn,
            fragments,
            provider=provider,
            provider_name=provider_name,
            model=model,
            dimensions=dimensions,
            batch_size=batch_size,
        )

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
        if self.uow is None:
            raise RuntimeError("DatabaseUnitOfWork must be configured on EmbeddingService")
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
                visible_id = await reuse_lookup(
                    conn, purpose=purpose, content_hash=digest, model=model, dimensions=dimensions
                )
            if visible_id is not None and on_visible is not None:
                await on_visible(conn, embedding_id=visible_id)
            return visible_id

    async def embed_claim(self, claim_id: int, *, model: str, dimensions: int) -> int | None:
        if self.uow is None:
            raise RuntimeError("DatabaseUnitOfWork must be configured on EmbeddingService")
        async with self.uow.transaction() as conn:
            claims = await self._claim_repo.get_many(conn, [claim_id])
        if not claims:
            raise ValueError(f"claim {claim_id} does not exist")
        claim = claims[0]
        on_visible = (
            (
                lambda conn, *, embedding_id: self._handoff_to_matching(
                    conn,
                    claim=claim,
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

    async def _handoff_to_matching(
        self,
        conn: psycopg.AsyncConnection,
        *,
        claim: Claim,
    ) -> None:
        scheduled = await self._prerequisites.maybe_schedule(conn, claim_id=claim.id)
        if not scheduled:
            logger.info(
                "matching prerequisites unsatisfied for claim=%s; deferral withheld",
                claim.id,
            )

    async def embed_story_revision(
        self, story_revision_id: int, *, model: str, dimensions: int
    ) -> int | None:
        if self.uow is None:
            raise RuntimeError("DatabaseUnitOfWork must be configured on EmbeddingService")
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
