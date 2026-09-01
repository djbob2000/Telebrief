"""Article finalizer managing single-call validation, deterministic recovery, and claim tracing."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_coverage_diagnostics import (
    ArticleCoverageDiagnostics,
    diagnose_article_coverage,
)
from src.publication.article_length import ArticleLengthProfile
from src.publication.article_models import StructuredArticleDraft
from src.publication.article_recovery import ArticleDeterministicComposer
from src.publication.article_trace import (
    ArticleClaimTraceUnit,
    build_article_claim_trace,
)
from src.publication.article_validator import validate_article_draft
from src.publication.errors import ArticleFinalizationInvariantError

logger = logging.getLogger(__name__)


class GenerationAttemptObserver(Protocol):
    async def attempt_started(self, kind: str, **kwargs: Any) -> int: ...
    async def attempt_finished(self, attempt_id: int, status: str, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class ArticleFinalizationResult:
    draft: StructuredArticleDraft
    claim_trace: tuple[ArticleClaimTraceUnit, ...]
    writer_status: Literal["passed", "rejected", "failed"]
    recovery_mode: Literal["none", "supplement", "full_fallback"]
    ai_covered_story_ids: tuple[str, ...]
    supplemented_story_ids: tuple[str, ...]
    final_covered_story_ids: tuple[str, ...]
    metadata: dict[str, Any]


def _build_final_metadata(
    *,
    winning_kind: str,
    writer_status: str,
    recovery_mode: str,
    coverage_plan: ArticleCoveragePlan,
    ai_covered_story_ids: Sequence[str],
    supplemented_story_ids: Sequence[str],
    final_covered_story_ids: Sequence[str],
    ai_diag: ArticleCoverageDiagnostics | None,
    final_diag: ArticleCoverageDiagnostics,
    trace: Sequence[ArticleClaimTraceUnit],
) -> dict[str, Any]:
    planned_story_count = len(coverage_plan.story_ids)
    ai_story_coverage = (
        len(ai_covered_story_ids) / planned_story_count if planned_story_count else 1.0
    )
    origin_counts = Counter(unit.generation_origin for unit in trace)
    trace_meta = [
        {
            "unit_id": unit.unit_id,
            "support_ids": list(unit.support_ids),
            "source_refs": list(unit.source_refs),
            "fragment_ids": list(unit.fragment_ids),
            "source_item_ids": list(unit.source_item_ids),
            "temporal_roles": list(unit.temporal_roles),
            "generation_origin": unit.generation_origin,
            "claim_atoms": [
                {
                    "text": atom.text,
                    "support_ids": list(atom.support_ids),
                    "temporal_roles": list(atom.temporal_roles),
                }
                for atom in unit.claim_atoms
            ],
        }
        for unit in trace
    ]
    return {
        "status": "writer_success" if writer_status == "passed" else "fallback_success",
        "winning_kind": winning_kind,
        "writer_status": writer_status,
        "recovery_mode": recovery_mode,
        "planned_story_count": planned_story_count,
        "ai_covered_story_count": len(ai_covered_story_ids),
        "supplemented_story_count": len(supplemented_story_ids),
        "final_covered_story_count": len(final_covered_story_ids),
        "ai_story_coverage": ai_story_coverage,
        "final_story_coverage": final_diag.story_coverage,
        "planned_detail_support_count": final_diag.planned_detail_support_count,
        "ai_detail_support_coverage": ai_diag.detail_support_coverage
        if ai_diag is not None
        else 0.0,
        "final_detail_support_coverage": final_diag.detail_support_coverage,
        "coverage": {
            "planned_story_count": final_diag.planned_story_count,
            "covered_story_count": final_diag.covered_story_count,
            "uncovered_story_ids": list(final_diag.uncovered_story_ids),
            "develop_story_coverage": final_diag.develop_story_coverage,
            "weave_story_coverage": final_diag.weave_story_coverage,
            "brief_story_coverage": final_diag.brief_story_coverage,
            "planned_detail_support_count": final_diag.planned_detail_support_count,
            "covered_detail_support_count": final_diag.covered_detail_support_count,
            "uncovered_detail_support_ids": list(final_diag.uncovered_detail_support_ids),
            "detail_support_coverage": final_diag.detail_support_coverage,
            "leaked_contact_payloads": list(final_diag.leaked_contact_payloads),
        },
        "generation_origin_counts": {
            "AI": origin_counts.get("AI", 0),
            "SUPPLEMENT": origin_counts.get("SUPPLEMENT", 0),
            "FALLBACK": origin_counts.get("FALLBACK", 0),
        },
        "validation": {
            "is_valid": True,
            "unsupported_claim_count": 0,
            "unit_count": len(trace),
        },
        "unsupported_final_claim_count": 0,
        "leaked_directory_payload_count": len(final_diag.leaked_contact_payloads),
        "claim_trace": trace_meta,
    }


class ArticleFinalizer:
    """State machine that finalizes the single writer output and performs deterministic recovery."""

    def __init__(self, composer: ArticleDeterministicComposer | None = None) -> None:
        self.composer = composer or ArticleDeterministicComposer()

    async def finalize(
        self,
        *,
        writer_draft: StructuredArticleDraft | None,
        writer_error: Exception | None,
        writer_attempt_id: int,
        context: ArticleEditorialContext,
        coverage_plan: ArticleCoveragePlan,
        editorial_config: PublicationEditorialConfig,
        length_profile: ArticleLengthProfile | None = None,
        attempt_observer: GenerationAttemptObserver | None = None,
    ) -> ArticleFinalizationResult:
        """Validate writer output, trigger recovery if needed, and assert final invariants."""
        # 1. Handle writer failure / error
        if writer_error is not None or writer_draft is None:
            if attempt_observer:
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_writer_rejected",
                    metadata={
                        "writer_status": "failed",
                        "error": str(writer_error) if writer_error else "empty writer response",
                    },
                )
            return await self._run_full_fallback(
                writer_status="failed",
                ai_diag=None,
                ai_covered_story_ids=(),
                context=context,
                coverage_plan=coverage_plan,
                editorial_config=editorial_config,
                length_profile=length_profile,
                attempt_observer=attempt_observer,
            )

        # 2. Validate writer draft
        writer_validation = validate_article_draft(
            writer_draft,
            context,
            config=editorial_config,
            length_profile=length_profile,
        )

        if not writer_validation.is_valid:
            logger.info(
                "Article writer draft failed validation: %s",
                list(writer_validation.violations),
            )
            if attempt_observer:
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_validation_rejected",
                    metadata={
                        "writer_status": "rejected",
                        "violations": list(writer_validation.violations),
                    },
                )
            return await self._run_full_fallback(
                writer_status="rejected",
                ai_diag=None,
                ai_covered_story_ids=(),
                context=context,
                coverage_plan=coverage_plan,
                editorial_config=editorial_config,
                length_profile=length_profile,
                attempt_observer=attempt_observer,
            )

        # 3. Writer draft is valid; diagnose its coverage
        ai_diag = diagnose_article_coverage(writer_draft, coverage_plan)
        ai_covered = tuple(ai_diag.covered_story_ids)

        if set(ai_covered) == set(coverage_plan.story_ids):
            # Complete AI coverage!
            trace = build_article_claim_trace(writer_draft, context)
            final_diag = ai_diag
            meta = _build_final_metadata(
                winning_kind="event_article_writer",
                writer_status="passed",
                recovery_mode="none",
                coverage_plan=coverage_plan,
                ai_covered_story_ids=ai_covered,
                supplemented_story_ids=(),
                final_covered_story_ids=ai_covered,
                ai_diag=ai_diag,
                final_diag=final_diag,
                trace=trace,
            )
            if attempt_observer:
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="succeeded",
                    metadata=meta,
                )
            return ArticleFinalizationResult(
                draft=writer_draft,
                claim_trace=trace,
                writer_status="passed",
                recovery_mode="none",
                ai_covered_story_ids=ai_covered,
                supplemented_story_ids=(),
                final_covered_story_ids=ai_covered,
                metadata=meta,
            )

        # 4. Safe incomplete writer draft: attempt deterministic supplement
        if attempt_observer:
            await attempt_observer.attempt_finished(
                writer_attempt_id,
                status="succeeded",
                metadata={
                    "writer_status": "passed",
                    "ai_covered_story_ids": list(ai_covered),
                    "ai_story_coverage": ai_diag.story_coverage,
                },
            )

        supp_attempt_id = 0
        if attempt_observer:
            supp_attempt_id = await attempt_observer.attempt_started("deterministic_supplement")

        try:
            supplemented = self.composer.supplement_safe_draft(
                writer_draft,
                ai_diag.uncovered_story_ids,
                context,
                coverage_plan,
            )
            supp_validation = validate_article_draft(
                supplemented,
                context,
                config=editorial_config,
                length_profile=length_profile,
            )
            if not supp_validation.is_valid:
                raise ValueError(
                    f"Supplemented draft failed validation: {list(supp_validation.violations)}"
                )

            trace = build_article_claim_trace(supplemented, context)
            final_diag = diagnose_article_coverage(supplemented, coverage_plan)
            final_covered = tuple(final_diag.covered_story_ids)

            if (
                set(final_covered) != set(coverage_plan.story_ids)
                or final_diag.story_coverage != 1.0
            ):
                raise ValueError(
                    f"Supplemented draft coverage incomplete: {final_covered} vs {coverage_plan.story_ids}"
                )

            supplemented_story_ids = tuple(
                sid for sid in coverage_plan.story_ids if sid not in ai_covered
            )
            meta = _build_final_metadata(
                winning_kind="event_article_writer_with_supplement",
                writer_status="passed",
                recovery_mode="supplement",
                coverage_plan=coverage_plan,
                ai_covered_story_ids=ai_covered,
                supplemented_story_ids=supplemented_story_ids,
                final_covered_story_ids=final_covered,
                ai_diag=ai_diag,
                final_diag=final_diag,
                trace=trace,
            )
            if attempt_observer:
                await attempt_observer.attempt_finished(
                    supp_attempt_id,
                    status="succeeded",
                    metadata=meta,
                )
            return ArticleFinalizationResult(
                draft=supplemented,
                claim_trace=trace,
                writer_status="passed",
                recovery_mode="supplement",
                ai_covered_story_ids=ai_covered,
                supplemented_story_ids=supplemented_story_ids,
                final_covered_story_ids=final_covered,
                metadata=meta,
            )
        except Exception as exc:
            logger.warning("Deterministic supplement failed, escalating to full fallback: %s", exc)
            if attempt_observer and supp_attempt_id:
                await attempt_observer.attempt_finished(
                    supp_attempt_id,
                    status="failed",
                    metadata={"error": str(exc)},
                )
            return await self._run_full_fallback(
                writer_status="passed",
                ai_diag=ai_diag,
                ai_covered_story_ids=ai_covered,
                context=context,
                coverage_plan=coverage_plan,
                editorial_config=editorial_config,
                length_profile=length_profile,
                attempt_observer=attempt_observer,
            )

    async def _run_full_fallback(
        self,
        *,
        writer_status: Literal["passed", "rejected", "failed"],
        ai_diag: ArticleCoverageDiagnostics | None,
        ai_covered_story_ids: tuple[str, ...],
        context: ArticleEditorialContext,
        coverage_plan: ArticleCoveragePlan,
        editorial_config: PublicationEditorialConfig,
        length_profile: ArticleLengthProfile | None,
        attempt_observer: GenerationAttemptObserver | None,
    ) -> ArticleFinalizationResult:
        fb_attempt_id = 0
        if attempt_observer:
            fb_attempt_id = await attempt_observer.attempt_started("deterministic_fallback")

        try:
            fallback = self.composer.render_full_fallback(
                context, coverage_plan, max_sections=editorial_config.article_max_sections
            )
            fb_validation = validate_article_draft(
                fallback,
                context,
                config=editorial_config,
                length_profile=length_profile,
            )
            if not fb_validation.is_valid:
                raise ArticleFinalizationInvariantError(
                    f"Deterministic fallback failed validation: {list(fb_validation.violations)}"
                )

            trace = build_article_claim_trace(fallback, context)
            final_diag = diagnose_article_coverage(fallback, coverage_plan)
            final_covered = tuple(final_diag.covered_story_ids)

            if (
                set(final_covered) != set(coverage_plan.story_ids)
                or final_diag.story_coverage != 1.0
            ):
                raise ArticleFinalizationInvariantError(
                    f"Deterministic fallback coverage incomplete: {final_covered} vs {coverage_plan.story_ids}"
                )

            meta = _build_final_metadata(
                winning_kind="event_article_deterministic_fallback",
                writer_status=writer_status,
                recovery_mode="full_fallback",
                coverage_plan=coverage_plan,
                ai_covered_story_ids=ai_covered_story_ids,
                supplemented_story_ids=(),
                final_covered_story_ids=final_covered,
                ai_diag=ai_diag,
                final_diag=final_diag,
                trace=trace,
            )
            if attempt_observer:
                await attempt_observer.attempt_finished(
                    fb_attempt_id,
                    status="succeeded",
                    metadata=meta,
                )
            return ArticleFinalizationResult(
                draft=fallback,
                claim_trace=trace,
                writer_status=writer_status,
                recovery_mode="full_fallback",
                ai_covered_story_ids=ai_covered_story_ids,
                supplemented_story_ids=(),
                final_covered_story_ids=final_covered,
                metadata=meta,
            )
        except Exception as exc:
            if attempt_observer and fb_attempt_id:
                await attempt_observer.attempt_finished(
                    fb_attempt_id,
                    status="failed",
                    metadata={"error": str(exc)},
                )
            if isinstance(exc, ArticleFinalizationInvariantError):
                raise
            raise ArticleFinalizationInvariantError(
                f"Failed to finalize deterministic article fallback: {exc}"
            ) from exc
