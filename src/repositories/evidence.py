"""Evidence and verification repository implementations (Plan 3 Task 9)."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from typing import Any

import psycopg

from src.domain.evidence import (
    EvidenceAssessmentPolicyVersion,
    EvidenceAssessmentRun,
    EvidenceCluster,
    EvidenceClusterMember,
    EvidenceClusterProposal,
    VerificationAssessment,
    VerificationPolicyVersion,
)


class EvidencePolicyRepository:
    """Repository for evidence_assessment_policy_versions."""

    async def insert(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        version: int,
        config_hash: str,
        prompt_version: str,
    ) -> EvidenceAssessmentPolicyVersion:
        cursor = await conn.execute(
            """
            INSERT INTO evidence_assessment_policy_versions (
                edition_id, version, config_hash, prompt_version
            ) VALUES (%s, %s, %s, %s)
            RETURNING id, edition_id, version, config_hash, prompt_version, created_at
            """,
            (edition_id, version, config_hash, prompt_version),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("failed to insert evidence_assessment_policy_versions")
        return EvidenceAssessmentPolicyVersion.from_row(row)

    async def get_latest(
        self, conn: psycopg.AsyncConnection, edition_id: int
    ) -> EvidenceAssessmentPolicyVersion | None:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM evidence_assessment_policy_versions
            WHERE edition_id = %s
            ORDER BY version DESC
            LIMIT 1
            """,
            (edition_id,),
        )
        row = await cursor.fetchone()
        return EvidenceAssessmentPolicyVersion.from_row(row) if row else None

    async def get_by_id(
        self, conn: psycopg.AsyncConnection, policy_id: int
    ) -> EvidenceAssessmentPolicyVersion | None:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM evidence_assessment_policy_versions
            WHERE id = %s
            """,
            (policy_id,),
        )
        row = await cursor.fetchone()
        return EvidenceAssessmentPolicyVersion.from_row(row) if row else None

    async def list_for_edition(
        self, conn: psycopg.AsyncConnection, edition_id: int
    ) -> list[EvidenceAssessmentPolicyVersion]:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM evidence_assessment_policy_versions
            WHERE edition_id = %s
            ORDER BY version ASC
            """,
            (edition_id,),
        )
        return [EvidenceAssessmentPolicyVersion.from_row(row) for row in await cursor.fetchall()]


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


