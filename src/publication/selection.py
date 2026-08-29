"""AI Editorial selection service over frozen candidate sets (Plan 4 Task 3)."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from src.config_loader import Config
from src.db.uow import DatabaseUnitOfWork
from src.publication.digest_contracts import (
    DIGEST_PUBLICATION_TYPES,
)
from src.publication.models import (
    PublicationCandidate,
    PublicationInput,
    PublicationRun,
    PublicationSelectionDecision,
)
from src.publication.repository import PublicationRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelectionProposal:
    """Proposal from the selector model for one candidate story."""

    story_id: int
    story_revision_id: int
    decision: str  # 'INCLUDE', 'OMIT'
    presentation_intent: str | None = (
        None  # 'lead', 'normal', 'brief', 'unverified_operational', 'follow_up'
    )
    confidence: float | None = None
    reason: str | None = None
    rank: int | None = None
    exclusion_reason: str | None = None
    metadata: dict[str, Any] | None = None


class SelectionModel(Protocol):
    """Protocol for models or heuristics performing editorial selection."""

    async def select_stories(
        self,
        *,
        run: PublicationRun,
        candidates: list[PublicationCandidate],
    ) -> list[SelectionProposal]:
        raise NotImplementedError


class HeuristicSelectionModel:
    """Default rule-based selection model when no AI model is configured."""

    async def select_stories(
        self,
        *,
        run: PublicationRun,
        candidates: list[PublicationCandidate],
    ) -> list[SelectionProposal]:
        proposals: list[SelectionProposal] = []
        for rank, cand in enumerate(candidates, start=1):
            intent = "lead" if rank == 1 else "normal"
            proposals.append(
                SelectionProposal(
                    story_id=cand.story_id,
                    story_revision_id=cand.story_revision_id,
                    decision="INCLUDE",
                    presentation_intent=intent,
                    confidence=0.9,
                    reason="Automatically included by heuristic selection",
                    rank=rank,
                )
            )
        return proposals


class EditorialSelectionService:
    """Service to run editorial selection over sealed candidates and freeze publication inputs."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        repo: PublicationRepository | None = None,
        model: SelectionModel | None = None,
        config: Config | None = None,
    ) -> None:
        self.uow = uow
        self.repo = repo or PublicationRepository()
        if model is not None:
            self.model = model
        else:
            from src.publication.selection_ai import FailOpenSelectionModel

            self.model = FailOpenSelectionModel(config=config)

    async def select(
        self,
        run_id: int,
        *,
        defer_generation: bool = True,
    ) -> list[PublicationInput]:
        async with self.uow.transaction() as conn:
            run = await self.repo.lock_run(conn, run_id)
            if run is None:
                raise ValueError(f"publication run {run_id} not found")

            if run.status != "candidates_sealed":
                if run.status in ("selected_inputs_sealed", "generating", "succeeded"):
                    return await self.repo.load_sealed_inputs(conn, run_id)
                raise RuntimeError(
                    f"cannot select for publication run {run_id} in status '{run.status}' (expected 'candidates_sealed')"
                )

            candidates = await self.repo.load_sealed_candidates(conn, run_id)
            if not candidates:
                # No candidates to select: transition to selected_inputs_sealed with empty inputs
                await self.repo.transition_run(conn, run_id, "selected_inputs_sealed")
                return []

            allowed_keys = {(c.story_id, c.story_revision_id): c for c in candidates}

        # Model call outside transaction
        raw_proposals = await self.model.select_stories(run=run, candidates=candidates)

        # Validate proposals: must only reference candidates in allowed_keys
        validated_proposals: list[tuple[PublicationCandidate, SelectionProposal]] = []
        for prop in raw_proposals:
            cand = allowed_keys.get((prop.story_id, prop.story_revision_id))
            if cand is None:
                logger.warning(
                    "selector proposed unknown story (%s, %s) not in candidates of run %s",
                    prop.story_id,
                    prop.story_revision_id,
                    run_id,
                )
                continue
            validated_proposals.append((cand, prop))

        is_digest = run.publication_type in DIGEST_PUBLICATION_TYPES
        normalized_proposals: list[tuple[PublicationCandidate, SelectionProposal]] = []
        for cand, prop in validated_proposals:
            if is_digest and prop.decision != "INCLUDE":
                meta = dict(prop.metadata or {})
                meta["model_decision"] = prop.decision
                meta["coverage_override"] = True
                intent = prop.presentation_intent or "normal"
                effective_prop = SelectionProposal(
                    story_id=prop.story_id,
                    story_revision_id=prop.story_revision_id,
                    decision="INCLUDE",
                    presentation_intent=intent,
                    confidence=prop.confidence,
                    reason=prop.reason or "Coverage override for digest publication",
                    rank=prop.rank,
                    metadata=meta,
                )
            else:
                effective_prop = prop
            normalized_proposals.append((cand, effective_prop))

        def _sort_key(
            item: tuple[PublicationCandidate, SelectionProposal],
        ) -> tuple[int, int, int]:
            cand, prop = item
            has_valid_rank = isinstance(prop.rank, int) and prop.rank > 0
            rank_val: int = (
                prop.rank if (has_valid_rank and prop.rank is not None) else cand.deterministic_rank
            )
            return (0 if has_valid_rank else 1, rank_val, cand.deterministic_rank)

        included_items = sorted(
            [item for item in normalized_proposals if item[1].decision == "INCLUDE"],
            key=_sort_key,
        )
        omitted_items = [item for item in normalized_proposals if item[1].decision != "INCLUDE"]

        async with self.uow.transaction() as conn:
            # Re-lock run
            locked_run = await self.repo.lock_run(conn, run_id)
            if locked_run is None or locked_run.status != "candidates_sealed":
                return await self.repo.load_sealed_inputs(conn, run_id)

            # Record omitted decisions first
            for cand, prop in omitted_items:
                decision_rec = PublicationSelectionDecision(
                    id=0,
                    publication_run_id=run_id,
                    candidate_id=cand.id,
                    decision=prop.decision,
                    presentation_intent=prop.presentation_intent,
                    confidence=prop.confidence,
                    reason=prop.reason,
                    rank=prop.rank,
                    metadata=prop.metadata or {},
                    created_at=dt.datetime.now(dt.timezone.utc),
                )
                await self.repo.insert_selection_decision(conn, run_id, decision_rec)

            selected_inputs: list[PublicationInput] = []
            include_rank = 1

            # Load eligibility policy config to check excluded_platforms
            cur = await conn.execute(
                "SELECT config->'excluded_platforms' FROM eligibility_policy_versions WHERE id = %s",
                (run.eligibility_policy_id,),
            )
            pol_row = await cur.fetchone()
            excluded_platforms: list[str] = []
            if pol_row is not None and pol_row[0] is not None and isinstance(pol_row[0], list):
                excluded_platforms = [str(p).strip().lower() for p in pol_row[0] if str(p).strip()]

            for cand, prop in included_items:
                decision_rec = PublicationSelectionDecision(
                    id=0,
                    publication_run_id=run_id,
                    candidate_id=cand.id,
                    decision=prop.decision,
                    presentation_intent=prop.presentation_intent,
                    confidence=prop.confidence,
                    reason=prop.reason,
                    rank=prop.rank,
                    metadata=prop.metadata or {},
                    created_at=dt.datetime.now(dt.timezone.utc),
                )
                inserted_decision = await self.repo.insert_selection_decision(
                    conn, run_id, decision_rec
                )

                # Query claims attached <= snapshot_at excluding excluded platforms
                c_cur = await conn.execute(
                    """
                    SELECT sc.claim_id, src.role
                    FROM story_claims sc
                    JOIN claims c ON c.id = sc.claim_id
                    JOIN source_item_revisions sir ON sir.id = c.source_item_revision_id
                    JOIN source_items si ON si.id = sir.source_item_id
                    JOIN sources src ON src.id = si.source_id
                    WHERE sc.story_id = %s
                      AND sc.attached_at <= %s
                      AND c.created_at <= %s
                      AND (cardinality(%s::text[]) = 0 OR src.platform <> ALL(%s::text[]))
                    ORDER BY sc.claim_id ASC
                    """,
                    (
                        cand.story_id,
                        run.snapshot_at,
                        run.snapshot_at,
                        excluded_platforms,
                        excluded_platforms,
                    ),
                )
                c_rows = await c_cur.fetchall()
                claim_ids = [r[0] for r in c_rows]
                claim_roles = {r[0]: r[1] for r in c_rows}

                # Stories with 0 valid claims must never reach publication
                if not claim_ids:
                    continue

                ec_cur = await conn.execute(
                    """
                    SELECT DISTINCT ec.id
                    FROM evidence_clusters ec
                    JOIN evidence_assessment_runs ear ON ear.id = ec.run_id
                    WHERE ear.story_id = %s
                      AND ear.completed_at <= %s
                      AND ear.status = 'succeeded'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM evidence_cluster_members ecm
                          WHERE ecm.cluster_id = ec.id
                            AND ecm.claim_id <> ALL(%s::bigint[])
                      )
                    ORDER BY ec.id ASC
                    """,
                    (cand.story_id, run.snapshot_at, claim_ids),
                )
                evidence_cluster_ids = [r[0] for r in await ec_cur.fetchall()]

                inp = await self.repo.freeze_selected_input(
                    conn,
                    run_id,
                    story_id=cand.story_id,
                    story_revision_id=cand.story_revision_id,
                    selection_decision_id=inserted_decision.id,
                    presentation_intent=prop.presentation_intent,
                    rank=include_rank,
                    claim_ids=claim_ids,
                    claim_roles=claim_roles,
                    evidence_cluster_ids=evidence_cluster_ids,
                )
                selected_inputs.append(inp)
                include_rank += 1

            await self.repo.transition_run(conn, run_id, "selected_inputs_sealed")
            if defer_generation:
                await self._defer_generation(conn, run_id)
            return selected_inputs

    async def _defer_generation(self, conn: psycopg.AsyncConnection, run_id: int) -> None:
        try:
            from src.jobs.publication import generate_publication

            await generate_publication.configure(connection=conn).defer_async(run_id=run_id)
        except Exception as err:
            # Re-raise: rolling back keeps the run from being stranded in
            # selected_inputs_sealed with no generation job queued.
            logger.error("could not defer generate_publication for run %s: %s", run_id, err)
            raise
