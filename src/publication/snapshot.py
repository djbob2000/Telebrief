"""Publication snapshot service for freezing temporal candidates (Plan 4 Task 2)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from src.db.uow import DatabaseUnitOfWork
from src.publication.models import (
    PublicationCandidate,
    PublicationPolicySet,
    PublicationRun,
)
from src.publication.policies import PublicationPolicyService
from src.publication.repository import PublicationRepository

logger = logging.getLogger(__name__)


class PublicationSnapshotService:
    """Service to create publication runs and seal deterministic candidates."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        repo: PublicationRepository | None = None,
        policy_service: PublicationPolicyService | None = None,
    ) -> None:
        self.uow = uow
        self.repo = repo or PublicationRepository()
        self.policy_service = policy_service or PublicationPolicyService()

    async def create_run(
        self,
        *,
        edition_id: int,
        publication_type: str,
        snapshot_at: dt.datetime,
        request_key: str | None = None,
        policy_ids: PublicationPolicySet | tuple[int, int, int] | None = None,
        config: Any | None = None,
        lookback_hours_override: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PublicationRun:
        key = request_key or f"manual:{edition_id}:{publication_type}:{uuid.uuid4().hex}"

        async with self.uow.transaction() as conn:
            policy_set: PublicationPolicySet | tuple[int, int, int]
            if policy_ids is None:
                policy_set = await self.policy_service.ensure_current(
                    conn,
                    edition_id=edition_id,
                    publication_type=publication_type,
                    config=config,
                    lookback_hours_override=lookback_hours_override,
                )
            else:
                policy_set = policy_ids

            return await self.repo.get_or_create_run(
                conn,
                edition_id=edition_id,
                publication_type=publication_type,
                request_key=key,
                snapshot_at=snapshot_at,
                policy_ids=policy_set,
                metadata=metadata,
            )

    async def seal_candidates(
        self,
        run_id: int,
        *,
        conn: Any | None = None,
    ) -> list[PublicationCandidate]:
        """Seal deterministic candidates for a run.

        When ``conn`` is provided the sealing joins the caller's transaction
        so a subsequent same-connection job deferral is atomic with the state
        transition; otherwise a dedicated transaction is opened.
        """
        if conn is not None:
            return await self._seal_on(conn, run_id)
        async with self.uow.transaction() as conn:
            return await self._seal_on(conn, run_id)

    async def _seal_on(self, conn: Any, run_id: int) -> list[PublicationCandidate]:
        run = await self.repo.lock_run(conn, run_id)
        if run is None:
            raise ValueError(f"publication run {run_id} not found")

        if run.status != "created":
            return await self.repo.load_sealed_candidates(conn, run_id)

        eligible_rows = await self.repo.eligible_story_revisions(
            conn,
            edition_id=run.edition_id,
            snapshot_at=run.snapshot_at,
            eligibility_policy_id=run.eligibility_policy_id,
        )

        created_candidates: list[PublicationCandidate] = []
        for rank, row in enumerate(eligible_rows, start=1):
            cand = await self.repo.insert_candidate(
                conn,
                run.id,
                story_id=row["story_id"],
                story_revision_id=row["story_revision_id"],
                deterministic_rank=rank,
                snapshot_features=row.get("snapshot_features", {}),
            )
            created_candidates.append(cand)

        await self.repo.transition_run(conn, run.id, "candidates_sealed")
        return created_candidates
