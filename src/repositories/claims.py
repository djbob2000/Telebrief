"""Claim repository: explicit SQL over psycopg async connections.

Repositories never commit and never open their own connections; the caller
owns transaction boundaries. Claims, relations, and state events are
immutable; the only UPDATE paths here are run status transitions and attempt
completion (both documented exceptions).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import psycopg
from psycopg.types.json import Jsonb

from src.domain.claims import (
    Claim,
    ClaimExtractionPolicyVersion,
    ClaimExtractionRun,
    ClaimRelation,
    ClaimStateEvent,
    EditionRelevanceDecision,
    NewClaim,
    ProcessingAttempt,
)


class ClaimExtractionPolicyRepository:
    """Persistence for `claim_extraction_policy_versions`."""

    async def insert(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        version: int,
        config_hash: str,
        prompt_version: str,
    ) -> ClaimExtractionPolicyVersion:
        cursor = await conn.execute(
            """
            INSERT INTO claim_extraction_policy_versions (
                edition_id, version, config_hash, prompt_version
            )
            VALUES (%s, %s, %s, %s)
            RETURNING id, edition_id, version, config_hash, prompt_version, created_at
            """,
            (edition_id, version, config_hash, prompt_version),
        )
        return ClaimExtractionPolicyVersion.from_row(await cursor.fetchone())

    async def get(
        self, conn: psycopg.AsyncConnection, policy_id: int
    ) -> ClaimExtractionPolicyVersion | None:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM claim_extraction_policy_versions WHERE id = %s
            """,
            (policy_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else ClaimExtractionPolicyVersion.from_row(row)

    async def list_for_edition(
        self, conn: psycopg.AsyncConnection, edition_id: int
    ) -> list[ClaimExtractionPolicyVersion]:
        """All policy versions of one edition in ascending version order.

        The editions table carries NO current-claim-policy pointer column, so
        "current" is resolved by identity (config_hash, prompt_version) with
        latest-version-wins semantics in the service layer.
        """
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM claim_extraction_policy_versions WHERE edition_id = %s ORDER BY version
            """,
            (edition_id,),
        )
        rows = await cursor.fetchall()
        return [ClaimExtractionPolicyVersion.from_row(row) for row in rows]


class ClaimExtractionRunRepository:
    """Persistence for canonical `claim_extraction_runs` and their attempts.

    The partial unique index uq_claim_extraction_success makes the database
    authoritative for at-most-one-success per
    (source_item_revision_id, edition_id, extraction_policy_id); the guarded
    ``mark_succeeded`` keeps the common path race-friendly but the index
    remains the backstop under concurrent writers.
    """

    async def get_or_create_run(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_item_revision_id: int,
        edition_id: int,
        extraction_policy_id: int,
        relevance_decision_id: int,
    ) -> tuple[ClaimExtractionRun, bool]:
        """Return the canonical succeeded run when one exists, else open a new
        running run. At-least-once task executions therefore converge on the
        same semantic run instead of duplicating claims."""
        existing = await self._canonical_success(
            conn,
            source_item_revision_id=source_item_revision_id,
            edition_id=edition_id,
            extraction_policy_id=extraction_policy_id,
        )
        if existing is not None:
            return existing, False

        cursor = await conn.execute(
            """
            INSERT INTO claim_extraction_runs (
                source_item_revision_id, edition_id, extraction_policy_id,
                relevance_decision_id, status
            )
            VALUES (%s, %s, %s, %s, 'running')
            RETURNING id, source_item_revision_id, edition_id, extraction_policy_id,
                relevance_decision_id, started_at, completed_at, status, error_kind,
                metadata
            """,
            (
                source_item_revision_id,
                edition_id,
                extraction_policy_id,
                relevance_decision_id,
            ),
        )
        return ClaimExtractionRun.from_row(await cursor.fetchone()), True

    async def get(self, conn: psycopg.AsyncConnection, run_id: int) -> ClaimExtractionRun | None:
        cursor = await conn.execute(
            """
            SELECT id, source_item_revision_id, edition_id, extraction_policy_id,
                   relevance_decision_id, started_at, completed_at, status,
                   error_kind, metadata
            FROM claim_extraction_runs WHERE id = %s
            """,
            (run_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else ClaimExtractionRun.from_row(row)

    async def latest_for_key(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_item_revision_id: int,
        edition_id: int,
        extraction_policy_id: int,
    ) -> ClaimExtractionRun | None:
        """Newest run for the semantic key regardless of status.

        Identity ids are monotonic, so id order is creation order. The service
        layer reuses a still-``running`` run from this query so provider
        retries keep one semantic run and only append attempt rows; succeeded
        runs are canonical (see ``_canonical_success``), failed runs never
        block a fresh chain.
        """
        cursor = await conn.execute(
            """
            SELECT id, source_item_revision_id, edition_id, extraction_policy_id,
                   relevance_decision_id, started_at, completed_at, status,
                   error_kind, metadata
            FROM claim_extraction_runs
            WHERE source_item_revision_id = %s AND edition_id = %s
              AND extraction_policy_id = %s
            ORDER BY id DESC LIMIT 1
            """,
            (source_item_revision_id, edition_id, extraction_policy_id),
        )
        row = await cursor.fetchone()
        return None if row is None else ClaimExtractionRun.from_row(row)

    async def latest_open_attempt(
        self, conn: psycopg.AsyncConnection, *, stage: str, semantic_run_id: int
    ) -> ProcessingAttempt | None:
        """Newest still-running attempt row for one semantic run, if any.

        Used by terminal fail-open finalization to close the audit history of
        an execution that died with the provider unavailable.
        """
        cursor = await conn.execute(
            """
            SELECT stage, semantic_run_id, attempt_no, provider, model,
                started_at, completed_at, status, error_kind, metadata
            FROM processing_attempts
            WHERE stage = %s AND semantic_run_id = %s AND status = 'running'
            ORDER BY attempt_no DESC LIMIT 1
            """,
            (stage, semantic_run_id),
        )
        row = await cursor.fetchone()
        return None if row is None else ProcessingAttempt.from_row(row)

    async def list_decisions_missing_success(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        extraction_policy_id: int,
        after_decision_id: int | None = None,
        limit: int = 500,
    ) -> list[EditionRelevanceDecision]:
        """Relevant decisions that still owe a SUCCESSFUL extraction for the
        exact policy (bounded slice with optional exclusive id cursor).

        Only the LATEST decision per (revision, edition) qualifies so
        superseded parents never re-run. A SUCCEEDED run satisfies the debt;
        failed or running runs do NOT — fail-open items must be retried by
        backfill, and duplicate deferrals are safe because concurrent
        executions converge on the single canonical success.
        """
        cursor = await conn.execute(
            """
            SELECT d.id, d.source_item_revision_id, d.edition_id, d.relevance_policy_id,
                   d.status, d.confidence, d.reason, d.provider, d.model,
                   d.parent_decision_id, d.created_at
            FROM edition_relevance_decisions d
            WHERE d.edition_id = %s
              AND d.status = 'relevant'
              AND d.id > COALESCE(%s, 0)
              AND d.id = (
                  SELECT MAX(latest.id)
                  FROM edition_relevance_decisions latest
                  WHERE latest.source_item_revision_id = d.source_item_revision_id
                    AND latest.edition_id = d.edition_id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM claim_extraction_runs r
                  WHERE r.relevance_decision_id = d.id
                    AND r.extraction_policy_id = %s
                    AND r.status = 'succeeded'
              )
            ORDER BY d.id
            LIMIT %s
            """,
            (edition_id, after_decision_id, extraction_policy_id, limit),
        )
        rows = await cursor.fetchall()
        return [EditionRelevanceDecision.from_row(row) for row in rows]

    async def mark_succeeded(
        self, conn: psycopg.AsyncConnection, run_id: int, *, completed_at: dt.datetime
    ) -> bool:
        """Transition one run to succeeded; False when another run already
        holds the canonical success slot for the same semantic key."""
        cursor = await conn.execute(
            """
            UPDATE claim_extraction_runs
            SET status = 'succeeded', completed_at = %s
            WHERE id = %s AND NOT EXISTS (
                SELECT 1
                FROM claim_extraction_runs other
                WHERE other.source_item_revision_id
                          = claim_extraction_runs.source_item_revision_id
                  AND other.edition_id = claim_extraction_runs.edition_id
                  AND other.extraction_policy_id
                          = claim_extraction_runs.extraction_policy_id
                  AND other.status = 'succeeded'
            )
            RETURNING id
            """,
            (completed_at, run_id),
        )
        return await cursor.fetchone() is not None

    async def mark_failed(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        *,
        error_kind: str | None,
        completed_at: dt.datetime,
    ) -> None:
        """Transition one run to failed; a failed run never occupies the
        canonical slot, so a later retry may succeed."""
        await conn.execute(
            """
            UPDATE claim_extraction_runs
            SET status = 'failed', completed_at = %s, error_kind = %s
            WHERE id = %s
            """,
            (completed_at, error_kind, run_id),
        )

    async def start_attempt(
        self,
        conn: psycopg.AsyncConnection,
        *,
        stage: str,
        semantic_run_id: int,
        provider: str | None = None,
        model: str | None = None,
    ) -> ProcessingAttempt:
        """Append one provider attempt to the audit history.

        attempt_no is computed as MAX+1 inside the caller's transaction;
        PRIMARY KEY (stage, semantic_run_id, attempt_no) is the concurrency
        backstop.
        """
        cursor = await conn.execute(
            """
            INSERT INTO processing_attempts (
                stage, semantic_run_id, attempt_no, provider, model, status
            )
            SELECT %s, %s, COALESCE(MAX(attempt_no), 0) + 1, %s, %s, 'running'
            FROM processing_attempts
            WHERE stage = %s AND semantic_run_id = %s
            RETURNING stage, semantic_run_id, attempt_no, provider, model,
                started_at, completed_at, status, error_kind, metadata
            """,
            (stage, semantic_run_id, provider, model, stage, semantic_run_id),
        )
        return ProcessingAttempt.from_row(await cursor.fetchone())

    async def finish_attempt(
        self,
        conn: psycopg.AsyncConnection,
        attempt: ProcessingAttempt,
        *,
        status: str,
        completed_at: dt.datetime,
        error_kind: str | None = None,
    ) -> None:
        """Close an audit attempt row with its outcome (documented exception)."""
        await conn.execute(
            """
            UPDATE processing_attempts
            SET status = %s, completed_at = %s, error_kind = %s
            WHERE stage = %s AND semantic_run_id = %s AND attempt_no = %s
            """,
            (
                status,
                completed_at,
                error_kind,
                attempt.stage,
                attempt.semantic_run_id,
                attempt.attempt_no,
            ),
        )

    async def _canonical_success(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_item_revision_id: int,
        edition_id: int,
        extraction_policy_id: int,
    ) -> ClaimExtractionRun | None:
        cursor = await conn.execute(
            """
            SELECT id, source_item_revision_id, edition_id, extraction_policy_id,
                   relevance_decision_id, started_at, completed_at, status,
                   error_kind, metadata
            FROM claim_extraction_runs
            WHERE source_item_revision_id = %s AND edition_id = %s
              AND extraction_policy_id = %s AND status = 'succeeded'
            """,
            (source_item_revision_id, edition_id, extraction_policy_id),
        )
        row = await cursor.fetchone()
        return None if row is None else ClaimExtractionRun.from_row(row)


class ClaimRepository:
    """Persistence for immutable `claims`, relations, and state events.

    ``insert_claims`` must only be called inside the same transaction that
    marks the extraction run succeeded, so downstream consumers observe
    claims and canonical success atomically (spec §15).
    """

    async def insert_claims(
        self,
        conn: psycopg.AsyncConnection,
        *,
        run: ClaimExtractionRun,
        claims: Sequence[NewClaim],
    ) -> list[Claim]:
        inserted: list[Claim] = []
        for claim in claims:
            cursor = await conn.execute(
                """
                INSERT INTO claims (
                    claim_extraction_run_id, source_item_revision_id, edition_id,
                    assertion_text, normalized_assertion, event_time_start,
                    event_time_end, event_time_precision, event_time_confidence,
                    event_time_original_text, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, claim_extraction_run_id, source_item_revision_id,
                    edition_id, assertion_text, normalized_assertion,
                    event_time_start, event_time_end, event_time_precision,
                    event_time_confidence, event_time_original_text, metadata,
                    created_at
                """,
                (
                    run.id,
                    run.source_item_revision_id,
                    run.edition_id,
                    claim.assertion_text,
                    claim.normalized_assertion,
                    claim.event_time_start,
                    claim.event_time_end,
                    claim.event_time_precision,
                    claim.event_time_confidence,
                    claim.event_time_original_text,
                    Jsonb(claim.metadata),
                ),
            )
            inserted.append(Claim.from_row(await cursor.fetchone()))
        return inserted

    async def list_for_run(
        self, conn: psycopg.AsyncConnection, claim_extraction_run_id: int
    ) -> list[Claim]:
        """All claims of one extraction run in insertion (id) order.

        Replay paths converge on these rows instead of re-running the model.
        """
        cursor = await conn.execute(
            """
            SELECT id, claim_extraction_run_id, source_item_revision_id,
                   edition_id, assertion_text, normalized_assertion,
                   event_time_start, event_time_end, event_time_precision,
                   event_time_confidence, event_time_original_text, metadata,
                   created_at
            FROM claims WHERE claim_extraction_run_id = %s ORDER BY id
            """,
            (claim_extraction_run_id,),
        )
        rows = await cursor.fetchall()
        return [Claim.from_row(row) for row in rows]

    async def get_many(
        self, conn: psycopg.AsyncConnection, claim_ids: Sequence[int]
    ) -> list[Claim]:
        """Fetch claims preserving the caller's id order."""
        ids = list(claim_ids)
        if not ids:
            return []
        cursor = await conn.execute(
            """
            SELECT id, claim_extraction_run_id, source_item_revision_id,
                   edition_id, assertion_text, normalized_assertion,
                   event_time_start, event_time_end, event_time_precision,
                   event_time_confidence, event_time_original_text, metadata,
                   created_at
            FROM claims
            WHERE id = ANY(%s::bigint[])
            ORDER BY array_position(%s::bigint[], id)
            """,
            (ids, ids),
        )
        rows = await cursor.fetchall()
        return [Claim.from_row(row) for row in rows]

    async def attach_relation(
        self,
        conn: psycopg.AsyncConnection,
        *,
        from_claim_id: int,
        to_claim_id: int,
        relation_type: str,
    ) -> ClaimRelation:
        cursor = await conn.execute(
            """
            INSERT INTO claim_relations (from_claim_id, to_claim_id, relation_type)
            VALUES (%s, %s, %s)
            RETURNING id, from_claim_id, to_claim_id, relation_type, created_at
            """,
            (from_claim_id, to_claim_id, relation_type),
        )
        return ClaimRelation.from_row(await cursor.fetchone())

    async def insert_state_event(
        self,
        conn: psycopg.AsyncConnection,
        *,
        claim_id: int,
        type: str,
        observed_at: dt.datetime,
        reason: str | None = None,
        evidence: dict | None = None,
    ) -> ClaimStateEvent:
        cursor = await conn.execute(
            """
            INSERT INTO claim_state_events (claim_id, type, observed_at, reason, evidence)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, claim_id, type, observed_at, reason, evidence, created_at
            """,
            (claim_id, type, observed_at, reason, Jsonb(evidence or {})),
        )
        return ClaimStateEvent.from_row(await cursor.fetchone())
