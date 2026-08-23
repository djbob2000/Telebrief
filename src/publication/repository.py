"""Persistence repository for publication runs, frozen snapshots, publications, and deliveries."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from src.publication.models import (
    DeliveryDestination,
    EditorialSelectionPolicyVersion,
    EligibilityPolicyVersion,
    Publication,
    PublicationCandidate,
    PublicationDelivery,
    PublicationDeliveryAttempt,
    PublicationDeliveryPayload,
    PublicationGenerationAttempt,
    PublicationInput,
    PublicationPolicySet,
    PublicationRun,
    PublicationSelectionDecision,
    WriterPolicyVersion,
)

logger = logging.getLogger(__name__)


class PublicationPolicyRepository:
    """Repository for eligibility, selection, and writer policy versions."""

    async def get_or_create_eligibility_policy(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        config_hash: str,
        prompt_version: str,
    ) -> EligibilityPolicyVersion:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM eligibility_policy_versions
            WHERE edition_id = %s AND config_hash = %s AND prompt_version = %s
            ORDER BY version DESC LIMIT 1
            """,
            (edition_id, config_hash, prompt_version),
        )
        row = await cursor.fetchone()
        if row is not None:
            return EligibilityPolicyVersion.from_row(row)

        cursor = await conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM eligibility_policy_versions WHERE edition_id = %s",
            (edition_id,),
        )
        next_ver = (await cursor.fetchone())[0]

        cursor = await conn.execute(
            """
            INSERT INTO eligibility_policy_versions (edition_id, version, config_hash, prompt_version)
            VALUES (%s, %s, %s, %s)
            RETURNING id, edition_id, version, config_hash, prompt_version, created_at
            """,
            (edition_id, next_ver, config_hash, prompt_version),
        )
        return EligibilityPolicyVersion.from_row(await cursor.fetchone())

    async def get_or_create_selection_policy(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        config_hash: str,
        prompt_version: str,
    ) -> EditorialSelectionPolicyVersion:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM editorial_selection_policy_versions
            WHERE edition_id = %s AND config_hash = %s AND prompt_version = %s
            ORDER BY version DESC LIMIT 1
            """,
            (edition_id, config_hash, prompt_version),
        )
        row = await cursor.fetchone()
        if row is not None:
            return EditorialSelectionPolicyVersion.from_row(row)

        cursor = await conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM editorial_selection_policy_versions WHERE edition_id = %s",
            (edition_id,),
        )
        next_ver = (await cursor.fetchone())[0]

        cursor = await conn.execute(
            """
            INSERT INTO editorial_selection_policy_versions (edition_id, version, config_hash, prompt_version)
            VALUES (%s, %s, %s, %s)
            RETURNING id, edition_id, version, config_hash, prompt_version, created_at
            """,
            (edition_id, next_ver, config_hash, prompt_version),
        )
        return EditorialSelectionPolicyVersion.from_row(await cursor.fetchone())

    async def get_or_create_writer_policy(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        config_hash: str,
        prompt_version: str,
    ) -> WriterPolicyVersion:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM writer_policy_versions
            WHERE edition_id = %s AND config_hash = %s AND prompt_version = %s
            ORDER BY version DESC LIMIT 1
            """,
            (edition_id, config_hash, prompt_version),
        )
        row = await cursor.fetchone()
        if row is not None:
            return WriterPolicyVersion.from_row(row)

        cursor = await conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM writer_policy_versions WHERE edition_id = %s",
            (edition_id,),
        )
        next_ver = (await cursor.fetchone())[0]

        cursor = await conn.execute(
            """
            INSERT INTO writer_policy_versions (edition_id, version, config_hash, prompt_version)
            VALUES (%s, %s, %s, %s)
            RETURNING id, edition_id, version, config_hash, prompt_version, created_at
            """,
            (edition_id, next_ver, config_hash, prompt_version),
        )
        return WriterPolicyVersion.from_row(await cursor.fetchone())


