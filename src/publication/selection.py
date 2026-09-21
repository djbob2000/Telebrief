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
    HARD_EXCLUSION_REASONS,
)
from src.publication.models import (
    PublicationCandidate,
    PublicationInput,
    PublicationRun,
    PublicationSelectionDecision,
)
from src.publication.policies import (
    ARTICLE_PUBLICATION_TYPES,
    SUPPORTED_SELECTION_SEMANTICS_VERSIONS,
    UnsupportedFrozenSemanticVersion,
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
    """Default rule-based selection model for deterministic / digest selection.

    For digest publications the model preserves the legacy behaviour:
      rank-1 → lead, all others → normal.

    For article publications (triggered when the candidate count exceeds the
    AI selection ceiling) a simple priority overlay is applied:
      - Commercial classifieds → OMIT.
      - Stories flagged publishability='news' OR with ≥3 in-window fragments
        get intent='normal' (eligible for DEVELOP/WEAVE in coverage_plan).
      - rank-1 among normal-eligible stories → intent='lead'.
      - Everything else → intent='brief'.

    This prevents directory-payload or single-comment stories from being
    elevated to DEVELOP merely because they have a low story_id.
    """

    # Minimum in-window fragment count for heuristic 'normal' promotion.
    _ARTICLE_FRAGMENT_THRESHOLD = 3

    async def select_stories(
        self,
        *,
        run: PublicationRun,
        candidates: list[PublicationCandidate],
    ) -> list[SelectionProposal]:
        is_article = run.publication_type in ARTICLE_PUBLICATION_TYPES

        proposals: list[SelectionProposal] = []
        lead_assigned = False
        for rank, cand in enumerate(candidates, start=1):
            features = cand.snapshot_features or {}
            if (
                features.get("is_commercial_classified")
                or features.get("exclusion_reason") == "commercial_classified"
            ):
                proposals.append(
                    SelectionProposal(
                        story_id=cand.story_id,
                        story_revision_id=cand.story_revision_id,
                        decision="OMIT",
                        confidence=1.0,
                        reason="Omitted: commercial classified offer",
                        exclusion_reason="commercial_classified",
                        rank=rank,
                    )
                )
                continue

            if is_article:
                publishability = features.get("publishability")
                urgency = features.get("urgency")
                # Only promote to lead/normal when event analysis explicitly
                # flagged the story as publishability='news' or urgency='high'.
                # Fragment count alone is NOT a sufficient signal — chatty single-
                # word confirmation threads (e.g. "работает", "работает же") can
                # accumulate many fragments without containing substantive news.
                is_substantive = (publishability == "news") or (urgency == "high")

                if is_substantive and not lead_assigned:
                    intent = "lead"
                    lead_assigned = True
                elif is_substantive:
                    intent = "normal"
                else:
                    intent = "brief"
            else:
                intent = "lead" if rank == 1 else "normal"

            proposals.append(
                SelectionProposal(
                    story_id=cand.story_id,
                    story_revision_id=cand.story_revision_id,
                    decision="INCLUDE",
                    presentation_intent=intent,
                    confidence=1.0,
                    reason="Coverage-preserving heuristic selection",
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
        self.config = config
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

            # Validate frozen selection policy semantics
            sel_policy = await self.repo.get_selection_policy_by_id(conn, run.selection_policy_id)
            if sel_policy is not None and sel_policy.config:
                sem_ver = sel_policy.config.get("selection_semantics_version")
                if sem_ver is not None and sem_ver not in SUPPORTED_SELECTION_SEMANTICS_VERSIONS:
                    raise UnsupportedFrozenSemanticVersion(
                        f"Unsupported frozen selection_semantics_version: {sem_ver} (supported: {SUPPORTED_SELECTION_SEMANTICS_VERSIONS})"
                    )

            candidates = await self.repo.load_sealed_candidates(conn, run_id)
            if not candidates:
                # No candidates to select: transition to selected_inputs_sealed with empty inputs
                await self.repo.transition_run(conn, run_id, "selected_inputs_sealed")
                return []

            allowed_keys = {(c.story_id, c.story_revision_id): c for c in candidates}

            # Resolve scope contract for model if supported
            try:
                from src.config_loader import load_config
                from src.processing.edition_scope import (
                    build_scope_contract,
                    resolve_edition_scope,
                )

                cfg = self.config or load_config()
                _slug, scope_cfg = await resolve_edition_scope(conn, cfg, run.edition_id)
                scope_contract = build_scope_contract(scope_cfg)
                if hasattr(self.model, "scope_contract"):
                    self.model.scope_contract = scope_contract
                if hasattr(self.model, "primary") and hasattr(self.model.primary, "scope_contract"):
                    self.model.primary.scope_contract = scope_contract
            except Exception as exc:
                logger.debug("Failed to resolve scope contract for selection: %s", exc)

        is_digest = run.publication_type in DIGEST_PUBLICATION_TYPES
        from src.publication.selection_ai import (
            AIPublicationSelectionModel,
            FailOpenSelectionModel,
        )

        if is_digest and (
            self.model is None
            or isinstance(self.model, (AIPublicationSelectionModel, FailOpenSelectionModel))
        ):
            logger.info("Using fast heuristic selection for digest publication run %s", run_id)
            selector_model: SelectionModel = HeuristicSelectionModel()
        else:
            selector_model = self.model or AIPublicationSelectionModel(config=self.config)

        # Model call outside transaction
        raw_proposals = await selector_model.select_stories(run=run, candidates=candidates)

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
        coverage_preserving = is_digest or run.publication_type in ARTICLE_PUBLICATION_TYPES

        props_by_cand_key = {
            (prop.story_id, prop.story_revision_id): (cand, prop)
            for cand, prop in validated_proposals
        }

        normalized_proposals: list[tuple[PublicationCandidate, SelectionProposal]] = []
        for cand in candidates:
            cand_key = (cand.story_id, cand.story_revision_id)
            cand_and_prop = props_by_cand_key.get(cand_key)
            candidate_prop = cand_and_prop[1] if cand_and_prop is not None else None

            if coverage_preserving:
                default_intent = (
                    "brief" if run.publication_type in ARTICLE_PUBLICATION_TYPES else "normal"
                )
                if candidate_prop is None:
                    effective_prop = SelectionProposal(
                        story_id=cand.story_id,
                        story_revision_id=cand.story_revision_id,
                        decision="INCLUDE",
                        presentation_intent=default_intent,
                        confidence=1.0,
                        reason="Zero-omission overlay: unproposed candidate preserved",
                        rank=cand.deterministic_rank,
                        exclusion_reason=None,
                        metadata={
                            "coverage_override": True,
                            "disagreement_with_gate": "selector_unproposed_candidate",
                        },
                    )
                elif candidate_prop.decision == "OMIT":
                    meta = dict(candidate_prop.metadata or {})
                    is_hard = candidate_prop.exclusion_reason in HARD_EXCLUSION_REASONS
                    disagreement = (
                        "selector_hard_exclusion_override"
                        if is_hard
                        else "selector_omission_override"
                    )
                    meta.update(
                        {
                            "model_decision": "OMIT",
                            "exclusion_reason": candidate_prop.exclusion_reason,
                            "coverage_override": True,
                            "disagreement_with_gate": disagreement,
                        }
                    )
                    effective_prop = SelectionProposal(
                        story_id=candidate_prop.story_id,
                        story_revision_id=candidate_prop.story_revision_id,
                        decision="INCLUDE",
                        presentation_intent=candidate_prop.presentation_intent or default_intent,
                        confidence=candidate_prop.confidence,
                        reason=candidate_prop.reason or "Zero-omission overlay for publication",
                        rank=candidate_prop.rank
                        if candidate_prop.rank is not None
                        else cand.deterministic_rank,
                        exclusion_reason=None,
                        metadata=meta,
                    )
                else:
                    effective_prop = candidate_prop
            else:
                if candidate_prop is None:
                    effective_prop = SelectionProposal(
                        story_id=cand.story_id,
                        story_revision_id=cand.story_revision_id,
                        decision="OMIT",
                        presentation_intent=None,
                        confidence=1.0,
                        reason="Unproposed candidate omitted",
                        rank=cand.deterministic_rank,
                        exclusion_reason="unproposed",
                    )
                else:
                    effective_prop = candidate_prop

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

                fragment_ids: list[int] = []
                if not claim_ids:
                    f_cur = await conn.execute(
                        """
                        SELECT sf.fragment_id
                        FROM story_fragments sf
                        JOIN source_fragments f ON f.id = sf.fragment_id
                        JOIN source_item_revisions sir ON sir.id = f.source_item_revision_id
                        JOIN source_items si ON si.id = sir.source_item_id
                        JOIN sources src ON src.id = si.source_id
                        WHERE sf.story_id = %s
                          AND sf.assigned_at <= %s
                          AND (cardinality(%s::text[]) = 0 OR src.platform <> ALL(%s::text[]))
                        ORDER BY sf.fragment_id ASC
                        """,
                        (cand.story_id, run.snapshot_at, excluded_platforms, excluded_platforms),
                    )
                    fragment_ids = [r[0] for r in await f_cur.fetchall()]

                # Stories with 0 valid claims and 0 valid fragments must never reach publication
                if not claim_ids and not fragment_ids:
                    continue

                evidence_cluster_ids: list[int] = []
                if claim_ids:
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
                    claim_ids=claim_ids if claim_ids else None,
                    claim_roles=claim_roles if claim_ids else None,
                    evidence_cluster_ids=evidence_cluster_ids if evidence_cluster_ids else None,
                    fragment_ids=fragment_ids if fragment_ids else None,
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
