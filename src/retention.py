"""Retention service for periodic cleanup of expired data (Plan 5 Task 6).

Knowledge is kept forever: source texts, Stories, revisions and publications
are never deleted here. What expires is working state whose job is done:

* fragment embedding vectors and Story centroids, which only matter while a
  Story can still absorb fragments (the clustering window is 72 hours). The
  vector becomes NULL and the row stays, so foreign keys and provenance hold;
  a later fragment with the same text refills it.
* Procrastinate job history and collection-run observations, which are
  diagnostics and readiness bookkeeping for recent runs.

Before a published Story loses its centroid, the centroid is copied to
``story_semantic_vectors`` (half precision), which longitudinal features use.
Each step runs in small batches, one transaction each, so the 1 GB host keeps
serving processing while the cleanup runs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from src.db.uow import DatabaseUnitOfWork
from src.repositories.facebook import FacebookRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionPolicy:
    """How long working state outlives the Stories that used it."""

    fragment_vector_days: int = 14
    centroid_days: int = 14
    job_history_hours: int = 720
    collection_observation_days: int = 30
    batch_size: int = 2000
    observation_runs_per_batch: int = 50
    max_batches_per_step: int = 500
    pause_seconds: float = 0.2


@dataclass(frozen=True)
class CleanupResult:
    """Outcome counters for retention maintenance."""

    artifacts_deleted: int = 0
    files_deleted: int = 0
    semantic_vectors_synced: int = 0
    centroids_evicted: int = 0
    fragment_vectors_evicted: int = 0
    jobs_deleted: int = 0
    collection_observations_deleted: int = 0


@dataclass(frozen=True)
class RetentionReportRow:
    """Rows and stored bytes one retention step would touch."""

    step: str
    rows: int
    bytes: int


_FINAL_JOB_STATUSES = ["succeeded", "failed", "cancelled", "aborted"]

# Published Stories whose centroid is newer than their semantic vector.
_SEMANTIC_SYNC_SOURCE = """
    FROM story_cluster_state sc
    JOIN stories s ON s.id = sc.story_id
    LEFT JOIN story_semantic_vectors ssv ON ssv.story_id = sc.story_id
    WHERE s.current_revision_id IS NOT NULL
      AND sc.centroid IS NOT NULL
      AND (ssv.story_id IS NULL OR ssv.source_updated_at < sc.updated_at)
"""

# Quiet, clean Stories; a published one keeps its centroid until the
# semantic vector holds its latest version.
_CENTROID_ELIGIBLE = """
    FROM story_cluster_state sc
    JOIN stories s ON s.id = sc.story_id
    WHERE sc.centroid IS NOT NULL
      AND sc.analysis_dirty = FALSE
      AND sc.last_seen_at < %(cutoff)s
      AND (
          s.current_revision_id IS NULL
          OR EXISTS (
              SELECT 1
              FROM story_semantic_vectors ssv
              WHERE ssv.story_id = sc.story_id
                AND ssv.source_updated_at >= sc.updated_at
          )
      )
"""

# Old vectors that no recent link, recent Story or pending analysis still uses.
_FRAGMENT_VECTOR_ELIGIBLE = """
    FROM fragment_embedding_vectors fev
    WHERE fev.embedding IS NOT NULL
      AND fev.created_at < %(cutoff)s
      AND NOT EXISTS (
          SELECT 1
          FROM source_fragment_embeddings sfe
          LEFT JOIN story_fragments sf ON sf.fragment_id = sfe.fragment_id
          LEFT JOIN story_cluster_state sc ON sc.story_id = sf.story_id
          WHERE sfe.vector_id = fev.id
            AND (
                sfe.created_at >= %(cutoff)s
                OR sc.analysis_dirty
                OR sc.last_seen_at >= %(cutoff)s
            )
      )
"""

# Finished jobs whose latest event is older than the window, as in
# Procrastinate's own delete_old_jobs (a job without events is kept). A job
# that a periodic defer row still references is kept: that foreign key has
# no cascade.
_JOB_ELIGIBLE = """
    FROM procrastinate_jobs j
    WHERE j.status::text = ANY(%(statuses)s)
      AND (
          SELECT max(e.at) FROM procrastinate_events e WHERE e.job_id = j.id
      ) < %(cutoff)s
      AND NOT EXISTS (
          SELECT 1 FROM procrastinate_periodic_defers d WHERE d.job_id = j.id
      )
"""

# Old collection runs whose observations no recent refresh run uses. The
# observation primary key starts with collection_run_id, so deleting whole
# runs walks that index instead of rescanning the large table per batch.
_OBSERVATION_RUN_ELIGIBLE = """
    FROM collection_runs cr
    WHERE COALESCE(cr.completed_at, cr.started_at) < %(cutoff)s
      AND EXISTS (
          SELECT 1 FROM collection_run_revision_observations cro
          WHERE cro.collection_run_id = cr.id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM publication_refresh_sources prs
          JOIN publication_refresh_runs prr ON prr.id = prs.refresh_run_id
          WHERE prs.collection_run_id = cr.id
            AND prr.requested_at >= %(cutoff)s
      )
