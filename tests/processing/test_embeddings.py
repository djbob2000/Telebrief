"""Tests for deduplicated, cached, and batched fragment embeddings."""

from __future__ import annotations

import datetime as dt

import pytest

from src.domain.event_pipeline import SourceFragment
from src.processing.embeddings import EmbeddingService


class DummyEmbeddingProvider:
    def __init__(self, dimensions: int = 4):
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    async def embed(self, text: str, *, purpose: str, model: str, dimensions: int) -> list[float]:
        self.calls.append([text])
        return [0.1] * dimensions

    async def embed_many(
        self, texts: list[str], *, purpose: str, model: str, dimensions: int
    ) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(i + 1)] * dimensions for i in range(len(texts))]


@pytest.mark.postgres
async def test_embedding_service_deduplicates_and_batches(conn, revision):
    now = dt.datetime.now(dt.timezone.utc)
    # Insert 3 fragments in DB, 2 of which share the same normalized_hash
    await conn.execute(
        """
        INSERT INTO source_fragments (
            id, source_item_revision_id, ordinal, text_content, normalized_hash,
            fragmenter_version, is_candidate, drop_reason, created_at
        ) OVERRIDING SYSTEM VALUE VALUES
        (1001, %s, 0, 'First duplicate text', 'hash_dup', 'v1', TRUE, NULL, %s),
        (1002, %s, 1, 'First duplicate text', 'hash_dup', 'v1', TRUE, NULL, %s),
        (1003, %s, 2, 'Unique second text', 'hash_uniq', 'v1', TRUE, NULL, %s),
        (1004, %s, 3, 'Filtered noise ad', 'hash_ad', 'v1', FALSE, 'commercial_classified', %s)
        """,
        (revision.id, now, revision.id, now, revision.id, now, revision.id, now),
    )

    fragments = [
        SourceFragment(
            1001, revision.id, 0, "First duplicate text", "hash_dup", "v1", True, None, now
        ),
        SourceFragment(
            1002, revision.id, 1, "First duplicate text", "hash_dup", "v1", True, None, now
        ),
        SourceFragment(
            1003, revision.id, 2, "Unique second text", "hash_uniq", "v1", True, None, now
        ),
        SourceFragment(
            1004,
            revision.id,
            3,
            "Filtered noise ad",
            "hash_ad",
            "v1",
            False,
            "commercial_classified",
            now,
        ),
    ]

    provider = DummyEmbeddingProvider(dimensions=4)
    service = EmbeddingService()

    # 1. First run: should embed the 2 unique candidate hashes
    res = await service.ensure_fragment_embeddings(
        conn,
        fragments,
        provider=provider,
        provider_name="test_provider",
        model="test-embed-model",
        dimensions=4,
        batch_size=10,
    )

    assert len(res) == 3
    assert 1001 in res
    assert 1002 in res
    assert 1003 in res
    assert 1004 not in res  # not candidate

    # Vector for 1001 and 1002 should match since they share the same hash
    assert res[1001][1] == res[1002][1]
    # Provider called only once with the 2 unique texts
    assert len(provider.calls) == 1
    assert len(provider.calls[0]) == 2

    # 2. Second run with same fragments: should be 100% cache hits (no provider calls)
    provider.calls.clear()
    res2 = await service.ensure_fragment_embeddings(
        conn,
        fragments,
        provider=provider,
        provider_name="test_provider",
        model="test-embed-model",
        dimensions=4,
        batch_size=10,
    )
    assert len(res2) == 3
    assert len(provider.calls) == 0

    # 3. Verify audit record in event_embedding_batches
    cursor = await conn.execute("SELECT status, item_count, model FROM event_embedding_batches")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "succeeded"
    assert row[1] == 2
    assert row[2] == "test-embed-model"
