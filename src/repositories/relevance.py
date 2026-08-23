"""Relevance repository: explicit SQL over psycopg async connections.

Repositories never commit and never open their own connections; the caller
owns transaction boundaries. Policy versions and decisions are immutable;
the only UPDATE path here is the editions.current_relevance_policy_id
pointer (a documented exception).
"""

from __future__ import annotations

import psycopg

from src.domain.claims import (
    EditionRelevanceDecision,
    RelevancePolicyVersion,
    VisionPolicyVersion,
)


class RelevancePolicyVersionRepository:
    """Persistence for `relevance_policy_versions` and the edition pointer."""

    async def insert(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        version: int,
        config_hash: str,
        prompt_version: str,
    ) -> RelevancePolicyVersion:
        cursor = await conn.execute(
            """
            INSERT INTO relevance_policy_versions (
                edition_id, version, config_hash, prompt_version
            )
            VALUES (%s, %s, %s, %s)
            RETURNING id, edition_id, version, config_hash, prompt_version, created_at
            """,
            (edition_id, version, config_hash, prompt_version),
        )
        return RelevancePolicyVersion.from_row(await cursor.fetchone())

    async def get(
        self, conn: psycopg.AsyncConnection, policy_id: int
    ) -> RelevancePolicyVersion | None:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM relevance_policy_versions WHERE id = %s
            """,
            (policy_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else RelevancePolicyVersion.from_row(row)

    async def get_current(
        self, conn: psycopg.AsyncConnection, edition_id: int
    ) -> RelevancePolicyVersion | None:
        """Read the policy the edition's current_relevance_policy_id points at."""
        cursor = await conn.execute(
            """
            SELECT p.id, p.edition_id, p.version, p.config_hash, p.prompt_version,
                   p.created_at
            FROM relevance_policy_versions p
            JOIN editions e ON e.current_relevance_policy_id = p.id
            WHERE e.id = %s
            """,
            (edition_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else RelevancePolicyVersion.from_row(row)

    async def set_current(
        self, conn: psycopg.AsyncConnection, *, edition_id: int, policy_id: int
    ) -> None:
        """Point an edition at its current relevance policy.

        The composite FK (current_relevance_policy_id, id) makes any
        cross-edition pointer impossible. Documented mutable exception.
        """
        await conn.execute(
            "UPDATE editions SET current_relevance_policy_id = %s WHERE id = %s",
            (policy_id, edition_id),
        )

    async def clear_current(self, conn: psycopg.AsyncConnection, *, edition_id: int) -> None:
        await conn.execute(
            "UPDATE editions SET current_relevance_policy_id = NULL WHERE id = %s",
            (edition_id,),
        )

    async def list_for_edition(
        self, conn: psycopg.AsyncConnection, edition_id: int
    ) -> list[RelevancePolicyVersion]:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM relevance_policy_versions WHERE edition_id = %s ORDER BY version
            """,
            (edition_id,),
        )
        rows = await cursor.fetchall()
        return [RelevancePolicyVersion.from_row(row) for row in rows]


class EditionRelevanceDecisionRepository:
    """Persistence for immutable `edition_relevance_decisions` rows."""

    async def insert_root(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_item_revision_id: int,
        edition_id: int,
        relevance_policy_id: int,
        status: str,
        confidence: float | None,
        reason: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> EditionRelevanceDecision:
        return await self._insert(
            conn,
            source_item_revision_id=source_item_revision_id,
            edition_id=edition_id,
            relevance_policy_id=relevance_policy_id,
            status=status,
            confidence=confidence,
            reason=reason,
            provider=provider,
            model=model,
            parent_decision_id=None,
        )

    async def insert_child(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_item_revision_id: int,
        edition_id: int,
        relevance_policy_id: int,
        status: str,
        confidence: float | None,
        reason: str,
        provider: str | None = None,
        model: str | None = None,
        parent_decision_id: int,
    ) -> EditionRelevanceDecision:
        """A post-vision decision: new immutable child of the prior decision."""
        return await self._insert(
            conn,
            source_item_revision_id=source_item_revision_id,
            edition_id=edition_id,
            relevance_policy_id=relevance_policy_id,
            status=status,
            confidence=confidence,
            reason=reason,
            provider=provider,
            model=model,
            parent_decision_id=parent_decision_id,
        )

    async def get(
        self, conn: psycopg.AsyncConnection, decision_id: int
    ) -> EditionRelevanceDecision | None:
        cursor = await conn.execute(
            """
            SELECT id, source_item_revision_id, edition_id, relevance_policy_id,
                   status, confidence, reason, provider, model, parent_decision_id,
                   created_at
            FROM edition_relevance_decisions WHERE id = %s
            """,
            (decision_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else EditionRelevanceDecision.from_row(row)

    async def get_root(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_item_revision_id: int,
        edition_id: int,
        relevance_policy_id: int,
    ) -> EditionRelevanceDecision | None:
        """The canonical root decision for the exact triple; duplicate-execution
        executions converge on this row instead of inserting a second verdict."""
        cursor = await conn.execute(
            """
            SELECT id, source_item_revision_id, edition_id, relevance_policy_id,
                   status, confidence, reason, provider, model, parent_decision_id,
                   created_at
            FROM edition_relevance_decisions
            WHERE source_item_revision_id = %s AND edition_id = %s
              AND relevance_policy_id = %s AND parent_decision_id IS NULL
            """,
            (source_item_revision_id, edition_id, relevance_policy_id),
        )
        row = await cursor.fetchone()
        return None if row is None else EditionRelevanceDecision.from_row(row)

    async def list_revision_ids_missing_root(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        relevance_policy_id: int,
        after_revision_id: int | None = None,
        limit: int = 500,
    ) -> list[int]:
        """Revisions bound to the edition that lack a ROOT decision for the
        exact policy, in stable id order, bounded by ``limit`` and optionally
        by an exclusive id cursor for chunked backfills."""
        cursor = await conn.execute(
            """
            SELECT r.id
            FROM source_item_revisions r
            JOIN source_items i ON i.id = r.source_item_id
            JOIN source_editions se ON se.source_id = i.source_id
            WHERE se.edition_id = %s
              AND r.id > COALESCE(%s, 0)
              AND NOT EXISTS (
                  SELECT 1
                  FROM edition_relevance_decisions d
                  WHERE d.source_item_revision_id = r.id
                    AND d.edition_id = se.edition_id
                    AND d.relevance_policy_id = %s
                    AND d.parent_decision_id IS NULL
              )
            ORDER BY r.id
            LIMIT %s
            """,
            (edition_id, after_revision_id, relevance_policy_id, limit),
        )
        rows = await cursor.fetchall()
        return [int(row[0]) for row in rows]

    async def latest_for_revision_edition(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_item_revision_id: int,
        edition_id: int,
    ) -> EditionRelevanceDecision | None:
        """Newest decision (root or child) for the exact revision + edition.

        Identity ids are monotonic, so id order is creation order.
        """
        cursor = await conn.execute(
            """
            SELECT id, source_item_revision_id, edition_id, relevance_policy_id,
                   status, confidence, reason, provider, model, parent_decision_id,
                   created_at
            FROM edition_relevance_decisions
            WHERE source_item_revision_id = %s AND edition_id = %s
            ORDER BY id DESC LIMIT 1
            """,
            (source_item_revision_id, edition_id),
        )
        row = await cursor.fetchone()
        return None if row is None else EditionRelevanceDecision.from_row(row)

    async def _insert(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_item_revision_id: int,
        edition_id: int,
        relevance_policy_id: int,
        status: str,
        confidence: float | None,
        reason: str,
        provider: str | None,
        model: str | None,
        parent_decision_id: int | None,
    ) -> EditionRelevanceDecision:
        cursor = await conn.execute(
            """
            INSERT INTO edition_relevance_decisions (
                source_item_revision_id, edition_id, relevance_policy_id,
                status, confidence, reason, provider, model, parent_decision_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, source_item_revision_id, edition_id, relevance_policy_id,
                status, confidence, reason, provider, model, parent_decision_id,
                created_at
            """,
            (
                source_item_revision_id,
                edition_id,
                relevance_policy_id,
                status,
                confidence,
                reason,
                provider,
                model,
                parent_decision_id,
            ),
        )
        return EditionRelevanceDecision.from_row(await cursor.fetchone())


class VisionPolicyRepository:
    """Minimal persistence for `vision_policy_versions` (service lands in Task 3)."""

    async def insert(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        version: int,
        mode: str,
        config_hash: str,
        prompt_version: str,
    ) -> VisionPolicyVersion:
        cursor = await conn.execute(
            """
            INSERT INTO vision_policy_versions (
                edition_id, version, mode, config_hash, prompt_version
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, edition_id, version, mode, config_hash, prompt_version,
                created_at
            """,
            (edition_id, version, mode, config_hash, prompt_version),
        )
        return VisionPolicyVersion.from_row(await cursor.fetchone())

    async def get(
        self, conn: psycopg.AsyncConnection, policy_id: int
    ) -> VisionPolicyVersion | None:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, mode, config_hash, prompt_version, created_at
            FROM vision_policy_versions WHERE id = %s
            """,
            (policy_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else VisionPolicyVersion.from_row(row)