"""


class RetentionService:
    """Cleans up expired artifacts and evicts working state that is no longer used."""

    def __init__(
        self,
        uow: DatabaseUnitOfWork,
        fb_repo: FacebookRepository | None = None,
        policy: RetentionPolicy | None = None,
    ) -> None:
        self.uow = uow
        self.fb_repo = fb_repo or FacebookRepository()
        self.policy = policy or RetentionPolicy()

    async def cleanup(self, now: dt.datetime | None = None) -> CleanupResult:
        """Run every retention step; domain items and Stories are never deleted."""
        cutoff = now or dt.datetime.now(dt.timezone.utc)
        artifacts_count, files_count = await self._cleanup_artifacts(cutoff)
        synced = await self.sync_story_semantic_vectors()
        centroids = await self.evict_story_centroids(cutoff)
        vectors = await self.evict_fragment_vectors(cutoff)
        jobs = await self.prune_job_history(cutoff)
        observations = await self.prune_collection_observations(cutoff)
        result = CleanupResult(
            artifacts_deleted=artifacts_count,
            files_deleted=files_count,
            semantic_vectors_synced=synced,
            centroids_evicted=centroids,
            fragment_vectors_evicted=vectors,
            jobs_deleted=jobs,
            collection_observations_deleted=observations,
        )
        logger.info("Retention cleanup completed at %s: %s", cutoff.isoformat(), result)
        return result

    async def _cleanup_artifacts(self, cutoff: dt.datetime) -> tuple[int, int]:
        artifacts_count = 0
        files_count = 0
        async with self.uow.transaction() as conn:
            expired_artifacts = await self.fb_repo.list_expired_artifacts(conn, cutoff)

            for artifact in expired_artifacts:
                # Delete physical file first; a failed unlink leaves the row
                # unmarked so the next cleanup pass retries it instead of
                # orphaning the file behind a deleted_at stamp.
                unlink_failed = False
                if artifact.storage_path:
                    try:
                        p = Path(artifact.storage_path)
                        if p.exists() and p.is_file():
                            p.unlink()
                            files_count += 1
                    except Exception as e:
                        unlink_failed = True
                        logger.warning(
                            "Failed deleting physical artifact file %s; will retry: %s",
                            artifact.storage_path,
                            e,
                        )

                # Preserve provenance metadata; only the physical file is
                # removed and the row is marked deleted (Plan 5 Task 6).
                if not unlink_failed:
                    await self.fb_repo.mark_artifact_deleted(conn, artifact.id, cutoff)
                    artifacts_count += 1
        return artifacts_count, files_count

    async def sync_story_semantic_vectors(self) -> int:
        """Copy the latest centroid of every published Story into its semantic vector."""
        sql = f"""
            INSERT INTO story_semantic_vectors (
                story_id, embedding, model, dimensions, source, source_updated_at
            )
            SELECT sc.story_id, sc.centroid::halfvec, sc.model, sc.dimensions,
                   'cluster_centroid', sc.updated_at
            {_SEMANTIC_SYNC_SOURCE}
            ORDER BY sc.story_id
            LIMIT %(limit)s
            ON CONFLICT (story_id) DO UPDATE SET
                embedding = EXCLUDED.embedding,
                model = EXCLUDED.model,
                dimensions = EXCLUDED.dimensions,
                source = EXCLUDED.source,
                source_updated_at = EXCLUDED.source_updated_at,
                updated_at = now()
            """  # noqa: S608
        return await self._run_batches("semantic_vectors", sql, {})

    async def evict_story_centroids(self, now: dt.datetime) -> int:
        """Drop centroids of Stories that cannot join new fragments any more."""
        cutoff = now - dt.timedelta(days=self.policy.centroid_days)
        sql = f"""
            WITH batch AS (
                SELECT sc.story_id
                {_CENTROID_ELIGIBLE}
                ORDER BY sc.story_id
                LIMIT %(limit)s
                FOR UPDATE OF sc SKIP LOCKED
            )
            UPDATE story_cluster_state sc
            SET centroid = NULL
            FROM batch
            WHERE sc.story_id = batch.story_id
            """  # noqa: S608
        return await self._run_batches("centroids", sql, {"cutoff": cutoff})

    async def evict_fragment_vectors(self, now: dt.datetime) -> int:
        """Drop fragment vectors that no recent or pending Story still uses."""
        cutoff = now - dt.timedelta(days=self.policy.fragment_vector_days)
        sql = f"""
            WITH batch AS (
                SELECT fev.id
                {_FRAGMENT_VECTOR_ELIGIBLE}
                ORDER BY fev.created_at, fev.id
                LIMIT %(limit)s
                FOR UPDATE OF fev SKIP LOCKED
            )
            UPDATE fragment_embedding_vectors fev
            SET embedding = NULL, evicted_at = now()
            FROM batch
            WHERE fev.id = batch.id
            """  # noqa: S608
        return await self._run_batches("fragment_vectors", sql, {"cutoff": cutoff})

    async def prune_job_history(self, now: dt.datetime) -> int:
        """Delete finished Procrastinate jobs; their events go by cascade."""
        cutoff = now - dt.timedelta(hours=self.policy.job_history_hours)
        sql = f"""
            WITH batch AS (
                SELECT j.id
                {_JOB_ELIGIBLE}
                ORDER BY j.id
                LIMIT %(limit)s
            )
            DELETE FROM procrastinate_jobs j
            USING batch
            WHERE j.id = batch.id
            """  # noqa: S608
        params = {"cutoff": cutoff, "statuses": _FINAL_JOB_STATUSES}
        return await self._run_batches("jobs", sql, params)

    async def prune_collection_observations(self, now: dt.datetime) -> int:
        """Delete readiness observations of collection runs no refresh run needs."""
        cutoff = now - dt.timedelta(days=self.policy.collection_observation_days)
        sql = f"""
            WITH runs AS (
                SELECT cr.id
                {_OBSERVATION_RUN_ELIGIBLE}
                ORDER BY cr.id
                LIMIT %(limit)s
            )
            DELETE FROM collection_run_revision_observations cro
            USING runs
            WHERE cro.collection_run_id = runs.id
            """  # noqa: S608
        return await self._run_batches(
            "collection_observations",
            sql,
            {"cutoff": cutoff},
            limit=self.policy.observation_runs_per_batch,
            stop_on_partial=False,
        )

    async def report(self, now: dt.datetime | None = None) -> list[RetentionReportRow]:
        """Count what each step would touch, without changing anything."""
        moment = now or dt.datetime.now(dt.timezone.utc)
        policy = self.policy
        queries: list[tuple[str, str, dict[str, Any]]] = [
            (
                "semantic_vectors_to_sync",
                f"SELECT count(*), 0 {_SEMANTIC_SYNC_SOURCE}",  # noqa: S608
                {},
            ),
            (
                "centroids_to_evict",
                f"SELECT count(*), COALESCE(sum(pg_column_size(sc.centroid)), 0) "  # noqa: S608
                f"{_CENTROID_ELIGIBLE}",
                {"cutoff": moment - dt.timedelta(days=policy.centroid_days)},
            ),
            (
                "fragment_vectors_to_evict",
                f"SELECT count(*), COALESCE(sum(pg_column_size(fev.embedding)), 0) "  # noqa: S608
                f"{_FRAGMENT_VECTOR_ELIGIBLE}",
                {"cutoff": moment - dt.timedelta(days=policy.fragment_vector_days)},
            ),
            (
                "jobs_to_delete",
                f"SELECT count(*), 0 {_JOB_ELIGIBLE}",  # noqa: S608
                {
                    "cutoff": moment - dt.timedelta(hours=policy.job_history_hours),
                    "statuses": _FINAL_JOB_STATUSES,
                },
            ),
            (
                "collection_observations_to_delete",
                "SELECT count(*), 0 FROM collection_run_revision_observations cro "  # noqa: S608
                f"WHERE cro.collection_run_id IN (SELECT cr.id {_OBSERVATION_RUN_ELIGIBLE})",
                {"cutoff": moment - dt.timedelta(days=policy.collection_observation_days)},
            ),
        ]
        rows: list[RetentionReportRow] = []
        for step, sql, params in queries:
            async with self.uow.transaction() as conn:
                cursor = await conn.execute(sql, params)
                row = await cursor.fetchone()
            count, size = (int(row[0]), int(row[1])) if row else (0, 0)
            rows.append(RetentionReportRow(step=step, rows=count, bytes=size))
        return rows

    async def _run_batches(
        self,
        step: str,
        sql: str,
        params: dict[str, Any],
        *,
        limit: int | None = None,
        stop_on_partial: bool = True,
    ) -> int:
        """Repeat one bounded statement until it has nothing left to touch.

        ``limit`` bounds the rows (or, for observations, the collection runs)
        one statement selects. When the rows touched cannot be compared with
        that limit, ``stop_on_partial=False`` stops only on an empty batch.
        """
        total = 0
        batch_limit = limit or self.policy.batch_size
        batch_params = {**params, "limit": batch_limit}
        for _ in range(self.policy.max_batches_per_step):
            try:
                async with self.uow.transaction() as conn:
                    cursor = await conn.execute(sql, batch_params)
                    touched = max(cursor.rowcount, 0)
            except psycopg.Error:
                logger.exception("Retention step %s failed after %d rows", step, total)
                return total
            total += touched
            if touched == 0 or (stop_on_partial and touched < batch_limit):
                break
            await asyncio.sleep(self.policy.pause_seconds)
        if total:
            logger.info("Retention step %s touched %d rows", step, total)
        return total
