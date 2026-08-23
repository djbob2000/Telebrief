"""Lightweight optional verification service (Plan 3 Task 9).

Descriptive evidence metadata only (spec §22).
No truth scores and NO publication gates (publication_blocking, eligible, allowed, publishable
are banned by architectural invariant).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

import psycopg

from src.db.uow import DatabaseUnitOfWork
from src.domain.evidence import (
    EvidenceAssessmentRun,
    EvidenceCluster,
    VerificationAssessment,
    VerificationPolicyVersion,
)
from src.repositories.evidence import (
    VerificationAssessmentRepository,
    VerificationPolicyRepository,
)

logger = logging.getLogger(__name__)

DEFAULT_VERIFICATION_PROMPT_VERSION = "verification-prompt-v1"
DEFAULT_VERIFICATION_CONFIG_HASH = "verification-cfg-default"


class VerificationPolicyService:
    """Service to ensure and load active VerificationPolicyVersions."""

    def __init__(self, policies: VerificationPolicyRepository | None = None) -> None:
        self._policies = policies or VerificationPolicyRepository()

    async def ensure_current(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        config_hash: str = DEFAULT_VERIFICATION_CONFIG_HASH,
        prompt_version: str = DEFAULT_VERIFICATION_PROMPT_VERSION,
    ) -> VerificationPolicyVersion:
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


@dataclass(frozen=True)
class VerificationRunResult:
    """Outcome of verifying an evidence assessment run."""

    run: EvidenceAssessmentRun | None = None
    persisted_count: int = 0
    degraded: str | None = None


class VerificationService:
    """Application service for lightweight evidence verification."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork | None = None,
        provider: Any | None = None,
        policies: VerificationPolicyRepository | None = None,
        assessments: VerificationAssessmentRepository | None = None,
        policy_service: VerificationPolicyService | None = None,
    ) -> None:
        self.uow = uow
        self.provider = provider
        self._policies = policies or VerificationPolicyRepository()
        self._assessments = assessments or VerificationAssessmentRepository()
        self._policy_service = policy_service or VerificationPolicyService(self._policies)

    async def assess(
        self,
        *,
        run: EvidenceAssessmentRun,
        clusters: list[EvidenceCluster],
        policy_id: int | None = None,
    ) -> list[VerificationAssessment]:
        if policy_id is None:
            # Verification rows are policy-bound; guessing an id would corrupt
            # the audit trail. Callers resolve the policy first.
            raise ValueError("verification assess() requires an explicit policy_id")
        active_policy_id = policy_id
        created_assessments: list[VerificationAssessment] = []
        now = dt.datetime.now(dt.timezone.utc)

        for cluster in clusters:
            state, risk_level, reason = self._evaluate_cluster(cluster)

            if self.provider is not None:
                try:
                    ai_state, ai_risk, ai_reason = await self._evaluate_with_provider(cluster)
                    if ai_state:
                        state = ai_state
                    if ai_risk:
                        risk_level = ai_risk
                    if ai_reason:
                        reason = ai_reason
                except Exception as err:
                    logger.warning(
                        "verification provider failed for cluster %s: %s",
                        cluster.id,
                        err,
                    )

            assessment = VerificationAssessment(
                id=0,
                evidence_assessment_run_id=run.id,
                verification_policy_id=active_policy_id,
                cluster_id=cluster.id,
                state=state,
                risk_level=risk_level,
                reason=reason,
                provider=getattr(self.provider, "name", None),
                model=getattr(self.provider, "model", None),
                created_at=now,
            )
            created_assessments.append(assessment)

        return created_assessments

    async def verify_run(
        self,
        run_id: int,
        policy_id: int | None = None,
    ) -> VerificationRunResult:
        if self.uow is None:
            raise RuntimeError("VerificationService requires uow for verify_run")

        try:
            from src.repositories.evidence import (
                EvidenceAssessmentRunRepository,
                EvidenceClusterRepository,
            )

            async with self.uow.transaction() as conn:
                run = await EvidenceAssessmentRunRepository().get_by_id(conn, run_id)
                if run is None:
                    return VerificationRunResult(degraded="run_not_found")

                active_policy_id = policy_id
                if active_policy_id is None:
                    policy = await self._policy_service.ensure_current(
                        conn, edition_id=run.edition_id
                    )
                    active_policy_id = policy.id

                existing_count = await self._assessments.count_for_run(
                    conn, run_id=run_id, policy_id=active_policy_id
                )
                if existing_count > 0:
                    return VerificationRunResult(run=run, persisted_count=0, degraded=None)

                clusters = await EvidenceClusterRepository().list_clusters_for_run(conn, run.id)
                assessments_to_insert = await self.assess(
                    run=run, clusters=clusters, policy_id=active_policy_id
                )
                await self._assessments.insert_assessments(conn, assessments=assessments_to_insert)
                return VerificationRunResult(
                    run=run, persisted_count=len(assessments_to_insert), degraded=None
                )
        except Exception as err:
            # Fail-open keeps the pipeline permissive, but the traceback must
            # surface: a swallowed programming error would otherwise look like
            # routine provider unavailability.
            logger.warning("verification unavailable for run %s: %s", run_id, err, exc_info=True)
            return VerificationRunResult(degraded="verification_unavailable")

    def _evaluate_cluster(self, cluster: EvidenceCluster) -> tuple[str, str | None, str]:
        """Heuristic rule-based verification state and risk assessment."""
        if cluster.contradicting_claims > 0:
            return "disputed", "medium", "Обнаружены противоречивые свидетельства в кластере"
        if cluster.supporting_claims >= 2:
            return (
                "corroborated",
                "low",
                f"Подтверждено {cluster.supporting_claims} свидетельствами",
            )
        if cluster.supporting_claims == 1:
            return "reported", "low", "Единичное свидетельство (первичное сообщение)"
        return "reported", None, "Кластер без подтверждающих или опровергающих свидетельств"

    async def _evaluate_with_provider(
        self, cluster: EvidenceCluster
    ) -> tuple[str | None, str | None, str | None]:
        if self.provider is None or not hasattr(self.provider, "chat_completion"):
            return None, None, None
        resp = await self.provider.chat_completion(
            messages=[{"role": "user", "content": f"Assess evidence: {cluster.summary}"}]
        )
        return None, None, str(resp)
