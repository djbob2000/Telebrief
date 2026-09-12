"""Article finalizer managing single-call validation, deterministic recovery, and claim tracing."""

from __future__ import annotations

import logging
import re
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
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
    _split_sentences_safe,
)
from src.publication.article_recovery import ArticleDeterministicComposer
from src.publication.article_trace import (
    ArticleClaimTraceUnit,
    build_article_claim_trace,
)
from src.publication.article_validator import ArticleValidationIssue, validate_article_draft
from src.publication.errors import (
    ArticleFinalizationInvariantError,
    ArticlePublicationRejected,
)

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


def _sanitize_unsupported_quotes(
    draft: StructuredArticleDraft,
    quote_violations: Sequence[ArticleValidationIssue],
    context: ArticleEditorialContext,
) -> StructuredArticleDraft:
    """Deterministically convert unsupported direct quote marks into indirect speech without quotes."""
    bad_spans = {c.raw for iss in quote_violations for c in iss.unsupported_claims if c.raw}
    if not bad_spans:
        return draft

    def _strip_spans(text: str) -> str:
        res = text
        for span in bad_spans:
            inner = span.strip(' «»"“”')
            res = res.replace(span, inner)
        return res

    new_sections = []
    for sec in draft.sections:
        new_heading = _strip_spans(sec.heading)
        new_paras = []
        for p in sec.paragraphs:
            new_p_text = _strip_spans(p.text)
            new_paras.append(
                ArticleParagraph(
                    text=new_p_text,
                    cited_support_ids=p.cited_support_ids,
                    claims=p.claims,
                    generation_origin=p.generation_origin,
                )
            )
        new_sections.append(
            ArticleSection(
                heading=new_heading,
                heading_support_ids=sec.heading_support_ids,
                heading_claims=sec.heading_claims,
                paragraphs=tuple(new_paras),
                heading_generation_origin=sec.heading_generation_origin,
            )
        )

    return StructuredArticleDraft(
        title=_strip_spans(draft.title),
        title_support_ids=draft.title_support_ids,
        lead=_strip_spans(draft.lead),
        lead_support_ids=draft.lead_support_ids,
        sections=tuple(new_sections),
        title_claims=draft.title_claims,
        lead_claims=draft.lead_claims,
        cited_evidence_ids=draft.cited_evidence_ids,
        word_count=draft.word_count,
        title_generation_origin=draft.title_generation_origin,
        lead_generation_origin=draft.lead_generation_origin,
    )


