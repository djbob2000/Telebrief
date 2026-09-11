"""Exact-assignment selectors for background Event-First authority work."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import psycopg

from src.domain.event_authority import AuthorityBarrierState, AuthorityTarget
from src.repositories.event_retries import EventProcessingRetryRepository


class EventAuthorityRepository:
    """Queries that schedule and recheck bounded authority work."""

    async def list_background_targets(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        triage_version: str,
        scope_version: str,
        scope_config_hash: str,
        now: dt.datetime,
        limit: int,
        active_window_hours: int | None = None,
    ) -> list[AuthorityTarget]:
        cutoff_clause = ""
        params: list[object] = [edition_id]
        if active_window_hours is not None:
            active_cutoff = now - dt.timedelta(hours=active_window_hours)
            cutoff_clause = "AND sc.last_seen_at >= %s"
            params.append(active_cutoff)
        params.extend(
            [
                now,
                triage_version,
                scope_config_hash,
                edition_id,
                scope_version,
                scope_config_hash,
                limit,
            ]
        )
        cursor = await conn.execute(
            f"""
            SELECT sc.story_id, sc.latest_assignment_id
            FROM story_cluster_state sc
            JOIN stories s ON s.id = sc.story_id
            LEFT JOIN story_event_processing_retries retry
              ON retry.story_id = sc.story_id
             AND retry.latest_assignment_id = sc.latest_assignment_id
             AND retry.stage = 'triage'
            WHERE s.edition_id = %s
              AND s.knowledge_source = 'event_first'
              AND sc.analysis_dirty = TRUE
              {cutoff_clause}
              AND retry.exhausted_at IS NULL
              AND (retry.next_retry_at IS NULL OR retry.next_retry_at <= %s)
              AND NOT EXISTS (
                  SELECT 1
                  FROM story_event_triage_decisions setd
                  JOIN story_edition_scope_decisions sesd
                    ON sesd.story_id = setd.story_id
                   AND sesd.latest_assignment_id = setd.latest_assignment_id
                   AND sesd.scope_config_hash = setd.scope_config_hash
                  WHERE setd.story_id = sc.story_id
                    AND setd.latest_assignment_id = sc.latest_assignment_id
                    AND setd.triage_version = %s
                    AND setd.scope_config_hash = %s
                    AND sesd.edition_id = %s
                    AND sesd.scope_version = %s
                    AND sesd.scope_config_hash = %s
                    AND (
                        setd.retention <> 'KEEP'
                        OR EXISTS (
                            SELECT 1
                            FROM story_revisions sr
                            WHERE sr.story_id = sc.story_id
                              AND sr.event_assignment_id = sc.latest_assignment_id
                              AND sr.event_payload IS NOT NULL
                              AND sr.event_payload->>'publishability' IN ('news', 'brief')
                        )
                    )
              )
            ORDER BY sc.last_seen_at DESC, sc.story_id DESC
            LIMIT %s
            """,  # noqa: S608 — static predicate template; values are bound params
            params,
        )
        return [
            AuthorityTarget(
                story_id=int(row[0]),
                assignment_id=int(row[1]),
                edition_id=edition_id,
                triage_version=triage_version,
                scope_version=scope_version,
                scope_config_hash=scope_config_hash,
            )
            for row in await cursor.fetchall()
        ]

    async def filter_unsatisfied_targets(
        self,
        conn: psycopg.AsyncConnection,
        *,
        targets: Sequence[AuthorityTarget],
        snapshot_at: dt.datetime,
    ) -> list[AuthorityTarget]:
        """Return requested targets still missing exact authority evidence."""
        if not targets:
            return []
        cursor = await conn.execute(
            """
            WITH requested AS (
                SELECT *
                FROM unnest(
                    %s::bigint[], %s::bigint[], %s::bigint[],
                    %s::text[], %s::text[], %s::text[]
                ) AS r(
                    story_id, assignment_id, edition_id,
                    triage_version, scope_version, scope_config_hash
                )
            )
            SELECT r.story_id, r.assignment_id, r.edition_id,
                   r.triage_version, r.scope_version, r.scope_config_hash
            FROM requested r
            WHERE NOT EXISTS (
                SELECT 1
                FROM story_event_triage_decisions setd
                JOIN story_edition_scope_decisions sesd
                  ON sesd.story_id = setd.story_id
                 AND sesd.latest_assignment_id = setd.latest_assignment_id
                 AND sesd.scope_config_hash = setd.scope_config_hash
                WHERE setd.story_id = r.story_id
                  AND setd.latest_assignment_id = r.assignment_id
                  AND setd.triage_version = r.triage_version
                  AND setd.scope_config_hash = r.scope_config_hash
                  AND setd.created_at <= %s
                  AND sesd.edition_id = r.edition_id
                  AND sesd.scope_version = r.scope_version
                  AND sesd.scope_config_hash = r.scope_config_hash
                  AND sesd.created_at <= %s
                  AND (
                      setd.retention <> 'KEEP'
                      OR EXISTS (
                          SELECT 1
                          FROM story_revisions sr
                          WHERE sr.story_id = r.story_id
                            AND sr.event_assignment_id = r.assignment_id
                            AND sr.created_at <= %s
                            AND sr.event_payload IS NOT NULL
                            AND sr.event_payload->>'publishability' IN ('news', 'brief')
                      )
                  )
            )
            ORDER BY r.story_id, r.assignment_id
            """,
            (
                [target.story_id for target in targets],
                [target.assignment_id for target in targets],
                [target.edition_id for target in targets],
                [target.triage_version for target in targets],
                [target.scope_version for target in targets],
                [target.scope_config_hash for target in targets],
                snapshot_at,
                snapshot_at,
                snapshot_at,
            ),
        )
        return [AuthorityTarget(*row) for row in await cursor.fetchall()]

    async def list_due_enrichment_assignments(
        self,
        conn: psycopg.AsyncConnection,
        *,
        now: dt.datetime,
        limit: int = 100,
    ) -> list[tuple[int, int]]:
        """Find dirty assignments whose optional Rich Analysis is due."""
        cursor = await conn.execute(
            """
            SELECT due.story_id, due.latest_assignment_id
            FROM (
                SELECT DISTINCT sc.story_id, sc.latest_assignment_id, sc.last_seen_at
                FROM story_cluster_state sc
                JOIN story_event_triage_decisions setd
                  ON setd.story_id = sc.story_id
                 AND setd.latest_assignment_id = sc.latest_assignment_id
                JOIN story_edition_scope_decisions sesd
                  ON sesd.story_id = setd.story_id
                 AND sesd.latest_assignment_id = setd.latest_assignment_id
                 AND sesd.scope_config_hash = setd.scope_config_hash
                LEFT JOIN story_event_processing_retries retry
                  ON retry.story_id = sc.story_id
                 AND retry.latest_assignment_id = sc.latest_assignment_id
                 AND retry.stage = 'analysis'
                WHERE sc.analysis_dirty = TRUE
                  AND setd.retention = 'KEEP'
                  AND setd.enrichment = 'ANALYZE'
                  AND sesd.scope_class IN ('LOCAL', 'DIRECT_IMPACT')
                  AND retry.exhausted_at IS NULL
                  AND (retry.next_retry_at IS NULL OR retry.next_retry_at <= %s)
            ) AS due
            ORDER BY due.last_seen_at ASC, due.story_id ASC
            LIMIT %s
            """,
            (now, limit),
        )
        return [(int(row[0]), int(row[1])) for row in await cursor.fetchall()]

    async def get_publication_barrier_state(
        self,
        conn: psycopg.AsyncConnection,
        *,
        required_targets: Sequence[AuthorityTarget],
        gap_targets: Sequence[AuthorityTarget],
        evaluation_at: dt.datetime,
        deadline_at: dt.datetime,
        observed_at: dt.datetime,
        publication_repo,
    ) -> AuthorityBarrierState:
        """Classify mutable retry/claim state without changing exact gap truth."""
        del observed_at
        gap_tuple = tuple(gap_targets)
        if not gap_tuple:
            completed_at = await publication_repo.get_authority_barrier_completed_at(
                conn, targets=required_targets, snapshot_at=evaluation_at
            )
            return AuthorityBarrierState((), completed_at, 0, 0, None, 0, 0)

        retry_states = await EventProcessingRetryRepository().get_for_assignments(
            conn,
            [(target.story_id, target.assignment_id) for target in gap_tuple],
            stage="triage",
        )
        cursor = await conn.execute(
            """
            SELECT story_id, latest_assignment_id
            FROM story_event_processing_claims
            WHERE stage = 'triage'
              AND lease_expires_at > now()
              AND (story_id, latest_assignment_id) IN (
                  SELECT * FROM unnest(%s::bigint[], %s::bigint[])
              )
            """,
            (
                [target.story_id for target in gap_tuple],
                [target.assignment_id for target in gap_tuple],
            ),
        )
        live_claims = {(int(row[0]), int(row[1])) for row in await cursor.fetchall()}
        terminal = 0
        retry_exceeds = 0
        processable = 0
        in_flight = 0
        future_retries: list[dt.datetime] = []
        for target in gap_tuple:
            retry = retry_states.get((target.story_id, target.assignment_id))
            if (
                retry is not None
                and retry.exhausted_at is not None
                and retry.exhausted_at <= evaluation_at
                and retry.updated_at <= evaluation_at
            ):
                terminal += 1
                continue
            if retry is not None and retry.updated_at <= evaluation_at:
                if retry.next_retry_at is not None and retry.next_retry_at >= deadline_at:
                    retry_exceeds += 1
                    continue
                if retry.next_retry_at is not None and retry.next_retry_at > evaluation_at:
                    future_retries.append(retry.next_retry_at)
                    continue
            if (target.story_id, target.assignment_id) in live_claims:
                in_flight += 1
            else:
                processable += 1
        return AuthorityBarrierState(
            gap_tuple,
            None,
            terminal,
            retry_exceeds,
            min(future_retries) if future_retries else None,
            processable,
            in_flight,
        )
