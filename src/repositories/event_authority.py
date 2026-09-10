"""Exact-assignment selectors for background Event-First authority work."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import psycopg

from src.domain.event_authority import AuthorityTarget


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
    ) -> list[AuthorityTarget]:
        cursor = await conn.execute(
            """
            SELECT sc.story_id, sc.latest_assignment_id
            FROM story_cluster_state sc
            JOIN stories s ON s.id = sc.story_id
            LEFT JOIN story_event_processing_retries retry
              ON retry.story_id = sc.story_id
             AND retry.latest_assignment_id = sc.latest_assignment_id
             AND retry.stage = 'triage'
            WHERE s.edition_id = %s
              AND s.knowledge_source = 'event_first'
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
            ORDER BY sc.last_seen_at ASC, sc.story_id ASC
            LIMIT %s
            """,
            (
                edition_id,
                now,
                triage_version,
                scope_config_hash,
                edition_id,
                scope_version,
                scope_config_hash,
                limit,
            ),
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