class PublicationRepository:
    """Repository for publication runs, candidates, inputs, attempts, and publications."""

    async def get_or_create_run(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        publication_type: str,
        request_key: str,
        snapshot_at: dt.datetime,
        policy_ids: PublicationPolicySet | tuple[int, int, int],
        metadata: dict[str, Any] | None = None,
    ) -> PublicationRun:
        if isinstance(policy_ids, PublicationPolicySet):
            eligibility_id = policy_ids.eligibility_policy_id
            selection_id = policy_ids.selection_policy_id
            writer_id = policy_ids.writer_policy_id
        else:
            eligibility_id, selection_id, writer_id = policy_ids

        cursor = await conn.execute(
            """
            SELECT id, edition_id, publication_type, request_key, snapshot_at,
                   eligibility_policy_id, selection_policy_id, writer_policy_id,
                   status, error_kind, metadata, created_at, completed_at
            FROM publication_runs
            WHERE request_key = %s
            """,
            (request_key,),
        )
        row = await cursor.fetchone()
        if row is not None:
            return PublicationRun.from_row(row)

        cursor = await conn.execute(
            """
            INSERT INTO publication_runs (
                edition_id, publication_type, request_key, snapshot_at,
                eligibility_policy_id, selection_policy_id, writer_policy_id,
                metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, edition_id, publication_type, request_key, snapshot_at,
                      eligibility_policy_id, selection_policy_id, writer_policy_id,
                      status, error_kind, metadata, created_at, completed_at
            """,
            (
                edition_id,
                publication_type,
                request_key,
                snapshot_at,
                eligibility_id,
                selection_id,
                writer_id,
                Jsonb(metadata or {}),
            ),
        )
        return PublicationRun.from_row(await cursor.fetchone())

    async def get_run_by_id(
        self, conn: psycopg.AsyncConnection, run_id: int
    ) -> PublicationRun | None:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, publication_type, request_key, snapshot_at,
                   eligibility_policy_id, selection_policy_id, writer_policy_id,
                   status, error_kind, metadata, created_at, completed_at
            FROM publication_runs
            WHERE id = %s
            """,
            (run_id,),
        )
        row = await cursor.fetchone()
        return PublicationRun.from_row(row) if row is not None else None

    async def lock_run(self, conn: psycopg.AsyncConnection, run_id: int) -> PublicationRun | None:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, publication_type, request_key, snapshot_at,
                   eligibility_policy_id, selection_policy_id, writer_policy_id,
                   status, error_kind, metadata, created_at, completed_at
            FROM publication_runs
            WHERE id = %s
            FOR UPDATE
            """,
            (run_id,),
        )
        row = await cursor.fetchone()
        return PublicationRun.from_row(row) if row is not None else None

    async def transition_run(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        new_status: str,
        *,
        error_kind: str | None = None,
        completed_at: dt.datetime | None = None,
    ) -> None:
        await conn.execute(
            """
            UPDATE publication_runs
            SET status = %s,
                error_kind = COALESCE(%s, error_kind),
                completed_at = COALESCE(%s, completed_at)
            WHERE id = %s
            """,
            (new_status, error_kind, completed_at, run_id),
        )

    async def eligible_story_revisions(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        snapshot_at: dt.datetime,
        eligibility_policy_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query stories and their latest revision visible at snapshot_at."""
        cursor = await conn.execute(
            """
            WITH latest_revs AS (
                SELECT DISTINCT ON (sr.story_id)
                    sr.story_id,
                    sr.id AS story_revision_id,
                    sr.revision_no,
                    sr.current_state,
                    sr.semantic_text,
                    sr.created_at AS revision_created_at
                FROM story_revisions sr
                JOIN stories s ON s.id = sr.story_id
                WHERE s.edition_id = %s
                  AND sr.created_at <= %s
                ORDER BY sr.story_id, sr.revision_no DESC, sr.created_at DESC
            )
            SELECT
                lr.story_id,
                lr.story_revision_id,
                lr.revision_no,
                lr.current_state,
                lr.semantic_text,
                lr.revision_created_at,
                COALESCE(
                    (
                        SELECT count(DISTINCT sc.claim_id)
                        FROM story_claims sc
                        JOIN claims c ON c.id = sc.claim_id
                        WHERE sc.story_id = lr.story_id
                          AND sc.attached_at <= %s
                          AND c.created_at <= %s
                    ), 0
                ) AS claim_count,
                COALESCE(
                    (
                        SELECT count(DISTINCT sir.source_item_id)
                        FROM story_claims sc
                        JOIN claims c ON c.id = sc.claim_id
                        JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                        WHERE sc.story_id = lr.story_id
                          AND sc.attached_at <= %s
                          AND c.created_at <= %s
                    ), 0
                ) AS source_count
            FROM latest_revs lr
            WHERE lr.current_state NOT IN ('invalid', 'archived', 'rejected')
            ORDER BY lr.revision_created_at DESC, lr.story_id ASC
            """,
            (edition_id, snapshot_at, snapshot_at, snapshot_at, snapshot_at, snapshot_at),
        )
        rows = await cursor.fetchall()
        candidates = []
        for r in rows:
            candidates.append(
                {
                    "story_id": r[0],
                    "story_revision_id": r[1],
                    "revision_no": r[2],
                    "current_state": r[3],
                    "semantic_text": r[4],
                    "created_at": r[5],
                    "claim_count": r[6],
                    "source_count": r[7],
                    "snapshot_features": {
                        "claim_count": r[6],
                        "source_count": r[7],
                        "semantic_text": r[4],
                    },
                }
            )
        return candidates

    async def insert_candidate(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        *,
        story_id: int,
        story_revision_id: int,
        deterministic_rank: int,
        snapshot_features: dict[str, Any] | None = None,
    ) -> PublicationCandidate:
        cursor = await conn.execute(
            """
            INSERT INTO publication_candidates (
                publication_run_id, story_id, story_revision_id,
                deterministic_rank, snapshot_features
            ) VALUES (%s, %s, %s, %s, %s)
            RETURNING id, publication_run_id, story_id, story_revision_id,
                      deterministic_rank, snapshot_features, created_at
            """,
            (
                run_id,
                story_id,
                story_revision_id,
                deterministic_rank,
                Jsonb(snapshot_features or {}),
            ),
        )
        return PublicationCandidate.from_row(await cursor.fetchone())

    async def load_sealed_candidates(
        self, conn: psycopg.AsyncConnection, run_id: int
    ) -> list[PublicationCandidate]:
        cursor = await conn.execute(
            """
            SELECT id, publication_run_id, story_id, story_revision_id,
                   deterministic_rank, snapshot_features, created_at
            FROM publication_candidates
            WHERE publication_run_id = %s
            ORDER BY deterministic_rank ASC
            """,
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [PublicationCandidate.from_row(row) for row in rows]

    async def insert_selection_decision(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        decision: PublicationSelectionDecision,
    ) -> PublicationSelectionDecision:
        cursor = await conn.execute(
            """
            INSERT INTO publication_selection_decisions (
                publication_run_id, candidate_id, decision,
                presentation_intent, confidence, reason, rank, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, publication_run_id, candidate_id, decision,
                      presentation_intent, confidence, reason, rank, metadata, created_at
            """,
            (
                run_id,
                decision.candidate_id,
                decision.decision,
                decision.presentation_intent,
                decision.confidence,
                decision.reason,
                decision.rank,
                Jsonb(decision.metadata or {}),
            ),
        )
        return PublicationSelectionDecision.from_row(await cursor.fetchone())

    async def freeze_selected_input(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        *,
        story_id: int,
        story_revision_id: int,
        selection_decision_id: int,
        presentation_intent: str | None = None,
        rank: int = 1,
        claim_ids: list[int] | None = None,
        evidence_cluster_ids: list[int] | None = None,
    ) -> PublicationInput:
        cursor = await conn.execute(
            """
            INSERT INTO publication_inputs (
                publication_run_id, story_id, story_revision_id,
                selection_decision_id, presentation_intent, rank
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, publication_run_id, story_id, story_revision_id,
                      selection_decision_id, presentation_intent, rank, created_at
            """,
            (
                run_id,
                story_id,
                story_revision_id,
                selection_decision_id,
                presentation_intent,
                rank,
            ),
        )
        input_row = PublicationInput.from_row(await cursor.fetchone())

        if claim_ids:
            cur = conn.cursor()
            await cur.executemany(
                """
                INSERT INTO publication_input_claims (publication_input_id, claim_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
                """,
                [(input_row.id, cid) for cid in claim_ids],
            )

        if evidence_cluster_ids:
            cur = conn.cursor()
            await cur.executemany(
                """
                INSERT INTO publication_input_evidence_clusters (publication_input_id, evidence_cluster_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
                """,
                [(input_row.id, ecid) for ecid in evidence_cluster_ids],
            )

        return PublicationInput(
            id=input_row.id,
            publication_run_id=input_row.publication_run_id,
            story_id=input_row.story_id,
            story_revision_id=input_row.story_revision_id,
            selection_decision_id=input_row.selection_decision_id,
            presentation_intent=input_row.presentation_intent,
            rank=input_row.rank,
            created_at=input_row.created_at,
            claim_ids=claim_ids or [],
            evidence_cluster_ids=evidence_cluster_ids or [],
        )

    async def load_sealed_inputs(
        self, conn: psycopg.AsyncConnection, run_id: int
    ) -> list[PublicationInput]:
        cursor = await conn.execute(
            """
            SELECT id, publication_run_id, story_id, story_revision_id,
                   selection_decision_id, presentation_intent, rank, created_at
            FROM publication_inputs
            WHERE publication_run_id = %s
            ORDER BY rank ASC
            """,
            (run_id,),
        )
        inputs: list[PublicationInput] = []
        for row in await cursor.fetchall():
            inp = PublicationInput.from_row(row)
            # load claims
            c_cur = await conn.execute(
                "SELECT claim_id FROM publication_input_claims WHERE publication_input_id = %s ORDER BY claim_id ASC",
                (inp.id,),
            )
            c_ids = [r[0] for r in await c_cur.fetchall()]
            # load clusters
            ec_cur = await conn.execute(
                "SELECT evidence_cluster_id FROM publication_input_evidence_clusters WHERE publication_input_id = %s ORDER BY evidence_cluster_id ASC",
                (inp.id,),
            )
            ec_ids = [r[0] for r in await ec_cur.fetchall()]
            inputs.append(
                PublicationInput(
                    id=inp.id,
                    publication_run_id=inp.publication_run_id,
                    story_id=inp.story_id,
                    story_revision_id=inp.story_revision_id,
                    selection_decision_id=inp.selection_decision_id,
                    presentation_intent=inp.presentation_intent,
                    rank=inp.rank,
                    created_at=inp.created_at,
                    claim_ids=c_ids,
                    evidence_cluster_ids=ec_ids,
                )
            )
        return inputs

    async def insert_generation_attempt(
        self,
        conn: psycopg.AsyncConnection,
        *,
        run_id: int,
        attempt_no: int,
        kind: str,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PublicationGenerationAttempt:
        cursor = await conn.execute(
            """
            INSERT INTO publication_generation_attempts (
                publication_run_id, attempt_no, kind, provider, model, prompt_hash, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, publication_run_id, attempt_no, kind, status, error_kind,
                      provider, model, prompt_hash, metadata, started_at, completed_at
            """,
            (
                run_id,
                attempt_no,
                kind,
                provider,
                model,
                prompt_hash,
                Jsonb(metadata or {}),
            ),
        )
        return PublicationGenerationAttempt.from_row(await cursor.fetchone())

    async def update_generation_attempt(
        self,
        conn: psycopg.AsyncConnection,
        attempt_id: int,
        *,
        status: str,
        error_kind: str | None = None,
        metadata: dict[str, Any] | None = None,
        completed_at: dt.datetime | None = None,
    ) -> None:
        await conn.execute(
            """
            UPDATE publication_generation_attempts
            SET status = %s,
                error_kind = %s,
                metadata = CASE WHEN %s::jsonb IS NOT NULL THEN metadata || %s::jsonb ELSE metadata END,
                completed_at = COALESCE(%s, now())
            WHERE id = %s
            """,
            (
                status,
                error_kind,
                Jsonb(metadata) if metadata is not None else None,
                Jsonb(metadata) if metadata is not None else None,
                completed_at,
                attempt_id,
            ),
        )

    async def create_publication(
        self,
        conn: psycopg.AsyncConnection,
        *,
        run_id: int,
        winning_attempt_id: int,
        publication_type: str,
        title: str,
        lead: str | None,
        body: str,
        metadata: dict[str, Any] | None = None,
    ) -> Publication:
        cursor = await conn.execute(
            """
            INSERT INTO publications (
                publication_run_id, winning_generation_attempt_id,
                publication_type, title, lead, body, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, publication_run_id, winning_generation_attempt_id,
                      publication_type, title, lead, body, metadata, created_at
            """,
            (
                run_id,
                winning_attempt_id,
                publication_type,
                title,
                lead,
                body,
                Jsonb(metadata or {}),
            ),
        )
        return Publication.from_row(await cursor.fetchone())

    async def get_publication_by_run_id(
        self, conn: psycopg.AsyncConnection, run_id: int
    ) -> Publication | None:
        cursor = await conn.execute(
            """
            SELECT id, publication_run_id, winning_generation_attempt_id,
                   publication_type, title, lead, body, metadata, created_at
            FROM publications
            WHERE publication_run_id = %s
            """,
            (run_id,),
        )
        row = await cursor.fetchone()
        return Publication.from_row(row) if row is not None else None

    async def get_publication_by_id(
        self, conn: psycopg.AsyncConnection, pub_id: int
    ) -> Publication | None:
        cursor = await conn.execute(
            """
            SELECT id, publication_run_id, winning_generation_attempt_id,
                   publication_type, title, lead, body, metadata, created_at
            FROM publications
            WHERE id = %s
            """,
            (pub_id,),
        )
        row = await cursor.fetchone()
        return Publication.from_row(row) if row is not None else None

    async def get_latest_publication(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        publication_type: str,
    ) -> Publication | None:
        cursor = await conn.execute(
            """
            SELECT p.id, p.publication_run_id, p.winning_generation_attempt_id,
                   p.publication_type, p.title, p.lead, p.body, p.metadata, p.created_at
            FROM publications p
            JOIN publication_runs pr ON p.publication_run_id = pr.id
            WHERE pr.edition_id = %s AND p.publication_type = %s
            ORDER BY p.created_at DESC
            LIMIT 1
            """,
            (edition_id, publication_type),
        )
        row = await cursor.fetchone()
        return Publication.from_row(row) if row is not None else None


class DeliveryRepository:
    """Repository for destinations, payloads, deliveries, and delivery attempts."""

    async def get_or_create_destination(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        platform: str,
        destination_key: str,
        config: dict[str, Any] | None = None,
    ) -> DeliveryDestination:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, platform, destination_key, config, is_active, created_at
            FROM delivery_destinations
            WHERE edition_id = %s AND platform = %s AND destination_key = %s
            """,
            (edition_id, platform, destination_key),
        )
        row = await cursor.fetchone()
        if row is not None:
            return DeliveryDestination.from_row(row)

        cursor = await conn.execute(
            """
            INSERT INTO delivery_destinations (edition_id, platform, destination_key, config)
            VALUES (%s, %s, %s, %s)
            RETURNING id, edition_id, platform, destination_key, config, is_active, created_at
            """,
            (edition_id, platform, destination_key, Jsonb(config or {})),
        )
        return DeliveryDestination.from_row(await cursor.fetchone())

    async def get_destination_by_id(
        self, conn: psycopg.AsyncConnection, destination_id: int
    ) -> DeliveryDestination | None:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, platform, destination_key, config, is_active, created_at
            FROM delivery_destinations
            WHERE id = %s
            """,
            (destination_id,),
        )
        row = await cursor.fetchone()
        return DeliveryDestination.from_row(row) if row is not None else None

    async def list_active_destinations(
        self, conn: psycopg.AsyncConnection, edition_id: int
    ) -> list[DeliveryDestination]:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, platform, destination_key, config, is_active, created_at
            FROM delivery_destinations
            WHERE edition_id = %s AND is_active = true
            ORDER BY id ASC
            """,
            (edition_id,),
        )
        return [DeliveryDestination.from_row(row) for row in await cursor.fetchall()]

    async def create_payload(
        self,
        conn: psycopg.AsyncConnection,
        *,
        publication_id: int,
        destination_id: int,
        payload_format: str,
        rendered_content: dict[str, Any],
        content_hash: str,
    ) -> PublicationDeliveryPayload:
        cursor = await conn.execute(
            """
            INSERT INTO publication_delivery_payloads (
                publication_id, destination_id, payload_format, rendered_content, content_hash
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (publication_id, destination_id) DO UPDATE SET
                payload_format = EXCLUDED.payload_format,
                rendered_content = EXCLUDED.rendered_content,
                content_hash = EXCLUDED.content_hash
            RETURNING id, publication_id, destination_id, payload_format,
                      rendered_content, content_hash, created_at
            """,
            (
                publication_id,
                destination_id,
                payload_format,
                Jsonb(rendered_content),
                content_hash,
            ),
        )
        return PublicationDeliveryPayload.from_row(await cursor.fetchone())

    async def get_payload(
        self, conn: psycopg.AsyncConnection, payload_id: int
    ) -> PublicationDeliveryPayload | None:
        cursor = await conn.execute(
            """
            SELECT id, publication_id, destination_id, payload_format,
                   rendered_content, content_hash, created_at
            FROM publication_delivery_payloads
            WHERE id = %s
            """,
            (payload_id,),
        )
        row = await cursor.fetchone()
        return PublicationDeliveryPayload.from_row(row) if row is not None else None

    async def create_delivery(
        self,
        conn: psycopg.AsyncConnection,
        *,
        publication_id: int,
        destination_id: int,
        payload_id: int,
        idempotency_key: str,
    ) -> PublicationDelivery:
        cursor = await conn.execute(
            """
            INSERT INTO publication_deliveries (
                publication_id, destination_id, payload_id, idempotency_key
            ) VALUES (%s, %s, %s, %s)
            RETURNING id, publication_id, destination_id, payload_id,
                      idempotency_key, status, external_delivery_id, metadata,
                      created_at, completed_at
            """,
            (publication_id, destination_id, payload_id, idempotency_key),
        )
        return PublicationDelivery.from_row(await cursor.fetchone())

    async def get_delivery_by_id(
        self, conn: psycopg.AsyncConnection, delivery_id: int
    ) -> PublicationDelivery | None:
        cursor = await conn.execute(
            """
            SELECT id, publication_id, destination_id, payload_id,
                   idempotency_key, status, external_delivery_id, metadata,
                   created_at, completed_at
            FROM publication_deliveries
            WHERE id = %s
            """,
            (delivery_id,),
        )
        row = await cursor.fetchone()
        return PublicationDelivery.from_row(row) if row is not None else None

    async def record_delivery_attempt(
        self,
        conn: psycopg.AsyncConnection,
        *,
        delivery_id: int,
        attempt_no: int,
        status: str,
        error_kind: str | None = None,
        error_message: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> PublicationDeliveryAttempt:
        cursor = await conn.execute(
            """
            INSERT INTO publication_delivery_attempts (
                publication_delivery_id, attempt_no, status,
                error_kind, error_message, response, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, now())
            RETURNING id, publication_delivery_id, attempt_no, status,
                      error_kind, error_message, response, started_at, completed_at
            """,
            (
                delivery_id,
                attempt_no,
                status,
                error_kind,
                error_message,
                Jsonb(response or {}),
            ),
        )
        return PublicationDeliveryAttempt.from_row(await cursor.fetchone())

    async def update_delivery_status(
        self,
        conn: psycopg.AsyncConnection,
        delivery_id: int,
        *,
        status: str,
        external_delivery_id: str | None = None,
    ) -> None:
        await conn.execute(
            """
            UPDATE publication_deliveries
            SET status = %s,
                external_delivery_id = COALESCE(%s, external_delivery_id),
                completed_at = CASE WHEN %s IN ('succeeded', 'failed') THEN now() ELSE completed_at END
            WHERE id = %s
            """,
            (status, external_delivery_id, status, delivery_id),
        )
