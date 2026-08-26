"""Archive repository: storage, metadata, and exact vector search for historical news."""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from pgvector import Vector
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class ArchiveArticle:
    """Historical news article entity."""

    id: int
    edition_slug: str
    source_name: str
    source_url: str | None
    external_id: str | None
    title: str
    content: str
    published_at: dt.datetime
    category: str | None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> ArchiveArticle:
        return cls(
            id=int(row[0]),
            edition_slug=str(row[1]),
            source_name=str(row[2]),
            source_url=str(row[3]) if row[3] is not None else None,
            external_id=str(row[4]) if row[4] is not None else None,
            title=str(row[5]),
            content=str(row[6]),
            published_at=row[7],
            category=str(row[8]) if row[8] is not None else None,
            tags=list(row[9]) if row[9] is not None else [],
            metadata=dict(row[10]) if isinstance(row[10], dict) else {},
            created_at=row[11],
        )


@dataclass(frozen=True)
class ArchiveMatchCandidate:
    """Result of historical vector similarity search."""

    article_id: int
    title: str
    content: str
    published_at: dt.datetime
    source_name: str
    source_url: str | None
    vector_distance: float


def compute_content_hash(text: str) -> str:
    """Compute sha256 hash of semantic text for deduplicating embeddings."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ArchiveRepository:
    """PostgreSQL repository for archive articles and pgvector embeddings."""

    async def insert_article(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_slug: str = "berdyansk",
        source_name: str,
        title: str,
        content: str,
        published_at: dt.datetime,
        source_url: str | None = None,
        external_id: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Insert or ignore an archive article, returning its ID."""
        cursor = await conn.execute(
            """
            INSERT INTO archive_articles (
                edition_slug, source_name, source_url, external_id,
                title, content, published_at, category, tags, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (edition_slug, source_name, external_id)
            DO UPDATE SET
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                published_at = EXCLUDED.published_at,
                category = EXCLUDED.category,
                tags = EXCLUDED.tags,
                metadata = EXCLUDED.metadata
            RETURNING id;
            """,
            (
                edition_slug,
                source_name,
                source_url,
                external_id or compute_content_hash(f"{title}\n{content}")[:32],
                title,
                content,
                published_at,
                category,
                tags or [],
                Jsonb(metadata or {}),
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Failed to insert or retrieve archive article ID")
        return int(row[0])

    async def insert_embedding(
        self,
        conn: psycopg.AsyncConnection,
        *,
        article_id: int,
        vector: list[float],
        model: str,
        dimensions: int,
        content_hash: str,
    ) -> int:
        """Insert vector embedding for an archive article."""
        cursor = await conn.execute(
            """
            INSERT INTO archive_embeddings (
                article_id, embedding, model, dimensions, content_hash
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (article_id, model, dimensions, content_hash)
            DO NOTHING
            RETURNING id;
            """,
            (article_id, Vector(vector), model, dimensions, content_hash),
        )
        row = await cursor.fetchone()
        if row is not None:
            return int(row[0])

        # Read existing row ID
        cursor = await conn.execute(
            """
            SELECT id FROM archive_embeddings
            WHERE article_id = %s AND model = %s AND dimensions = %s AND content_hash = %s;
            """,
            (article_id, model, dimensions, content_hash),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("Failed to locate archive embedding after conflict")
        return int(row[0])

    async def search_similar(
        self,
        conn: psycopg.AsyncConnection,
        *,
        query_vector: list[float],
        model: str,
        dimensions: int,
        edition_slug: str = "berdyansk",
        before_date: dt.datetime | None = None,
        max_distance: float = 0.55,
        limit: int = 3,
    ) -> list[ArchiveMatchCandidate]:
        """Search historical archive articles by cosine similarity with temporal filtering."""
        params: list[Any] = [
            Vector(query_vector),
            model,
            dimensions,
            edition_slug,
        ]

        temporal_clause = ""
        if before_date is not None:
            temporal_clause = "AND a.published_at < %s"
            params.append(before_date)

        params.extend([max_distance, limit])

        query = f"""
            SELECT
                a.id,
                a.title,
                a.content,
                a.published_at,
                a.source_name,
                a.source_url,
                (e.embedding <=> %s) AS distance
            FROM archive_embeddings e
            JOIN archive_articles a ON a.id = e.article_id
            WHERE e.model = %s
              AND e.dimensions = %s
              AND a.edition_slug = %s
              {temporal_clause}
              AND (e.embedding <=> %s) <= %s
            ORDER BY distance ASC
            LIMIT %s;
        """
        # Note: first %s is query_vector, second %s in ORDER/filter is the same query_vector
        # Build exact arguments:
        args = [
            Vector(query_vector),
            model,
            dimensions,
            edition_slug,
        ]
        if before_date is not None:
            args.append(before_date)
        args.extend([Vector(query_vector), max_distance, limit])

        cursor = await conn.execute(query, args)
        rows = await cursor.fetchall()
        return [
            ArchiveMatchCandidate(
                article_id=int(r[0]),
                title=str(r[1]),
                content=str(r[2]),
                published_at=r[3],
                source_name=str(r[4]),
                source_url=str(r[5]) if r[5] is not None else None,
                vector_distance=float(r[6]),
            )
            for r in rows
        ]

    async def get_unembedded_articles(
        self,
        conn: psycopg.AsyncConnection,
        *,
        model: str,
        dimensions: int,
        limit: int = 100,
    ) -> list[tuple[int, str, str]]:
        """Find articles missing embeddings for the specified model and dimension space."""
        cursor = await conn.execute(
            """
            SELECT a.id, a.title, a.content
            FROM archive_articles a
            LEFT JOIN archive_embeddings e
                ON a.id = e.article_id AND e.model = %s AND e.dimensions = %s
            WHERE e.id IS NULL
            ORDER BY a.published_at DESC
            LIMIT %s;
            """,
            (model, dimensions, limit),
        )
        rows = await cursor.fetchall()
        return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]
