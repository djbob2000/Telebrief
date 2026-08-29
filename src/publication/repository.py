"""Persistence repository for publication runs, frozen snapshots, publications, and deliveries."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any, TypeVar

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

_PolicyT = TypeVar("_PolicyT")


class PublicationPolicyRepository:
    """Repository for eligibility, selection, and writer policy versions."""

    async def get_or_create_eligibility_policy(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        config_hash: str,
        prompt_version: str,
        config: dict[str, Any] | None = None,
    ) -> EligibilityPolicyVersion:
        return await self._get_or_create_policy(
            conn,
            table="eligibility_policy_versions",
            edition_id=edition_id,
            config_hash=config_hash,
            prompt_version=prompt_version,
            config=config,
            from_row=EligibilityPolicyVersion.from_row,
        )

    async def get_or_create_selection_policy(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        config_hash: str,
        prompt_version: str,
        config: dict[str, Any] | None = None,
    ) -> EditorialSelectionPolicyVersion:
        return await self._get_or_create_policy(
            conn,
            table="editorial_selection_policy_versions",
            edition_id=edition_id,
            config_hash=config_hash,
            prompt_version=prompt_version,
            config=config,
            from_row=EditorialSelectionPolicyVersion.from_row,
        )

    async def get_or_create_writer_policy(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        config_hash: str,
        prompt_version: str,
        config: dict[str, Any] | None = None,
    ) -> WriterPolicyVersion:
        return await self._get_or_create_policy(
            conn,
            table="writer_policy_versions",
            edition_id=edition_id,
            config_hash=config_hash,
            prompt_version=prompt_version,
            config=config,
            from_row=WriterPolicyVersion.from_row,
        )

    async def _get_or_create_policy(
        self,
        conn: psycopg.AsyncConnection,
        *,
        table: str,
        edition_id: int,
        config_hash: str,
        prompt_version: str,
        config: dict[str, Any] | None = None,
        from_row: Callable[[Any], _PolicyT],
    ) -> _PolicyT:
        """Shared get-or-create for the per-edition policy tables.

        A concurrent creator can win the INSERT race; the UniqueViolation is
        answered by re-reading and converging on its row (duplicate policies
        are harmless), bounded by a small retry budget.
        """
        if table not in (
            "eligibility_policy_versions",
            "editorial_selection_policy_versions",
            "writer_policy_versions",
        ):
            raise ValueError(f"unsupported policy table {table!r}")
        select_sql = f"""
            SELECT id, edition_id, version, config_hash, prompt_version, config, created_at
            FROM {table}
            WHERE edition_id = %s AND config_hash = %s AND prompt_version = %s
            ORDER BY version DESC LIMIT 1
        """  # noqa: S608 — table is allowlisted above; values are bound params
        for _ in range(3):
            cursor = await conn.execute(select_sql, (edition_id, config_hash, prompt_version))
            row = await cursor.fetchone()
            if row is not None:
                return from_row(row)

            cursor = await conn.execute(
                f"SELECT COALESCE(MAX(version), 0) + 1 FROM {table} WHERE edition_id = %s",  # noqa: S608
                (edition_id,),
            )
            ver_row = await cursor.fetchone()
            next_ver = ver_row[0] if ver_row is not None else 1

            try:
                insert_sql = f"""
                    INSERT INTO {table} (edition_id, version, config_hash, prompt_version, config)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, edition_id, version, config_hash, prompt_version, config, created_at
                    """  # noqa: S608 — table is allowlisted above; values are bound params
                cursor = await conn.execute(
                    insert_sql,
                    (
                        edition_id,
                        next_ver,
                        config_hash,
                        prompt_version,
                        Jsonb(config or {}),
                    ),
                )
                res_row = await cursor.fetchone()
                if res_row is None:
                    raise RuntimeError(f"Failed to insert {table} row")
                return from_row(res_row)
            except psycopg.errors.UniqueViolation:
                continue
        raise RuntimeError(f"could not resolve current policy in {table} after race retries")


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
        """Query stories and their latest revision visible at snapshot_at with recent activity."""
        lookback_hours = 24
        excluded_platforms: list[str] = []
        if eligibility_policy_id is not None:
            cur = await conn.execute(
                "SELECT config->>'lookback_hours', config->'excluded_platforms' FROM eligibility_policy_versions WHERE id = %s",
                (eligibility_policy_id,),
            )
            pol_row = await cur.fetchone()
            if pol_row is not None:
                if pol_row[0] is not None:
                    try:
                        lookback_hours = int(pol_row[0])
                    except (ValueError, TypeError):
                        lookback_hours = 24
                if pol_row[1] is not None and isinstance(pol_row[1], list):
                    excluded_platforms = [
                        str(p).strip().lower() for p in pol_row[1] if str(p).strip()
                    ]

        window_start = snapshot_at - dt.timedelta(hours=lookback_hours)

        cursor = await conn.execute(
            """
            WITH latest_revs AS (
                SELECT DISTINCT ON (sr.story_id)
                    sr.story_id,
                    sr.id AS story_revision_id,
                    sr.revision_no,
                    sr.current_state,
                    sr.semantic_text,
                    sr.created_at AS revision_created_at,
                    sr.event_payload
                FROM story_revisions sr
                JOIN stories s ON s.id = sr.story_id
                WHERE s.edition_id = %s
                  AND sr.created_at <= %s
                ORDER BY sr.story_id, sr.revision_no DESC, sr.created_at DESC
            ),
            story_activity AS (
                SELECT
                    lr.story_id,
                    lr.story_revision_id,
                    lr.revision_no,
                    lr.current_state,
                    lr.semantic_text,
                    lr.revision_created_at,
                    lr.event_payload,
                    s.created_at AS story_created_at,
                    s.knowledge_source,
                    COALESCE(
                        (
                            SELECT count(DISTINCT sc.claim_id)
                            FROM story_claims sc
                            JOIN claims c ON c.id = sc.claim_id
                            JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                            JOIN source_items si ON si.id = sir.source_item_id
                            JOIN sources src ON src.id = si.source_id
                            WHERE sc.story_id = lr.story_id
                              AND sc.attached_at <= %s
                              AND c.created_at <= %s
                              AND (cardinality(%s::text[]) = 0 OR src.platform <> ALL(%s::text[]))
                        ),
                        (
                            SELECT scst.fragment_count
                            FROM story_cluster_state scst
                            WHERE scst.story_id = lr.story_id
                        ),
                        0
                    ) AS claim_count,
                    COALESCE(
                        (
                            SELECT count(DISTINCT sc.claim_id)
                            FROM story_claims sc
                            JOIN claims c ON c.id = sc.claim_id
                            JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                            JOIN source_items si ON si.id = sir.source_item_id
                            JOIN sources src ON src.id = si.source_id
                            WHERE sc.story_id = lr.story_id
                              AND sc.attached_at >= %s
                              AND sc.attached_at <= %s
                              AND c.created_at <= %s
                              AND (cardinality(%s::text[]) = 0 OR src.platform <> ALL(%s::text[]))
                        ),
                        (
                            SELECT scst.fragment_count
                            FROM story_cluster_state scst
                            WHERE scst.story_id = lr.story_id
                        ),
                        0
                    ) AS new_claims_count,
                    COALESCE(
                        (
                            SELECT count(DISTINCT sir.source_item_id)
                            FROM story_claims sc
                            JOIN claims c ON c.id = sc.claim_id
                            JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                            JOIN source_items si ON si.id = sir.source_item_id
                            JOIN sources src ON src.id = si.source_id
                            WHERE sc.story_id = lr.story_id
                              AND sc.attached_at <= %s
                              AND c.created_at <= %s
                              AND (cardinality(%s::text[]) = 0 OR src.platform <> ALL(%s::text[]))
                        ),
                        (
                            SELECT scst.unique_source_count
                            FROM story_cluster_state scst
                            WHERE scst.story_id = lr.story_id
                        ),
                        0
                    ) AS source_count,
                    COALESCE(
                        (
                            SELECT MAX(COALESCE(si.published_at, si.first_collected_at))
                            FROM story_claims sc
                            JOIN claims c ON c.id = sc.claim_id
                            JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                            JOIN source_items si ON si.id = sir.source_item_id
                            JOIN sources src ON src.id = si.source_id
                            WHERE sc.story_id = lr.story_id
                              AND sc.attached_at <= %s
                              AND c.created_at <= %s
                              AND (cardinality(%s::text[]) = 0 OR src.platform <> ALL(%s::text[]))
                        ),
                        (
                            SELECT scst.last_seen_at
                            FROM story_cluster_state scst
                            WHERE scst.story_id = lr.story_id
                        )
                    ) AS newest_source_published_at,
                    (
                        SELECT si.metadata->>'temporal_fidelity'
                        FROM story_claims sc
                        JOIN claims c ON c.id = sc.claim_id
                        JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                        JOIN source_items si ON si.id = sir.source_item_id
                        JOIN sources src ON src.id = si.source_id
                        WHERE sc.story_id = lr.story_id
                          AND sc.attached_at <= %s
                          AND c.created_at <= %s
                          AND (cardinality(%s::text[]) = 0 OR src.platform <> ALL(%s::text[]))
                        ORDER BY COALESCE(si.published_at, si.first_collected_at) DESC NULLS LAST
                        LIMIT 1
                    ) AS newest_source_temporal_fidelity,
                    (
                        SELECT MAX(event_time)
                        FROM (
                            SELECT lr.revision_created_at AS event_time
                            WHERE cardinality(%s::text[]) = 0
                            UNION ALL
                            SELECT MAX(sc.attached_at) AS event_time
                            FROM story_claims sc
                            JOIN claims c ON c.id = sc.claim_id
                            JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                            JOIN source_items si ON si.id = sir.source_item_id
                            JOIN sources src ON src.id = si.source_id
                            WHERE sc.story_id = lr.story_id
                              AND sc.attached_at <= %s
                              AND c.created_at <= %s
                              AND (cardinality(%s::text[]) = 0 OR src.platform <> ALL(%s::text[]))
                            UNION ALL
                            SELECT MAX(sse.observed_at) AS event_time
                            FROM story_state_events sse
                            WHERE sse.story_id = lr.story_id
                              AND sse.observed_at <= %s
                        ) t
                    ) AS last_activity_at,
                    (
                        cardinality(%s::text[]) = 0
                        AND EXISTS (
                            SELECT 1
                            FROM story_revisions sr2
                            WHERE sr2.story_id = lr.story_id
                              AND sr2.created_at >= %s
                              AND sr2.created_at <= %s
                        )
                    ) AS has_recent_revision,
                    EXISTS (
                        SELECT 1
                        FROM story_claims sc2
                        JOIN claims c2 ON c2.id = sc2.claim_id
                        JOIN source_item_revisions sir2 ON sir2.id = c2.source_item_revision_id
                        JOIN source_items si2 ON si2.id = sir2.source_item_id
                        JOIN sources src2 ON src2.id = si2.source_id
                        WHERE sc2.story_id = lr.story_id
                          AND sc2.attached_at >= %s
                          AND sc2.attached_at <= %s
                          AND c2.created_at <= %s
                          AND (cardinality(%s::text[]) = 0 OR src2.platform <> ALL(%s::text[]))
                    ) AS has_recent_claim,
                    EXISTS (
                        SELECT 1
                        FROM story_state_events sse2
                        WHERE sse2.story_id = lr.story_id
                          AND sse2.observed_at >= %s
                          AND sse2.observed_at <= %s
                    ) AS has_recent_event
                FROM latest_revs lr
                JOIN stories s ON s.id = lr.story_id
                WHERE lr.current_state NOT IN ('invalid', 'archived', 'rejected')
                  AND COALESCE(
                      (
                          SELECT sse.type
                          FROM story_state_events sse
                          WHERE sse.story_id = lr.story_id
                            AND sse.observed_at <= %s
                          ORDER BY sse.observed_at DESC, sse.id DESC
                          LIMIT 1
                      ), 'active'
                  ) <> 'archived'
            )
            SELECT
                story_id,
                story_revision_id,
                revision_no,
                current_state,
                semantic_text,
                revision_created_at,
                claim_count,
                source_count,
                new_claims_count,
                last_activity_at,
                story_created_at,
                has_recent_revision,
                has_recent_claim,
                has_recent_event,
                newest_source_published_at,
                newest_source_temporal_fidelity
            FROM story_activity
            WHERE (claim_count > 0 OR knowledge_source = 'event_first')
              AND (has_recent_revision OR has_recent_claim OR has_recent_event OR story_created_at >= %s)
              AND (
                  event_payload IS NULL
                  OR event_payload->>'publishability' IS NULL
                  OR event_payload->>'publishability' IN ('news', 'brief')
              )
            ORDER BY last_activity_at DESC NULLS LAST, story_id ASC
            """,
            (
                edition_id,
                snapshot_at,
                # claim_count
                snapshot_at,
                snapshot_at,
                excluded_platforms,
                excluded_platforms,
                # new_claims_count
                window_start,
                snapshot_at,
                snapshot_at,
                excluded_platforms,
                excluded_platforms,
                # source_count
                snapshot_at,
                snapshot_at,
                excluded_platforms,
                excluded_platforms,
                # newest_source_published_at
                snapshot_at,
                snapshot_at,
                excluded_platforms,
                excluded_platforms,
                # newest_source_temporal_fidelity
                snapshot_at,
                snapshot_at,
                excluded_platforms,
                excluded_platforms,
                # last_activity_at revision_created_at check
                excluded_platforms,
                # last_activity_at claims
                snapshot_at,
                snapshot_at,
                excluded_platforms,
                excluded_platforms,
                # last_activity_at state_events
                snapshot_at,
                # has_recent_revision check
                excluded_platforms,
                window_start,
                snapshot_at,
                # has_recent_claim
                window_start,
                snapshot_at,
                snapshot_at,
                excluded_platforms,
                excluded_platforms,
                # has_recent_event
                window_start,
                snapshot_at,
                # historical lifecycle_state
                snapshot_at,
                # outer WHERE story_created_at
                window_start,
            ),
        )
        rows = await cursor.fetchall()

        # If platforms are excluded, derive filtered editorial text for snapshot features
        filtered_story_texts: dict[int, str] = {}
        if excluded_platforms and rows:
            c_text_cur = await conn.execute(
                """
                SELECT sc.story_id, c.normalized_assertion
                FROM story_claims sc
                JOIN claims c ON c.id = sc.claim_id
                JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                JOIN source_items si ON si.id = sir.source_item_id
                JOIN sources src ON src.id = si.source_id
                WHERE sc.story_id = ANY(%s)
                  AND sc.attached_at <= %s
                  AND c.created_at <= %s
                  AND (cardinality(%s::text[]) = 0 OR src.platform <> ALL(%s::text[]))
                ORDER BY sc.story_id, sc.claim_id ASC
                """,
                (
                    [r[0] for r in rows],
                    snapshot_at,
                    snapshot_at,
                    excluded_platforms,
                    excluded_platforms,
                ),
            )
            c_text_rows = await c_text_cur.fetchall()
            from collections import defaultdict

            story_assertions = defaultdict(list)
            for sid, norm_assert in c_text_rows:
                if norm_assert:
                    story_assertions[sid].append(norm_assert.strip())
            for sid, asserts in story_assertions.items():
                filtered_story_texts[sid] = " ".join(asserts)

        candidates = []
        for r in rows:
            story_id = r[0]
            story_rev_id = r[1]
            rev_no = r[2]
            current_state = r[3]
            raw_semantic_text = r[4]
            semantic_text = filtered_story_texts.get(story_id) or raw_semantic_text
            rev_created_at = r[5]
            claim_count = r[6]
            source_count = r[7]
            new_claims_count = r[8]
            last_activity_at = r[9]
            story_created_at = r[10]
            has_recent_revision = r[11]
            has_recent_claim = r[12]
            has_recent_event = r[13]
            newest_source_published_at = r[14]
            newest_source_temporal_fidelity = r[15]

            source_age_hours = (
                round((snapshot_at - newest_source_published_at).total_seconds() / 3600.0, 1)
                if (
                    newest_source_published_at
                    and isinstance(newest_source_published_at, dt.datetime)
                )
                else None
            )

            if story_created_at >= window_start:
                activity_type = "new_story"
            elif new_claims_count > 0 or has_recent_claim:
                activity_type = "new_claims"
            elif has_recent_revision:
                activity_type = "revised"
            elif has_recent_event:
                activity_type = "state_change"
            else:
                activity_type = "activity"

            candidates.append(
                {
                    "story_id": story_id,
                    "story_revision_id": story_rev_id,
                    "revision_no": rev_no,
                    "current_state": current_state,
                    "semantic_text": semantic_text,
                    "created_at": rev_created_at,
                    "claim_count": claim_count,
                    "source_count": source_count,
                    "new_claims_count": new_claims_count,
                    "last_activity_at": last_activity_at,
                    "activity_type": activity_type,
                    "snapshot_features": {
                        "claim_count": claim_count,
                        "source_count": source_count,
                        "new_claims_count": new_claims_count,
                        "last_activity_at": (
                            last_activity_at.isoformat() if last_activity_at else None
                        ),
                        "activity_type": activity_type,
                        "semantic_text": semantic_text,
                        "source_published_at": (
                            newest_source_published_at.isoformat()
                            if newest_source_published_at
                            else None
                        ),
                        "newest_source_published_at": (
                            newest_source_published_at.isoformat()
                            if newest_source_published_at
                            else None
                        ),
                        "source_age_hours": source_age_hours,
                        "temporal_fidelity": newest_source_temporal_fidelity or "unknown",
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
        claim_roles: dict[int, str] | None = None,
        evidence_cluster_ids: list[int] | None = None,
        fragment_ids: list[int] | None = None,
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
            # Query current source attribution and temporal provenance metadata to permanently freeze in the snapshot
            src_cur = await conn.execute(
                """
                SELECT c.id, s.id, s.platform, s.name, s.role, s.url, s.external_id,
                       si.published_at, si.canonical_url, si.author_name,
                       si.metadata->>'temporal_fidelity' AS temporal_fidelity,
                       si.metadata->>'raw_timestamp' AS raw_timestamp
                FROM claims c
                JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                JOIN source_items si ON si.id = sir.source_item_id
                JOIN sources s ON s.id = si.source_id
                WHERE c.id = ANY(%s)
                """,
                (claim_ids,),
            )
            src_rows = await src_cur.fetchall()
            meta_by_cid = {
                r[0]: {
                    "source_id": r[1],
                    "platform": r[2],
                    "name": r[3],
                    "role": r[4],
                    "url": r[5],
                    "external_id": r[6],
                    "published_at": (
                        r[7].isoformat()
                        if isinstance(r[7], (dt.datetime, dt.date))
                        else (str(r[7]) if r[7] is not None else None)
                    ),
                    "canonical_url": r[8],
                    "author_name": r[9],
                    "temporal_fidelity": r[10],
                    "raw_timestamp": r[11],
                }
                for r in src_rows
            }
            roles = claim_roles or {}
            insert_rows = []
            for cid in claim_ids:
                smeta = meta_by_cid.get(cid, {})
                srole = roles.get(cid) or smeta.get("role")
                sname = smeta.get("name")
                insert_rows.append(
                    (input_row.id, cid, srole, sname, Jsonb(smeta) if smeta else None)
                )

            cur = conn.cursor()
            await cur.executemany(
                """
                INSERT INTO publication_input_claims (
                    publication_input_id, claim_id, source_role, source_name, source_snapshot
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (publication_input_id, claim_id) DO UPDATE
                SET source_role = EXCLUDED.source_role,
                    source_name = EXCLUDED.source_name,
                    source_snapshot = EXCLUDED.source_snapshot
                """,
                insert_rows,
            )

            cur = conn.cursor()
            await cur.executemany(
                """
                INSERT INTO publication_input_evidence_clusters (publication_input_id, evidence_cluster_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
                """,
                [(input_row.id, ecid) for ecid in (evidence_cluster_ids or [])],
            )

        if fragment_ids:
            cur = conn.cursor()
            await cur.executemany(
                """
                INSERT INTO publication_input_fragments (publication_input_id, fragment_id, source_snapshot)
                VALUES (%s, %s, %s)
                ON CONFLICT (publication_input_id, fragment_id) DO NOTHING
                """,
                [(input_row.id, fid, Jsonb({})) for fid in fragment_ids],
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
            fragment_ids=fragment_ids or [],
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
            # load fragments
            f_cur = await conn.execute(
                "SELECT fragment_id FROM publication_input_fragments WHERE publication_input_id = %s ORDER BY fragment_id ASC",
                (inp.id,),
            )
            f_ids = [r[0] for r in await f_cur.fetchall()]
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
                    fragment_ids=f_ids,
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

    async def get_latest_delivered_digest_text(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
    ) -> str | None:
        """Text of the newest successfully delivered telegram_channel payload
        belonging to a digest publication of the edition, or None."""
        cursor = await conn.execute(
            """
            SELECT pay.rendered_content ->> 'text'
            FROM publications p
            JOIN publication_runs pr ON p.publication_run_id = pr.id
            JOIN publication_delivery_payloads pay ON pay.publication_id = p.id
            JOIN delivery_destinations d ON pay.destination_id = d.id
            JOIN publication_deliveries del ON del.payload_id = pay.id
            WHERE pr.edition_id = %s
              AND p.publication_type = 'digest_grouped'
              AND d.platform = 'telegram_channel'
              AND del.status = 'succeeded'
            ORDER BY p.created_at DESC, pay.id ASC
            LIMIT 1
            """,
            (edition_id,),
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])


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
            ON CONFLICT (publication_id, destination_id) DO UPDATE SET
                payload_id = EXCLUDED.payload_id
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
