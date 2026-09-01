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

        # Defense-in-depth: audit candidates before sealing
        policy_triage_version: str | None = None
        policy_scope_version: str | None = None
        policy_scope_hash: str | None = None
        if run.eligibility_policy_id is not None:
            cur = await conn.execute(
                """
                SELECT config->>'triage_version',
                       config->>'scope_version',
                       config->>'scope_config_hash'
                FROM eligibility_policy_versions WHERE id = %s
                """,
                (run.eligibility_policy_id,),
            )
            p_row = await cur.fetchone()
            if p_row:
                policy_triage_version = p_row[0]
                policy_scope_version = p_row[1]
                policy_scope_hash = p_row[2]

        ef_story_ids = [
            r["story_id"] for r in eligible_rows if r.get("knowledge_source") == "event_first"
        ]
        if ef_story_ids:
            cur = await conn.execute(
                """
                SELECT sc.story_id,
                       setd.triage_version,
                       setd.scope_config_hash,
                       setd.retention,
                       sesd.scope_version,
                       sesd.scope_config_hash
                FROM story_cluster_state sc
                JOIN story_edition_scope_decisions sesd
                  ON sesd.story_id = sc.story_id
                 AND sesd.latest_assignment_id = sc.latest_assignment_id
                JOIN story_event_triage_decisions setd
                  ON setd.story_id = sc.story_id
                 AND setd.latest_assignment_id = sc.latest_assignment_id
                WHERE sc.story_id = ANY(%s)
                """,
                (ef_story_ids,),
            )
            triage_map = {row[0]: row for row in await cur.fetchall()}
            for row in eligible_rows:
                if row.get("knowledge_source") != "event_first":
                    continue
                sid = row["story_id"]
                if sid not in triage_map:
                    raise ValueError(
                        f"Invariant violation: event_first story {sid} has no scope/triage decisions"
                    )
                t_row = triage_map[sid]
                t_ver, t_shash, ret, s_ver, s_shash = (
                    t_row[1],
                    t_row[2],
                    t_row[3],
                    t_row[4],
                    t_row[5],
                )

                if ret != "KEEP":
                    raise ValueError(
                        f"Invariant violation: candidate story {sid} has non-KEEP retention: {ret!r}"
                    )
                if policy_triage_version and t_ver != policy_triage_version:
                    raise ValueError(
                        f"Invariant violation: candidate story {sid} triage_version {t_ver!r} != policy {policy_triage_version!r}"
                    )
                if policy_scope_version and s_ver != policy_scope_version:
                    raise ValueError(
                        f"Invariant violation: candidate story {sid} scope_version {s_ver!r} != policy {policy_scope_version!r}"
                    )
                if policy_scope_hash and (
                    t_shash != policy_scope_hash or s_shash != policy_scope_hash
                ):
                    raise ValueError(
                        f"Invariant violation: candidate story {sid} scope hash != policy {policy_scope_hash!r}"
                    )

                payload = row.get("event_payload")
                if payload and isinstance(payload, dict):
                    ev_items = payload.get("evidence_items")
                    if isinstance(ev_items, list) and ev_items:
                        has_publish = any(
                            isinstance(ev, dict) and ev.get("publication_use") == "PUBLISH"
                            for ev in ev_items
                        )
                        if not has_publish:
                            raise ValueError(
                                f"Invariant violation: candidate story {sid} has 0 PUBLISH evidence items"
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
