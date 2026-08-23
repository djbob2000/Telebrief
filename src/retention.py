"""Retention service for periodic cleanup of expired diagnostic files (Plan 5 Task 6)."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from src.db.uow import DatabaseUnitOfWork
from src.repositories.facebook import FacebookRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupResult:
    """Outcome counters for retention maintenance."""

    artifacts_deleted: int = 0
    files_deleted: int = 0


class RetentionService:
    """Cleans up expired diagnostic artifacts and temporary files."""

    def __init__(
        self,
        uow: DatabaseUnitOfWork,
        fb_repo: FacebookRepository | None = None,
    ) -> None:
        self.uow = uow
        self.fb_repo = fb_repo or FacebookRepository()

    async def cleanup(self, now: dt.datetime | None = None) -> CleanupResult:
        """Find and remove expired artifacts without deleting domain items or stories."""
        cutoff = now or dt.datetime.now(dt.timezone.utc)
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

        logger.info(
            "Retention cleanup completed at %s: removed %d artifacts (%d files deleted)",
            cutoff.isoformat(),
            artifacts_count,
            files_count,
        )
        return CleanupResult(
            artifacts_deleted=artifacts_count,
            files_deleted=files_count,
        )