def _prune_unsupported_paragraph_claims(
    draft: StructuredArticleDraft,
    claim_violations: Sequence[ArticleValidationIssue],
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan | None = None,
) -> StructuredArticleDraft:
    """Deterministically prune ungrounded speculative sentences or invalid paragraphs (AGENTS.md 0.7)."""
    bad_claims_by_unit: dict[str, set[str]] = {}
    problematic_units: set[str] = set()
    for iss in claim_violations:
        if not iss.blocking or not iss.unit_id.startswith("P"):
            continue
        problematic_units.add(iss.unit_id)
        bad_text = getattr(iss, "claim_text", None)
        if not bad_text:
            match = re.search(r"claim(?: atom)? '([^']+)'", iss.message)
            if match:
                bad_text = match.group(1)
        if bad_text:
            bad_claims_by_unit.setdefault(iss.unit_id, set()).add(bad_text.strip())
        if getattr(iss, "unsupported_claims", None):
            for uc in iss.unsupported_claims:
                raw = getattr(uc, "raw", None)
                if raw:
                    bad_claims_by_unit.setdefault(iss.unit_id, set()).add(str(raw).strip())

    if not problematic_units:
        return draft

    p_idx = 1
    new_sections: list[ArticleSection] = []
    pruned_count = 0
    for sec in draft.sections:
        new_paragraphs: list[ArticleParagraph] = []
        for p in sec.paragraphs:
            p_id = f"P{p_idx:03d}"
            p_idx += 1
            if p_id not in problematic_units:
                new_paragraphs.append(p)
                continue

            bad_texts = bad_claims_by_unit.get(p_id, set())
            sentences = _split_sentences_safe(p.text)
            if len(sentences) > 1 and bad_texts:
                kept_sentences: list[str] = []
                for s in sentences:
                    s_clean = s.strip()
                    is_bad = any(
                        bad == s_clean or bad in s_clean or s_clean in bad for bad in bad_texts
                    )
                    if is_bad:
                        pruned_count += 1
                    else:
                        kept_sentences.append(s_clean)

                if kept_sentences:
                    new_text = " ".join(kept_sentences)
                    new_claims = tuple(
                        ArticleClaimAtom(text=s, cited_support_ids=p.cited_support_ids)
                        for s in kept_sentences
                    )
                    new_paragraphs.append(
                        ArticleParagraph(
                            text=new_text,
                            cited_support_ids=p.cited_support_ids,
                            claims=new_claims,
                            generation_origin=p.generation_origin,
                        )
                    )
                    continue

            # Check if paragraph can be omitted if it is entirely invalid.
            # Fail-closed for DEVELOP stories or unverified proper names (remains to reject per test_case_8).
            # Non-DEVELOP stories with unsupported claim atoms in a substantial draft can be pruned
            # to uphold the Evidence Boundary and allow deterministic recovery per AGENTS.md 0.7.
            is_develop_para = False
            if coverage_plan and getattr(coverage_plan, "stories", None):
                dev_sups = {
                    sid
                    for s in coverage_plan.stories
                    if getattr(s, "prominence", "") == "DEVELOP"
                    for sid in s.support_ids
                }
                if any(sid in dev_sups for sid in p.cited_support_ids):
                    is_develop_para = True

            has_unsupported_name = any(
                getattr(iss, "code", "") == "UNSUPPORTED_PROPER_NAME"
                for iss in claim_violations
                if iss.unit_id == p_id
            )

            if not is_develop_para and not has_unsupported_name and len(draft.sections) >= 2:
                pruned_count += 1
                logger.info(
                    "Pruned entire invalid non-DEVELOP paragraph %s (%d chars) to uphold Evidence Boundary",
                    p_id,
                    len(p.text),
                )
                continue

            # Cannot prune DEVELOP paragraph or unverified proper name without violating fail-closed boundary
            new_paragraphs.append(p)

        if new_paragraphs:
            new_sections.append(
                ArticleSection(
                    heading=sec.heading,
                    heading_support_ids=sec.heading_support_ids,
                    heading_claims=sec.heading_claims,
                    paragraphs=tuple(new_paragraphs),
                    cited_evidence_ids=sec.cited_evidence_ids,
                    heading_generation_origin=sec.heading_generation_origin,
                )
            )

    if pruned_count == 0:
        return draft

    return StructuredArticleDraft(
        title=draft.title,
        title_support_ids=draft.title_support_ids,
        lead=draft.lead,
        lead_support_ids=draft.lead_support_ids,
        sections=tuple(new_sections),
        title_claims=draft.title_claims,
        lead_claims=draft.lead_claims,
        cited_evidence_ids=draft.cited_evidence_ids,
        word_count=0,
        title_generation_origin=draft.title_generation_origin,
        lead_generation_origin=draft.lead_generation_origin,
    )


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
        writer_metadata: dict[str, Any] | None = None,
    ) -> ArticleFinalizationResult:
        """Validate writer output, trigger recovery if needed, and assert final invariants."""
        # 1. Handle writer failure / error
        if writer_error is not None or writer_draft is None:
            if attempt_observer:
                err_meta: dict[str, Any] = {
                    "writer_status": "failed",
                    "error": str(writer_error) if writer_error else "empty writer response",
                }
                if writer_metadata:
                    err_meta.update(writer_metadata)
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_writer_rejected",
                    metadata=err_meta,
                )
            if not getattr(editorial_config, "article_allow_deterministic_fallback", False):
                raise ArticlePublicationRejected(
                    reason="writer_failed",
                    message=f"Article writer failed: {writer_error}",
                    metadata={
                        "error": str(writer_error) if writer_error else "empty writer response"
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
            # Deterministic quote repair: convert unverified quotes to indirect speech without quotes
            quote_violations = [
                iss
                for iss in writer_validation.issues
                if iss.blocking and iss.code == "UNSUPPORTED_DIRECT_QUOTE"
            ]
            if quote_violations:
                repaired_draft = _sanitize_unsupported_quotes(
                    writer_draft, quote_violations, context
                )
                repaired_val = validate_article_draft(
                    repaired_draft,
                    context,
                    config=editorial_config,
                    length_profile=length_profile,
                )
                if repaired_val.is_valid:
                    logger.info(
                        "Article writer draft repaired by converting %d unverified quote(s) to indirect speech",
                        len(quote_violations),
                    )
                    writer_draft = repaired_draft
                    writer_validation = repaired_val

        if not writer_validation.is_valid:
            # Deterministic sentence pruning per AGENTS.md 0.7:
            # Prune ungrounded speculative sentences from multi-sentence paragraphs if verified sentences remain.
            claim_violations = [
                iss
                for iss in writer_validation.issues
                if iss.blocking and iss.unit_id.startswith("P")
            ]
            if claim_violations:
                repaired_draft = _prune_unsupported_paragraph_claims(
                    writer_draft,
                    claim_violations,
                    context,
                    coverage_plan=coverage_plan,
                )
                repaired_val = validate_article_draft(
                    repaired_draft,
                    context,
                    config=editorial_config,
                    length_profile=length_profile,
                )
                if repaired_val.is_valid:
                    logger.info(
                        "Article writer draft repaired by pruning unsupported claim atom(s) from multi-sentence paragraph(s)"
                    )
                    writer_draft = repaired_draft
                    writer_validation = repaired_val

        if not writer_validation.is_valid:
            # Deterministic title repair: if only TITLE has blocking issues, sanitize or adopt section heading
            title_violations = [
                iss for iss in writer_validation.issues if iss.blocking and iss.unit_id == "TITLE"
            ]
            other_blocking = [
                iss for iss in writer_validation.issues if iss.blocking and iss.unit_id != "TITLE"
            ]
            if title_violations and not other_blocking:
                candidate_title = None
                candidate_sups = writer_draft.title_support_ids
                if writer_draft.sections and writer_draft.sections[0].heading:
                    candidate_title = writer_draft.sections[0].heading
                    candidate_sups = writer_draft.sections[0].heading_support_ids or candidate_sups
                if not candidate_title or candidate_title.upper() in ("[DELETE]", "DELETE", ""):
                    candidate_title = re.sub(r"\[.*?\]|\b\d+\b", "", writer_draft.title).strip()
                if candidate_title:
                    candidate_title = re.sub(r"\s+", " ", candidate_title).strip(" -:;,.")
                    repaired_draft = StructuredArticleDraft(
                        title=candidate_title,
                        title_support_ids=candidate_sups,
                        lead=writer_draft.lead,
                        lead_support_ids=writer_draft.lead_support_ids,
                        sections=writer_draft.sections,
                        title_claims=(
                            ArticleClaimAtom(
                                text=candidate_title, cited_support_ids=candidate_sups
                            ),
                        ),
                        lead_claims=writer_draft.lead_claims,
                        word_count=writer_draft.word_count,
                    )
                    repaired_val = validate_article_draft(
                        repaired_draft,
                        context,
                        config=editorial_config,
                        length_profile=length_profile,
                    )
                    if repaired_val.is_valid:
                        logger.info(
                            "Article writer draft repaired by replacing invalid title with verified heading"
                        )
                        writer_draft = repaired_draft
                        writer_validation = repaired_val

        if not writer_validation.is_valid:
            logger.info(
                "Article writer draft failed validation: %s",
                list(writer_validation.violations),
            )
            if attempt_observer:
                val_meta: dict[str, Any] = {
                    "writer_status": "rejected",
                    "violations": list(writer_validation.violations),
                    "draft": writer_draft.to_dict(),
                }
                if writer_metadata:
                    val_meta.update(writer_metadata)
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_validation_rejected",
                    metadata=val_meta,
                )
            if not getattr(editorial_config, "article_allow_deterministic_fallback", False):
                raise ArticlePublicationRejected(
                    reason="validation_failed",
                    message=f"Article writer draft failed validation: {list(writer_validation.violations)}",
                    metadata={
                        "violations": list(writer_validation.violations),
                        "draft": writer_draft.to_dict(),
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
        ai_diag = diagnose_article_coverage(writer_draft, coverage_plan, context=context)
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
            if writer_metadata:
                meta.update(writer_metadata)
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

        # 4. Incomplete writer draft: check safety gate before deterministic supplement
        hard_min = (
            length_profile.hard_min_words
            if length_profile is not None
            else getattr(editorial_config, "article_min_words", 500)
        )
        is_substantial = (
            writer_validation.word_count >= hard_min
            and writer_validation.section_count >= 2
            and ai_diag.covered_story_count >= 5
        )
        is_safe_for_supplement = (
            ai_diag.develop_story_coverage >= 1.0
            and (
                (ai_diag.story_coverage >= 0.80 and len(ai_diag.uncovered_story_ids) <= 3)
                or (
                    is_substantial
                    and (ai_diag.story_coverage >= 0.35 or len(ai_diag.uncovered_story_ids) <= 15)
                )
            )
        ) or (
            is_substantial
            and ai_diag.story_coverage >= 0.40
            and len(ai_diag.uncovered_story_ids) <= 12
            and getattr(editorial_config, "article_editor_enabled", False)
        )
        if not is_safe_for_supplement:
            logger.warning(
                "Writer draft is not safe for deterministic supplement "
                "(coverage=%.2f, missing=%d, develop_coverage=%.2f); supplement forbidden",
                ai_diag.story_coverage,
                len(ai_diag.uncovered_story_ids),
                ai_diag.develop_story_coverage,
            )
            if attempt_observer:
                fail_meta: dict[str, Any] = {
                    "writer_status": "rejected",
                    "ai_story_coverage": ai_diag.story_coverage,
                    "uncovered_story_ids": list(ai_diag.uncovered_story_ids),
                    "develop_story_coverage": ai_diag.develop_story_coverage,
                }
                if writer_metadata:
                    fail_meta.update(writer_metadata)
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="global_incompleteness",
                    metadata=fail_meta,
                )
            if not getattr(editorial_config, "article_allow_deterministic_fallback", False):
                raise ArticlePublicationRejected(
                    reason="global_incompleteness",
                    message=(
                        f"Writer draft is too incomplete for deterministic supplement: "
                        f"coverage={ai_diag.story_coverage:.2f} (required >= 0.80), "
                        f"missing={len(ai_diag.uncovered_story_ids)} (max 3), "
                        f"develop_coverage={ai_diag.develop_story_coverage:.2f} (required 1.0)"
                    ),
                    metadata={
                        "ai_story_coverage": ai_diag.story_coverage,
                        "uncovered_story_ids": list(ai_diag.uncovered_story_ids),
                        "develop_story_coverage": ai_diag.develop_story_coverage,
                        "draft": writer_draft.to_dict(),
                    },
                )
            return await self._run_full_fallback(
                writer_status="rejected",
                ai_diag=ai_diag,
                ai_covered_story_ids=ai_covered,
                context=context,
                coverage_plan=coverage_plan,
                editorial_config=editorial_config,
                length_profile=length_profile,
                attempt_observer=attempt_observer,
            )

        if attempt_observer:
            succ_meta: dict[str, Any] = {
                "writer_status": "passed",
                "ai_covered_story_ids": list(ai_covered),
                "ai_story_coverage": ai_diag.story_coverage,
            }
            if writer_metadata:
                succ_meta.update(writer_metadata)
            await attempt_observer.attempt_finished(
                writer_attempt_id,
                status="succeeded",
                metadata=succ_meta,
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
            final_diag = diagnose_article_coverage(supplemented, coverage_plan, context=context)
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
            if not getattr(editorial_config, "article_allow_deterministic_fallback", False):
                raise ArticlePublicationRejected(
                    reason="validation_failed",
                    message=f"Deterministic supplement failed: {exc}",
                    metadata={"error": str(exc)},
                ) from exc
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
            final_diag = diagnose_article_coverage(fallback, coverage_plan, context=context)
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
