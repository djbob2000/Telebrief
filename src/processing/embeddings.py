"""Semantic embedding services for Event-First fragments."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import psycopg

from src.domain.event_pipeline import SourceFragment
from src.embedding_providers import EmbeddingProvider
from src.repositories.embeddings import FragmentEmbeddingRepository

logger = logging.getLogger(__name__)


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
        fragment_repo: FragmentEmbeddingRepository | None = None,
        logger_instance: logging.Logger | None = None,
        **_kwargs: object,
    ) -> None:
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
