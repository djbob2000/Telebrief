"""Assess the exact digest candidate against frozen evidence and the rendered post."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass

from src.publication.digest_coverage import DigestCoverageTrace, build_digest_coverage_trace
from src.publication.digest_narrative import (
    DigestNarrativeDraft,
    DigestNarrativePlan,
    DigestNarrativeValidationResult,
    sanitize_digest_narrative_draft,
    validate_digest_narrative,
)
from src.publication.digest_presentation import DigestPresentationPlan
from src.publication.digest_quality_diagnostics import DigestQualityAudit, audit_rendered_digest
from src.publication.editorial_adapter import FrozenEditorialInput
from src.publication.evidence import PublicationEvidence
from src.publication.renderers import PublicationDigestRenderer, RenderedDigestArtifact


@dataclass(frozen=True)
class DigestAssessmentContext:
    frozen: FrozenEditorialInput
    plan: DigestNarrativePlan
    presentation_plan: DigestPresentationPlan
    evidence: Mapping[str, PublicationEvidence]
    support_text_by_id: Mapping[str, str]
    allowed_context_terms: tuple[str, ...]
    snapshot_at: dt.datetime
    timezone_name: str
    renderer: PublicationDigestRenderer


@dataclass(frozen=True)
class DigestAssessment:
    draft: DigestNarrativeDraft
    validation: DigestNarrativeValidationResult
    coverage: DigestCoverageTrace
    artifact: RenderedDigestArtifact
    audit: DigestQualityAudit

    @property
    def is_safe(self) -> bool:
        return (
            self.validation.is_valid
            and self.audit.is_publishable
            and self.coverage.story_coverage >= 1.0
            and self.coverage.material_fact_coverage >= 1.0
        )

    def checks(
        self,
    ) -> tuple[
        DigestNarrativeValidationResult,
        DigestCoverageTrace,
        RenderedDigestArtifact,
        DigestQualityAudit,
    ]:
        return self.validation, self.coverage, self.artifact, self.audit

    def checkpoint(
        self,
    ) -> tuple[
        DigestNarrativeDraft,
        DigestNarrativeValidationResult,
        DigestCoverageTrace,
        RenderedDigestArtifact,
        DigestQualityAudit,
    ]:
        return self.draft, *self.checks()


def assess_digest_candidate(
    draft: DigestNarrativeDraft, *, context: DigestAssessmentContext
) -> DigestAssessment:
    candidate = sanitize_digest_narrative_draft(draft)
    validation = validate_digest_narrative(
        candidate,
        context.plan,
        support_text_by_id=context.support_text_by_id,
        situation_plan=context.presentation_plan.city_situation,
        allowed_context_terms=context.allowed_context_terms,
        all_known_draft_supports=list(context.support_text_by_id.values()),
    )
    coverage = build_digest_coverage_trace(context.presentation_plan, candidate, context.plan)
    artifact = context.renderer.render_grouped_digest_artifact(
        context.frozen,
        snapshot_at=context.snapshot_at,
        timezone_name=context.timezone_name,
        narrative_draft=candidate,
        presentation_plan=context.presentation_plan,
    )
    audit = audit_rendered_digest(
        artifact,
        candidate,
        context.evidence,
        context.presentation_plan,
        coverage,
        narrative_validation=validation,
    )
    return DigestAssessment(candidate, validation, coverage, artifact, audit)