class EvidenceAssessmentRunRepository:
    """Repository for evidence_assessment_runs and exact claim frozen sets."""

    async def get_canonical_success(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_revision_id: int,
        policy_id: int,
        input_hash: str,
    ) -> EvidenceAssessmentRun | None:
        cursor = await conn.execute(
            """
            SELECT id, story_id, story_revision_id, edition_id, policy_id,
                   input_hash, started_at, completed_at, status, error_kind, metadata
            FROM evidence_assessment_runs
            WHERE story_revision_id = %s AND policy_id = %s AND input_hash = %s AND status = 'succeeded'
            LIMIT 1
            """,
            (story_revision_id, policy_id, input_hash),
        )
        row = await cursor.fetchone()
        return EvidenceAssessmentRun.from_row(row) if row else None

    async def insert_running(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        story_revision_id: int,
        edition_id: int,
        policy_id: int,
        input_hash: str,
        started_at: dt.datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceAssessmentRun:
        started = started_at or dt.datetime.now(dt.timezone.utc)
        meta_json = json.dumps(metadata or {})
        cursor = await conn.execute(
            """
            INSERT INTO evidence_assessment_runs (
                story_id, story_revision_id, edition_id, policy_id, input_hash,
                started_at, status, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s::jsonb)
            RETURNING id, story_id, story_revision_id, edition_id, policy_id,
                      input_hash, started_at, completed_at, status, error_kind, metadata
            """,
            (story_id, story_revision_id, edition_id, policy_id, input_hash, started, meta_json),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("failed to insert evidence_assessment_runs")
        return EvidenceAssessmentRun.from_row(row)

    async def freeze_run_claims(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        claim_ids: Sequence[int],
    ) -> None:
        if not claim_ids:
            return
        unique_ids = sorted(set(claim_ids))
        for cid in unique_ids:
            await conn.execute(
                """
                INSERT INTO evidence_assessment_run_claims (run_id, claim_id)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
                """,
                (run_id, cid),
            )

    async def mark_succeeded(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        completed_at: dt.datetime | None = None,
    ) -> bool:
        completed = completed_at or dt.datetime.now(dt.timezone.utc)
        cursor = await conn.execute(
            """
            UPDATE evidence_assessment_runs
            SET status = 'succeeded', completed_at = %s, error_kind = NULL
            WHERE id = %s AND status = 'running'
            """,
            (completed, run_id),
        )
        return cursor.rowcount > 0

    async def mark_failed(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        *,
        error_kind: str | None = None,
        completed_at: dt.datetime | None = None,
    ) -> bool:
        completed = completed_at or dt.datetime.now(dt.timezone.utc)
        cursor = await conn.execute(
            """
            UPDATE evidence_assessment_runs
            SET status = 'failed', completed_at = %s, error_kind = %s
            WHERE id = %s AND status = 'running'
            """,
            (completed, error_kind, run_id),
        )
        return cursor.rowcount > 0

    async def mark_unavailable(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        *,
        error_kind: str | None = None,
        completed_at: dt.datetime | None = None,
    ) -> bool:
        completed = completed_at or dt.datetime.now(dt.timezone.utc)
        cursor = await conn.execute(
            """
            UPDATE evidence_assessment_runs
            SET status = 'unavailable', completed_at = %s, error_kind = %s
            WHERE id = %s AND status = 'running'
            """,
            (completed, error_kind, run_id),
        )
        return cursor.rowcount > 0

    async def get_by_id(
        self, conn: psycopg.AsyncConnection, run_id: int
    ) -> EvidenceAssessmentRun | None:
        cursor = await conn.execute(
            """
            SELECT id, story_id, story_revision_id, edition_id, policy_id,
                   input_hash, started_at, completed_at, status, error_kind, metadata
            FROM evidence_assessment_runs
            WHERE id = %s
            """,
            (run_id,),
        )
        row = await cursor.fetchone()
        return EvidenceAssessmentRun.from_row(row) if row else None

    async def list_run_claim_ids(self, conn: psycopg.AsyncConnection, run_id: int) -> list[int]:
        cursor = await conn.execute(
            """
            SELECT claim_id FROM evidence_assessment_run_claims
            WHERE run_id = %s
            ORDER BY claim_id ASC
            """,
            (run_id,),
        )
        return [row[0] for row in await cursor.fetchall()]


class EvidenceClusterRepository:
    """Repository for evidence_clusters and evidence_cluster_members."""

    async def insert_clusters(
        self,
        conn: psycopg.AsyncConnection,
        *,
        run_id: int,
        clusters: Sequence[EvidenceClusterProposal],
    ) -> list[EvidenceCluster]:
        created: list[EvidenceCluster] = []
        for prop in clusters:
            supporting = prop.supporting_claims
            contradicting = prop.contradicting_claims
            if supporting == 0 and contradicting == 0 and prop.members:
                supporting = sum(1 for m in prop.members if m.stance == "SUPPORTS")
                contradicting = sum(1 for m in prop.members if m.stance == "CONTRADICTS")
            unique_sources = prop.unique_sources or max(supporting + contradicting, 1)
            groups = prop.estimated_independent_source_groups or unique_sources

            meta_json = json.dumps(prop.metadata or {})
            cursor = await conn.execute(
                """
                INSERT INTO evidence_clusters (
                    run_id, supersedes_cluster_id, label, summary,
                    supporting_claims, contradicting_claims, unique_sources,
                    estimated_independent_source_groups, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id, run_id, supersedes_cluster_id, label, summary,
                          supporting_claims, contradicting_claims, unique_sources,
                          estimated_independent_source_groups, metadata, created_at
                """,
                (
                    run_id,
                    prop.supersedes_cluster_id,
                    prop.label,
                    prop.summary,
                    supporting,
                    contradicting,
                    unique_sources,
                    groups,
                    meta_json,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError("failed to insert evidence_clusters")
            cluster = EvidenceCluster.from_row(row)
            created.append(cluster)

            if prop.members:
                for m in prop.members:
                    await conn.execute(
                        """
                        INSERT INTO evidence_cluster_members (cluster_id, claim_id, stance)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (cluster_id, claim_id) DO NOTHING
                        """,
                        (cluster.id, m.claim_id, m.stance),
                    )
        return created

    async def list_clusters_for_run(
        self, conn: psycopg.AsyncConnection, run_id: int
    ) -> list[EvidenceCluster]:
        cursor = await conn.execute(
            """
            SELECT id, run_id, supersedes_cluster_id, label, summary,
                   supporting_claims, contradicting_claims, unique_sources,
                   estimated_independent_source_groups, metadata, created_at
            FROM evidence_clusters
            WHERE run_id = %s
            ORDER BY id ASC
            """,
            (run_id,),
        )
        return [EvidenceCluster.from_row(row) for row in await cursor.fetchall()]

    async def list_cluster_members(
        self, conn: psycopg.AsyncConnection, cluster_id: int
    ) -> list[EvidenceClusterMember]:
        cursor = await conn.execute(
            """
            SELECT cluster_id, claim_id, stance
            FROM evidence_cluster_members
            WHERE cluster_id = %s
            ORDER BY claim_id ASC
            """,
            (cluster_id,),
        )
        return [EvidenceClusterMember.from_row(row) for row in await cursor.fetchall()]


class VerificationPolicyRepository:
    """Repository for verification_policy_versions."""

    async def insert(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        version: int,
        config_hash: str,
        prompt_version: str,
    ) -> VerificationPolicyVersion:
        cursor = await conn.execute(
            """
            INSERT INTO verification_policy_versions (
                edition_id, version, config_hash, prompt_version
            ) VALUES (%s, %s, %s, %s)
            RETURNING id, edition_id, version, config_hash, prompt_version, created_at
            """,
            (edition_id, version, config_hash, prompt_version),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("failed to insert verification_policy_versions")
        return VerificationPolicyVersion.from_row(row)

    async def get_latest(
        self, conn: psycopg.AsyncConnection, edition_id: int
    ) -> VerificationPolicyVersion | None:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM verification_policy_versions
            WHERE edition_id = %s
            ORDER BY version DESC
            LIMIT 1
            """,
            (edition_id,),
        )
        row = await cursor.fetchone()
        return VerificationPolicyVersion.from_row(row) if row else None

    async def get_by_id(
        self, conn: psycopg.AsyncConnection, policy_id: int
    ) -> VerificationPolicyVersion | None:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, version, config_hash, prompt_version, created_at
            FROM verification_policy_versions
            WHERE id = %s
            """,
            (policy_id,),
        )
        row = await cursor.fetchone()
        return VerificationPolicyVersion.from_row(row) if row else None


class VerificationAssessmentRepository:
    """Repository for verification_assessments."""

    async def insert_assessment(
        self,
        conn: psycopg.AsyncConnection,
        *,
        evidence_assessment_run_id: int,
        verification_policy_id: int,
        cluster_id: int | None,
        state: str,
        risk_level: str | None = None,
        reason: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> VerificationAssessment:
        meta_json = json.dumps(metadata or {})
        cursor = await conn.execute(
            """
            INSERT INTO verification_assessments (
                evidence_assessment_run_id, verification_policy_id, cluster_id,
                state, risk_level, reason, provider, model, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id, evidence_assessment_run_id, verification_policy_id, cluster_id,
                      state, risk_level, reason, provider, model, metadata, created_at
            """,
            (
                evidence_assessment_run_id,
                verification_policy_id,
                cluster_id,
                state,
                risk_level,
                reason,
                provider,
                model,
                meta_json,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("failed to insert verification_assessments")
        return VerificationAssessment.from_row(row)

    async def count_for_run(
        self, conn: psycopg.AsyncConnection, *, run_id: int, policy_id: int | None = None
    ) -> int:
        if policy_id is not None:
            cursor = await conn.execute(
                """
                SELECT count(*) FROM verification_assessments
                WHERE evidence_assessment_run_id = %s AND verification_policy_id = %s
                """,
                (run_id, policy_id),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT count(*) FROM verification_assessments
                WHERE evidence_assessment_run_id = %s
                """,
                (run_id,),
            )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def insert_assessments(
        self,
        conn: psycopg.AsyncConnection,
        *,
        assessments: Sequence[VerificationAssessment],
    ) -> list[VerificationAssessment]:
        created: list[VerificationAssessment] = []
        for a in assessments:
            res = await self.insert_assessment(
                conn,
                evidence_assessment_run_id=a.evidence_assessment_run_id,
                verification_policy_id=a.verification_policy_id,
                cluster_id=a.cluster_id,
                state=a.state,
                risk_level=a.risk_level,
                reason=a.reason,
                provider=a.provider,
                model=a.model,
                metadata=a.metadata,
            )
            created.append(res)
        return created

    async def list_for_run(
        self, conn: psycopg.AsyncConnection, run_id: int
    ) -> list[VerificationAssessment]:
        cursor = await conn.execute(
            """
            SELECT id, evidence_assessment_run_id, verification_policy_id, cluster_id,
                   state, risk_level, reason, provider, model, metadata, created_at
            FROM verification_assessments
            WHERE evidence_assessment_run_id = %s
            ORDER BY id ASC
            """,
            (run_id,),
        )
        return [VerificationAssessment.from_row(row) for row in await cursor.fetchall()]


# Compatibility aliases
EvidenceAssessmentPolicyRepository = EvidencePolicyRepository
