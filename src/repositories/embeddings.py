"""Embedding repository: immutable pgvector rows and exact retrieval.

Repositories never commit; the caller owns transaction boundaries. Embedding
rows are INSERT-only: the UNIQUE (object_id, model, dimensions, purpose,
content_hash) constraint is the "one immutable row per semantic object +
vector space" invariant, writers use ON CONFLICT DO NOTHING plus a re-read of
the winning row, and no UPDATE or DELETE path exists here.

Vectors bind through pgvector's ``Vector`` wrapper (the pooled connections in
src/db/pool.py register its dumper); reads come back as plain ``list[float]``
via the pool's loaders. Retrieval is EXACT cosine distance over
model+dimension-filtered rows — no ANN index exists by design.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime as dt
from typing import Literal

import psycopg
from pgvector import Vector

PURPOSE_CLAIM_QUERY: Literal["claim_query"] = "claim_query"
PURPOSE_STORY_DOCUMENT: Literal["story_document"] = "story_document"

_BACKFILL_BATCH_DEFAULT = 500


@dataclass(frozen=True)
class StoryVectorCandidate:
    """One exact-retrieval hit: the story's current revision vector."""

    story_id: int
    story_revision_id: int
    embedding_id: int
    vector_distance: float

    @classmethod
    def from_row(cls, row: Sequence) -> StoryVectorCandidate:
        return cls(
            story_id=int(row[0]),
            story_revision_id=int(row[1]),
            embedding_id=int(row[2]),
            vector_distance=float(row[3]),
        )


@dataclass(frozen=True)
class ClaimEmbeddingRow:
    """One full immutable claim_embeddings row including the stored vector."""

    id: int
    claim_id: int
    model: str
    dimensions: int
    purpose: str
    vector: list[float]

    @classmethod
    def from_row(cls, row: Sequence) -> ClaimEmbeddingRow:
        return cls(
            id=int(row[0]),
            claim_id=int(row[1]),
            model=str(row[2]),
            dimensions=int(row[3]),
            purpose=str(row[4]),
            vector=list(row[5]),
        )


@dataclass(frozen=True)
class ClaimEmbeddingIdentity:
    """Vector-space identity of one claim embedding (no vector payload).

    The story-matching prerequisite barrier uses the LATEST claim_query row
    to freeze which space matching runs in."""

    id: int
    claim_id: int
    model: str
    dimensions: int

    @classmethod
    def from_row(cls, row: Sequence) -> ClaimEmbeddingIdentity:
        return cls(
            id=int(row[0]),
            claim_id=int(row[1]),
            model=str(row[2]),
            dimensions=int(row[3]),
        )


@dataclass(frozen=True)
class StoryRevisionRef:
    """Minimal story-revision projection for embedding input ownership."""

    id: int
    semantic_text: str

    @classmethod
    def from_row(cls, row: Sequence) -> StoryRevisionRef:
        return cls(id=int(row[0]), semantic_text=str(row[1]))


