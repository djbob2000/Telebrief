"""Evidence assessment policy and clustering services (Plan 3 Task 9)."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence

import psycopg

from src.db.uow import DatabaseUnitOfWork
from src.domain.claims import Claim, ClaimRelation
from src.domain.evidence import (
    ClusterMemberProposal,
    EvidenceAssessmentPolicyVersion,
    EvidenceAssessmentRun,
    EvidenceClusterProposal,
    hash_sorted_ids,
)
from src.repositories.claims import ClaimRepository
from src.repositories.evidence import (
    EvidenceAssessmentRunRepository,
    EvidenceClusterRepository,
    EvidencePolicyRepository,
)
from src.repositories.stories import StoryRepository

logger = logging.getLogger(__name__)

DEFAULT_EVIDENCE_PROMPT_VERSION = "evidence-prompt-v1"
DEFAULT_EVIDENCE_CONFIG_HASH = "evidence-cfg-default"


class EvidencePolicyService:
    """Service to ensure and load active EvidenceAssessmentPolicyVersions."""

    def __init__(self, policies: EvidencePolicyRepository | None = None) -> None:
        self._policies = policies or EvidencePolicyRepository()

    async def ensure_current(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        config_hash: str = DEFAULT_EVIDENCE_CONFIG_HASH,
        prompt_version: str = DEFAULT_EVIDENCE_PROMPT_VERSION,
    ) -> EvidenceAssessmentPolicyVersion:
        latest = await self._policies.get_latest(conn, edition_id)
        if (
            latest is not None
            and latest.config_hash == config_hash
            and latest.prompt_version == prompt_version
        ):
            return latest

        next_version = (latest.version + 1) if latest else 1
        return await self._policies.insert(
            conn,
            edition_id=edition_id,
            version=next_version,
            config_hash=config_hash,
            prompt_version=prompt_version,
        )


class EvidenceCorrelator:
    """Deterministic & heuristic correlator for grouping claims into evidence clusters."""

    def correlate(
        self,
        claims: Sequence[Claim],
        relations: Sequence[ClaimRelation] = (),
    ) -> list[EvidenceClusterProposal]:
        if not claims:
            return []

        # Check for contradiction / correction relations
        contradicting_claim_ids: set[int] = set()
        for rel in relations:
            if rel.relation_type in ("CORRECTS", "SUPERSEDES", "RETRACTS", "CONTRADICTS"):
                contradicting_claim_ids.add(rel.from_claim_id)
                contradicting_claim_ids.add(rel.to_claim_id)

        # For simple clustering: if all claims relate to the story, group them into a single primary cluster
        members: list[ClusterMemberProposal] = []
        unique_sources: set[str] = set()
        supporting_count = 0
        contradicting_count = 0

        for claim in claims:
            role = claim.metadata.get("effective_source_role") or "source"
            unique_sources.add(f"{role}_{claim.id}")
            if claim.id in contradicting_claim_ids and len(claims) > 1 and members:
                stance = "CONTRADICTS"
                contradicting_count += 1
            else:
                stance = "SUPPORTS"
                supporting_count += 1
            members.append(ClusterMemberProposal(claim_id=claim.id, stance=stance))

        cluster = EvidenceClusterProposal(
            label=claims[0].assertion_text[:100],
            summary="Сводная кластеризация свидетельств по истории",
            supporting_claims=supporting_count,
            contradicting_claims=contradicting_count,
            unique_sources=len(unique_sources),
            estimated_independent_source_groups=len(unique_sources),
            members=members,
        )
        return [cluster]


class EvidenceAssessmentService:
    """Application service for running and persisting evidence assessments."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        stories: StoryRepository | None = None,
        claims: ClaimRepository | None = None,
        runs: EvidenceAssessmentRunRepository | None = None,
        clusters: EvidenceClusterRepository | None = None,
        correlator: EvidenceCorrelator | None = None,
    ) -> None:
        self.uow = uow
        self._stories = stories or StoryRepository()
        self._claims = claims or ClaimRepository()
        self._runs = runs or EvidenceAssessmentRunRepository()
        self._clusters = clusters or EvidenceClusterRepository()
        self._correlator = correlator or EvidenceCorrelator()

    async def assess(
        self,
        *,
        story_id: int,
        story_revision_id: int,
        policy_id: int,
    ) -> EvidenceAssessmentRun:
        async with self.uow.transaction() as conn:
            claim_ids = await self._stories.list_attached_claim_ids(conn, story_id)
            if not claim_ids:
                raise ValueError(f"story {story_id} has no attached claims")

            input_hash = hash_sorted_ids(claim_ids)
            canonical = await self._runs.get_canonical_success(
                conn,
                story_revision_id=story_revision_id,
                policy_id=policy_id,
                input_hash=input_hash,
            )
            if canonical is not None:
                return canonical

            story = await self._stories.get(conn, story_id)
            if story is None:
                raise ValueError(f"story {story_id} not found")

            run = await self._runs.insert_running(
                conn,
                story_id=story_id,
                story_revision_id=story_revision_id,
                edition_id=story.edition_id,
                policy_id=policy_id,
                input_hash=input_hash,
            )
            await self._runs.freeze_run_claims(conn, run.id, claim_ids)

            claims = await self._claims.get_many(conn, claim_ids)
            relations = await self._claims.list_relations_for_claims(conn, claim_ids)
            cluster_proposals = self._correlator.correlate(claims, relations)

            await self._clusters.insert_clusters(conn, run_id=run.id, clusters=cluster_proposals)
            await self._runs.mark_succeeded(
                conn, run.id, completed_at=dt.datetime.now(dt.timezone.utc)
            )

            # Defer optional verification task
            await self._defer_verification(conn, run.id)

            succeeded_run = await self._runs.get_by_id(conn, run.id)
            if succeeded_run is None:
                raise RuntimeError(
                    f"evidence assessment run {run.id} not found after mark_succeeded"
                )
            return succeeded_run

    async def _defer_verification(self, conn: psycopg.AsyncConnection, run_id: int) -> None:
        try:
            from src.jobs.processing import maybe_verify_evidence

            await maybe_verify_evidence.configure(connection=conn).defer_async(
                evidence_assessment_run_id=run_id
            )
        except Exception as err:
            logger.warning("could not defer verification for run %s: %s", run_id, err)