class EmbeddingRepository:
    """Persistence for `claim_embeddings` / `story_revision_embeddings`."""

    async def insert_claim_embedding(
        self,
        conn: psycopg.AsyncConnection,
        *,
        claim_id: int,
        embedding: Sequence[float],
        model: str,
        dimensions: int,
        purpose: str,
        content_hash: str,
    ) -> int | None:
        """Insert one immutable row; None means a concurrent writer already
        stored the identical row (caller re-reads the winner)."""
        cursor = await conn.execute(
            """
            INSERT INTO claim_embeddings (
                claim_id, embedding, model, dimensions, purpose, content_hash
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (claim_id, model, dimensions, purpose, content_hash)
            DO NOTHING
            RETURNING id
            """,
            (claim_id, Vector(list(embedding)), model, dimensions, purpose, content_hash),
        )
        row = await cursor.fetchone()
        return None if row is None else int(row[0])

    async def get_claim_embedding(
        self,
        conn: psycopg.AsyncConnection,
        *,
        claim_id: int,
        model: str,
        dimensions: int,
        purpose: str,
        content_hash: str,
    ) -> int | None:
        """Existing row id for the exact vector space + content, if any."""
        cursor = await conn.execute(
            """
            SELECT id FROM claim_embeddings
            WHERE claim_id = %s AND model = %s AND dimensions = %s
              AND purpose = %s AND content_hash = %s
            """,
            (claim_id, model, dimensions, purpose, content_hash),
        )
        row = await cursor.fetchone()
        return None if row is None else int(row[0])

    async def list_claim_embedding_ids(
        self,
        conn: psycopg.AsyncConnection,
        *,
        claim_id: int,
        model: str,
        dimensions: int,
    ) -> list[int]:
        cursor = await conn.execute(
            """
            SELECT id FROM claim_embeddings
            WHERE claim_id = %s AND model = %s AND dimensions = %s
            ORDER BY id
            """,
            (claim_id, model, dimensions),
        )
        return [int(row[0]) for row in await cursor.fetchall()]

    async def get_claim_embedding_row(
        self, conn: psycopg.AsyncConnection, *, embedding_id: int
    ) -> ClaimEmbeddingRow | None:
        """The full immutable row (vector included) for one embedding id.

        Callers use this to verify that a queued match_claim job's frozen
        ``claim_embedding_id`` really lives in the queued policy's vector
        space before any retrieval runs."""
        cursor = await conn.execute(
            """
            SELECT id, claim_id, model, dimensions, purpose, embedding
            FROM claim_embeddings WHERE id = %s
            """,
            (embedding_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else ClaimEmbeddingRow.from_row(row)

    async def latest_claim_embedding_identity(
        self, conn: psycopg.AsyncConnection, *, claim_id: int
    ) -> ClaimEmbeddingIdentity | None:
        """The newest claim_query vector for one claim (highest id wins).

        The prerequisite barrier freezes matching to this exact space, so
        "compatible embedding exists" has one deterministic meaning."""
        cursor = await conn.execute(
            """
            SELECT id, claim_id, model, dimensions
            FROM claim_embeddings
            WHERE claim_id = %s AND purpose = 'claim_query'
            ORDER BY id DESC LIMIT 1
            """,
            (claim_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else ClaimEmbeddingIdentity.from_row(row)

    async def insert_story_revision_embedding(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_revision_id: int,
        embedding: Sequence[float],
        model: str,
        dimensions: int,
        purpose: str,
        content_hash: str,
    ) -> int | None:
        cursor = await conn.execute(
            """
            INSERT INTO story_revision_embeddings (
                story_revision_id, embedding, model, dimensions, purpose, content_hash
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (story_revision_id, model, dimensions, purpose, content_hash)
            DO NOTHING
            RETURNING id
            """,
            (
                story_revision_id,
                Vector(list(embedding)),
                model,
                dimensions,
                purpose,
                content_hash,
            ),
        )
        row = await cursor.fetchone()
        return None if row is None else int(row[0])

    async def get_story_revision_embedding(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_revision_id: int,
        model: str,
        dimensions: int,
        purpose: str,
        content_hash: str,
    ) -> int | None:
        cursor = await conn.execute(
            """
            SELECT id FROM story_revision_embeddings
            WHERE story_revision_id = %s AND model = %s AND dimensions = %s
              AND purpose = %s AND content_hash = %s
            """,
            (story_revision_id, model, dimensions, purpose, content_hash),
        )
        row = await cursor.fetchone()
        return None if row is None else int(row[0])

    async def get_story_revision(
        self, conn: psycopg.AsyncConnection, story_revision_id: int
    ) -> StoryRevisionRef | None:
        cursor = await conn.execute(
            "SELECT id, semantic_text FROM story_revisions WHERE id = %s",
            (story_revision_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else StoryRevisionRef.from_row(row)

    async def find_story_candidates(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        query_embedding: Sequence[float],
        model: str,
        dimensions: int,
        limit: int,
        event_time_start: dt | None = None,
        event_time_end: dt | None = None,
    ) -> list[StoryVectorCandidate]:
        """Exact `<=>` cosine candidates for one edition.

        Filters edition / lifecycle ('active', 'reopened', 'resolved') /
        broad revision-creation time window / exact model + dimensions before
        ordering by distance, so heterogeneous vector spaces never meet the
        distance operator. No ANN index: this is a filtered exact scan.
        """
        query_vec = Vector(list(query_embedding))
        sql = """
        SELECT s.id AS story_id, sr.id AS story_revision_id, sre.id AS embedding_id,
               sre.embedding <=> %s AS vector_distance
        FROM stories s
        JOIN story_revisions sr ON sr.id = s.current_revision_id
        JOIN story_revision_embeddings sre ON sre.story_revision_id = sr.id
        WHERE s.edition_id = %s AND sre.model = %s AND sre.dimensions = %s
          AND s.lifecycle_state IN ('active', 'reopened', 'resolved')
        """
        params: list[object] = [query_vec, edition_id, model, dimensions]
        if event_time_start is not None:
            # Broad-time proxy until dedicated story temporal columns land;
            # the revision creation moment bounds when the meaning was formed.
            sql += "  AND sr.created_at >= %s\n"
            params.append(event_time_start)
        if event_time_end is not None:
            sql += "  AND sr.created_at <= %s\n"
            params.append(event_time_end)
        sql += "ORDER BY sre.embedding <=> %s\nLIMIT %s"
        params.extend([query_vec, limit])
        cursor = await conn.execute(sql, params)
        return [StoryVectorCandidate.from_row(row) async for row in cursor]

    async def list_claim_ids_missing_embedding(
        self,
        conn: psycopg.AsyncConnection,
        *,
        model: str,
        dimensions: int,
        after_claim_id: int | None = None,
        limit: int = _BACKFILL_BATCH_DEFAULT,
    ) -> list[int]:
        """Claims without ANY row in the exact (model, dimensions,
        claim_query) space — bounded slice with optional exclusive cursor."""
        cursor = await conn.execute(
            """
            SELECT c.id
            FROM claims c
            WHERE c.id > COALESCE(%s, 0)
              AND NOT EXISTS (
                  SELECT 1 FROM claim_embeddings e
                  WHERE e.claim_id = c.id AND e.model = %s
                    AND e.dimensions = %s AND e.purpose = %s
              )
            ORDER BY c.id
            LIMIT %s
            """,
            (after_claim_id, model, dimensions, PURPOSE_CLAIM_QUERY, limit),
        )
        return [int(row[0]) for row in await cursor.fetchall()]

    async def list_story_revision_ids_missing_embedding(
        self,
        conn: psycopg.AsyncConnection,
        *,
        model: str,
        dimensions: int,
        after_story_revision_id: int | None = None,
        limit: int = _BACKFILL_BATCH_DEFAULT,
    ) -> list[int]:
        cursor = await conn.execute(
            """
            SELECT sr.id
            FROM story_revisions sr
            WHERE sr.id > COALESCE(%s, 0)
              AND NOT EXISTS (
                  SELECT 1 FROM story_revision_embeddings e
                  WHERE e.story_revision_id = sr.id AND e.model = %s
                    AND e.dimensions = %s AND e.purpose = %s
              )
            ORDER BY sr.id
            LIMIT %s
            """,
            (after_story_revision_id, model, dimensions, PURPOSE_STORY_DOCUMENT, limit),
        )
        return [int(row[0]) for row in await cursor.fetchall()]
