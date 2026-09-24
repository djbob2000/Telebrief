"""Article finalizer managing single-call validation, deterministic recovery, and claim tracing."""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleStoryCoverage,
)
from src.publication.article_coverage_diagnostics import (
    ArticleCoverageDiagnostics,
    diagnose_article_coverage,
)
from src.publication.article_length import ArticleLengthProfile
from src.publication.article_material import (
    ArticleMaterialProjection,
    materialize_article_validation_context,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
    _normalize_for_dedup,
    _split_sentences_safe,
)
from src.publication.article_quality import (
    ArticleReaderQualityReport,
    diagnose_article_quality,
)
from src.publication.article_recovery import ArticleDeterministicComposer
from src.publication.article_trace import (
    ArticleClaimTraceUnit,
    build_article_claim_trace,
)
from src.publication.article_validator import (
    ArticleValidationIssue,
    ArticleValidationResult,
    validate_article_draft,
)
from src.publication.errors import (
    ArticleFinalizationInvariantError,
    ArticlePublicationRejected,
)

logger = logging.getLogger(__name__)


def _fallback_support_owner(support_id: str, context: ArticleEditorialContext) -> str:
    support = context.support_by_id.get(support_id)
    owner = getattr(support, "story_id", "") if support is not None else ""
    if owner:
        return owner
    match = re.search(r"story:(?:[^:]+|\d+)", support_id)
    return match.group(0) if match else ""


def _materialize_fallback_projection(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan,
    projection: ArticleMaterialProjection,
) -> tuple[ArticleEditorialContext, ArticleCoveragePlan]:
    """Give deterministic fallback the same surviving material as the writer.

    The normal writer path receives projected support text.  Fallback must not
    silently switch back to raw source text, which could restore directory or
    promotion payload that selection removed.  Supports marked for suppression
    are removed; trimmed supports use their projected text; and the plan is
    narrowed to stories that still have citable supports.
    """
    retained_supports: list[ArticleSupport] = []
    suppressed_story_ids = set(projection.suppressed_story_ids)
    for support in context.support_index:
        action = projection.actions_by_support_id.get(support.support_id, "KEEP")
        if (
            support.publication_use == "EXCLUDE"
            or _fallback_support_owner(support.support_id, context) in suppressed_story_ids
            or action == "SUPPRESS_PROMOTION_ONLY"
        ):
            continue
        projected_text = projection.text_by_support_id.get(support.support_id, "").strip()
        if not projected_text:
            continue
        retained_supports.append(replace(support, text=projected_text, source_text=projected_text))

    retained_by_id = {support.support_id: support for support in retained_supports}
    fallback_context = replace(
        context,
        support_index=tuple(retained_supports),
        support_by_id=retained_by_id,
    )

    retained_stories: list[ArticleStoryCoverage] = []
    retained_story_ids: set[str] = set()
    for story in coverage_plan.stories:
        support_ids = tuple(
            sid
            for sid in story.support_ids
            if sid in retained_by_id and _fallback_support_owner(sid, context) == story.story_id
        )
        detail_ids = tuple(
            sid
            for sid in story.detail_support_ids
            if sid in retained_by_id and _fallback_support_owner(sid, context) == story.story_id
        )
        if not support_ids and not detail_ids:
            continue
        retained_stories.append(
            replace(story, support_ids=support_ids, detail_support_ids=detail_ids)
        )
        retained_story_ids.add(story.story_id)

    retained_sections = []
    for section in coverage_plan.sections:
        assignments = tuple(
            replace(
                assignment,
                primary_evidence_ids=tuple(
                    sid
                    for sid in assignment.primary_evidence_ids
                    if sid in retained_by_id
                    and _fallback_support_owner(sid, context) == assignment.story_id
                ),
                concrete_details=tuple(
                    sid
                    for sid in assignment.concrete_details
                    if sid in retained_by_id
                    and _fallback_support_owner(sid, context) == assignment.story_id
                ),
            )
            for assignment in section.story_assignments
            if assignment.story_id in retained_story_ids
        )
        if not assignments:
            continue
        lead_story_id = (
            section.lead_story_id
            if section.lead_story_id in retained_story_ids
            else assignments[0].story_id
        )
        retained_sections.append(
            replace(
                section,
                lead_story_id=lead_story_id,
                story_assignments=assignments,
            )
        )

    fallback_plan = replace(
        coverage_plan,
        stories=tuple(retained_stories),
        sections=tuple(retained_sections),
    )
    return fallback_context, fallback_plan


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
    quality_report: ArticleReaderQualityReport | None = None,
    quality_report_before_edit: ArticleReaderQualityReport | None = None,
    quality_report_after_edit: ArticleReaderQualityReport | None = None,
    material_projection: ArticleMaterialProjection | None = None,
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
    meta: dict[str, Any] = {
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
        "evidence_boundary_passed": True,
        "quality_gate_passed": True,
        "unsupported_final_claim_count": 0,
        "leaked_directory_payload_count": len(final_diag.leaked_contact_payloads),
        "claim_trace": trace_meta,
    }
    if quality_report is not None:
        meta["reader_quality"] = _compact_quality_metadata(quality_report)
    if quality_report_before_edit is not None:
        compact_before = _compact_quality_metadata(quality_report_before_edit)
        meta["quality_before_edit"] = compact_before
        meta["reader_quality_before_edit"] = compact_before
    if quality_report_after_edit is not None:
        compact_after = _compact_quality_metadata(quality_report_after_edit)
        meta["quality_after_edit"] = compact_after
        meta["reader_quality_after_edit"] = compact_after
    elif quality_report is not None:
        meta["quality_after_edit"] = _compact_quality_metadata(quality_report)
    if material_projection is not None:
        meta["material_projection"] = material_projection.to_metadata()
    return meta


def _compact_quality_metadata(report: ArticleReaderQualityReport) -> dict[str, Any]:
    metadata = report.to_metadata()
    return {
        "version": metadata["version"],
        "finding_count": metadata["finding_count"],
        "needs_edit": metadata["needs_edit"],
        "counts_by_severity": metadata["counts_by_severity"],
        "counts_by_code": metadata["counts_by_code"],
        "findings": [
            {
                "code": finding.code,
                "unit_id": finding.unit_id,
                "severity": finding.severity,
            }
            for finding in report.findings
        ],
    }


def _compact_validation_metadata(result: ArticleValidationResult) -> dict[str, Any]:
    return {
        "is_valid": result.is_valid,
        "violation_count": len(result.issues),
        "issue_codes_and_units": [
            f"{issue.code}:{issue.unit_id}" for issue in result.issues if issue.blocking
        ],
    }


def _compact_quality_value(value: Any) -> dict[str, Any] | None:
    """Keep versioned quality counts and unit references, never prose payload."""
    if not isinstance(value, dict):
        return None
    findings = value.get("findings", ())
    compact_findings = []
    if isinstance(findings, Sequence) and not isinstance(findings, (str, bytes)):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            code = finding.get("code")
            unit_id = finding.get("unit_id")
            severity = finding.get("severity")
            if all(isinstance(item, str) for item in (code, unit_id, severity)):
                compact_findings.append({"code": code, "unit_id": unit_id, "severity": severity})
    compact: dict[str, Any] = {
        "version": value.get("version")
        if isinstance(value.get("version"), str)
        else "article-reader-quality-v3",
        "finding_count": value.get("finding_count", len(compact_findings)),
        "needs_edit": bool(value.get("needs_edit", False)),
        "counts_by_severity": value.get("counts_by_severity", {}),
        "counts_by_code": value.get("counts_by_code", {}),
        "findings": compact_findings,
    }
    return compact


def _compact_composition_value(value: Any) -> dict[str, Any] | None:
    """Retain composition topology and provenance IDs, not writer-facing prose."""
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key in ("line_count", "group_count", "bundle_count", "suppressed_story_ids"):
        if key in value:
            result[key] = value[key]
    lines = value.get("narrative_lines")
    if isinstance(lines, Sequence) and not isinstance(lines, (str, bytes)):
        result["narrative_lines"] = [
            {key: line[key] for key in ("line_id", "prominence", "group_ids") if key in line}
            for line in lines
            if isinstance(line, dict)
        ]
    groups = value.get("groups")
    if isinstance(groups, Sequence) and not isinstance(groups, (str, bytes)):
        result["groups"] = [
            {
                key: group[key]
                for key in (
                    "group_id",
                    "narrative_line_id",
                    "relation",
                    "lead_story_id",
                    "theme_key",
                    "members",
                )
                if key in group
            }
            for group in groups
            if isinstance(group, dict)
        ]
    return result


def _safe_writer_metadata(writer_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Whitelist compact diagnostics before persisting writer-attempt metadata.

    Writer metadata is assembled across providers and can grow as integrations
    evolve. Copying it wholesale risks persisting a prompt, draft, raw response,
    provider exception, or credential introduced by a future caller.
    """
    if not writer_metadata:
        return {}

    scalar_keys = {
        "attempt_number",
        "provider",
        "model",
        "response_chars",
        "parsed_word_count",
        "parsed_section_count",
        "planned_story_count",
        "covered_story_count",
        "story_coverage",
        "uncovered_story_ids",
        "provider_slot",
        "actual_provider",
        "actual_model",
        "response_id",
        "finish_reason",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
        "context_chars",
        "prompt_chars",
        "context_hash",
        "prompt_hash",
        "as_of",
        "as_of_utc",
        "edition_timezone",
        "editor_retry_count",
        "editor_patched_unit_ids",
        "coverage_retry_suppressed",
    }
    result: dict[str, Any] = {
        key: writer_metadata[key] for key in scalar_keys if key in writer_metadata
    }
    for key in ("quality", "quality_before_edit", "quality_after_edit"):
        compact_quality = _compact_quality_value(writer_metadata.get(key))
        if compact_quality is not None:
            result[key] = compact_quality
    composition = _compact_composition_value(writer_metadata.get("composition"))
    if composition is not None:
        result["composition"] = composition

    projection = writer_metadata.get("material_projection")
    if isinstance(projection, dict):
        result["material_projection"] = {
            key: projection[key]
            for key in (
                "actions_by_support_id",
                "reasons_by_support_id",
                "suppressed_story_ids",
                "trimmed_support_ids",
                "suppressed_story_count",
                "trimmed_support_count",
            )
            if key in projection
        }

    materialization = writer_metadata.get("materialization")
    if isinstance(materialization, dict):
        result["materialization"] = {
            key: materialization[key]
            for key in (
                "coverage_story_count",
                "story_packet_count",
                "bundle_count",
                "narrative_line_count",
                "composition_group_count",
                "group_size_distribution",
                "rendered_packet_representation",
                "packets_with_citable_support",
                "citable_support_count",
            )
            if key in materialization
        }

    retry_history = writer_metadata.get("writer_retry_history")
    if isinstance(retry_history, Sequence) and not isinstance(retry_history, (str, bytes)):
        safe_retries: list[dict[str, Any]] = []
        for item in retry_history:
            if not isinstance(item, dict):
                continue
            retry = {
                key: item[key]
                for key in (
                    "attempt_number",
                    "response_chars",
                    "parsed_word_count",
                    "parsed_section_count",
                    "planned_story_count",
                    "covered_story_count",
                    "story_coverage",
                    "catastrophic",
                    "provider_slot",
                    "actual_provider",
                    "actual_model",
                    "finish_reason",
                    "error_type",
                )
                if key in item
            }
            retry_quality = _compact_quality_value(item.get("quality"))
            if retry_quality is not None:
                retry["quality"] = retry_quality
            safe_retries.append(retry)
        result["writer_retry_history"] = safe_retries
    return result


def _quality_rejection_metadata(
    report: ArticleReaderQualityReport,
    *,
    quality_report_before_edit: ArticleReaderQualityReport | None,
    quality_report_after_edit: ArticleReaderQualityReport | None,
    writer_metadata: dict[str, Any] | None,
    validation: ArticleValidationResult,
) -> dict[str, Any]:
    safe_writer_metadata = _safe_writer_metadata(writer_metadata)
    unresolved = [
        {"code": finding.code, "unit_id": finding.unit_id, "severity": finding.severity}
        for finding in report.blocking_findings
    ]
    metadata: dict[str, Any] = {
        "stage": "post_finalization_quality",
        "quality_version": "article-reader-quality-v3",
        "quality_before_edit": (
            _compact_quality_metadata(quality_report_before_edit)
            if quality_report_before_edit is not None
            else None
        ),
        "quality_after_edit": (_compact_quality_metadata(quality_report_after_edit or report)),
        "quality_after_finalization": _compact_quality_metadata(report),
        "unresolved_quality_findings": unresolved,
        "editor_attempt_count": safe_writer_metadata.get("editor_retry_count", 0),
        "patched_unit_ids": safe_writer_metadata.get("editor_patched_unit_ids", []),
        "factual_validation": _compact_validation_metadata(validation),
        "evidence_boundary_passed": validation.is_valid,
        "quality_gate_passed": False,
    }
    if safe_writer_metadata:
        for key in (
            "composition",
            "material_projection",
            "materialization",
            "context_hash",
            "context_chars",
            "prompt_hash",
            "prompt_chars",
            "as_of",
            "as_of_utc",
            "edition_timezone",
        ):
            if key in safe_writer_metadata:
                metadata[key] = safe_writer_metadata[key]
    return metadata


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


def _merge_orphan_paragraphs(
    draft: StructuredArticleDraft,
) -> StructuredArticleDraft:
    """Merge single-sentence orphan paragraphs into preceding paragraph (AGENTS.md §0.9).

    A paragraph is considered an orphan if it contains only one sentence
    (≤1 sentence-ending punctuation mark) and is not the only paragraph in its section.
    Orphan paragraphs are appended to the preceding paragraph text with a space separator.
    """
    changed = False
    new_sections = []
    for sec in draft.sections:
        if len(sec.paragraphs) <= 1:
            new_sections.append(sec)
            continue
        merged_paras: list[ArticleParagraph] = []
        for p in sec.paragraphs:
            sentences = _split_sentences_safe(p.text)
            is_orphan = len(sentences) <= 1 and len(merged_paras) > 0
            if is_orphan:
                prev = merged_paras[-1]
                p_sentences = _split_sentences_safe(p.text)
                prev_sentences = _split_sentences_safe(prev.text)
                prev_norms = {_normalize_for_dedup(s) for s in prev_sentences}
                kept_p = [s for s in p_sentences if _normalize_for_dedup(s) not in prev_norms]
                if not kept_p:
                    changed = True
                    continue
                merged_text = prev.text.rstrip() + " " + " ".join(kept_p)
                merged_supports = list(prev.cited_support_ids) + [
                    sid for sid in p.cited_support_ids if sid not in prev.cited_support_ids
                ]
                merged_claims = tuple(list(prev.claims) + list(p.claims))
                merged_paras[-1] = ArticleParagraph(
                    text=merged_text,
                    cited_support_ids=tuple(merged_supports),
                    claims=merged_claims,
                    generation_origin=prev.generation_origin,
                )
                changed = True
                logger.info(
                    "Merged orphan single-sentence paragraph into preceding: %.60s...",
                    p.text[:60],
                )
            else:
                merged_paras.append(p)
        new_sections.append(
            ArticleSection(
                heading=sec.heading,
                heading_support_ids=sec.heading_support_ids,
                heading_claims=sec.heading_claims,
                paragraphs=tuple(merged_paras),
                heading_generation_origin=sec.heading_generation_origin,
            )
        )
    if not changed:
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
        word_count=draft.word_count,
        title_generation_origin=draft.title_generation_origin,
        lead_generation_origin=draft.lead_generation_origin,
    )


def _sanitize_phantom_heading_topics(
    draft: StructuredArticleDraft,
    heading_violations: Sequence[ArticleValidationIssue],
    context: ArticleEditorialContext | None = None,
) -> StructuredArticleDraft:
    """Deterministically sanitize section headings that have phantom topics, invalid support policies, or question overclaims."""
    bad_h_ids = {
        iss.unit_id
        for iss in heading_violations
        if iss.code
        in (
            "PHANTOM_HEADING_TOPIC",
            "INVALID_SUPPORT_POLICY",
            "QUESTION_CONTEXT_OVERCLAIM",
            "UNKNOWN_SUPPORT_ID",
            "UNKNOWN_CLAIM_SUPPORT_ID",
        )
    }
    if not bad_h_ids:
        return draft

    changed = False
    new_sections: list[ArticleSection] = []
    for s_idx, sec in enumerate(draft.sections, start=1):
        h_id = f"H{s_idx:03d}"
        if h_id in bad_h_ids:
            cur_heading = sec.heading
            cur_sups: tuple[str, ...] = tuple(sec.heading_support_ids)
            section_changed = False

            # 1. If heading has phantom topic after colon, sanitize prefix
            has_phantom = any(
                iss.unit_id == h_id and iss.code == "PHANTOM_HEADING_TOPIC"
                for iss in heading_violations
            )
            if has_phantom and ":" in cur_heading:
                prefix = cur_heading.split(":", 1)[0].strip()
                if prefix and len(prefix) >= 5:
                    cur_heading = prefix
                    changed = True
                    section_changed = True

            # 2. For unsupported or unanchored headings, borrow known PUBLISH
            # supports from paragraphs in the same section. The final Evidence
            # Boundary check still validates the heading text against them.
            needs_sup_repair = any(
                iss.unit_id == h_id
                and iss.code
                in (
                    "INVALID_SUPPORT_POLICY",
                    "QUESTION_CONTEXT_OVERCLAIM",
                    "UNKNOWN_SUPPORT_ID",
                    "UNKNOWN_CLAIM_SUPPORT_ID",
                )
                for iss in heading_violations
            )
            supports_repaired = False
            if needs_sup_repair and context is not None:
                valid_para_sups = []
                for p in sec.paragraphs:
                    paragraph_support_ids = (
                        *p.cited_support_ids,
                        *(sid for claim in p.claims for sid in claim.cited_support_ids),
                    )
                    for sid in paragraph_support_ids:
                        if sid in context.support_by_id:
                            sup = context.support_by_id[sid]
                            if sup.publication_use == "PUBLISH":
                                valid_para_sups.append(sid)
                if valid_para_sups:
                    cur_sups = tuple(dict.fromkeys(valid_para_sups[:3]))
                    changed = True
                    section_changed = True
                    supports_repaired = True
                    logger.info(
                        "Re-anchored heading %s supports from section paragraphs: %s",
                        h_id,
                        cur_sups,
                    )

            if section_changed:
                new_claims = (
                    (ArticleClaimAtom(text=cur_heading, cited_support_ids=cur_sups),)
                    if supports_repaired
                    else tuple(c for c in sec.heading_claims if c.text and c.text in cur_heading)
                )
                new_sections.append(
                    ArticleSection(
                        heading=cur_heading,
                        heading_support_ids=cur_sups,
                        heading_claims=new_claims,
                        paragraphs=sec.paragraphs,
                        cited_evidence_ids=sec.cited_evidence_ids,
                        heading_generation_origin=sec.heading_generation_origin,
                    )
                )
                logger.info(
                    "Sanitized heading %s: '%s' with %d supports",
                    h_id,
                    cur_heading,
                    len(cur_sups),
                )
                continue
        new_sections.append(sec)

    if not changed:
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
        word_count=draft.word_count,
        title_generation_origin=draft.title_generation_origin,
        lead_generation_origin=draft.lead_generation_origin,
    )


def _deduplicate_draft_content(
    draft: StructuredArticleDraft,
) -> StructuredArticleDraft:
    """Deduplicate repeated sentences within paragraphs and duplicate paragraphs across sections."""
    from src.publication.article_claims import _stem

    tok_re = re.compile(r"[\w-]+", re.UNICODE)
    changed = False
    all_prior_paragraphs: list[ArticleParagraph] = []
    new_sections: list[ArticleSection] = []

    for sec in draft.sections:
        new_paras: list[ArticleParagraph] = []
        for p in sec.paragraphs:
            # 1. Deduplicate sentences within paragraph
            sentences = _split_sentences_safe(p.text)
            if len(sentences) > 1:
                seen_sent_norms: set[str] = set()
                deduped_s: list[str] = []
                for s in sentences:
                    s_norm = _normalize_for_dedup(s)
                    if s_norm in seen_sent_norms:
                        changed = True
                        continue
                    seen_sent_norms.add(s_norm)
                    deduped_s.append(s)
                if len(deduped_s) < len(sentences):
                    new_text = " ".join(deduped_s)
                    new_claims = tuple(
                        ArticleClaimAtom(text=s, cited_support_ids=p.cited_support_ids)
                        for s in deduped_s
                    )
                    p = ArticleParagraph(
                        text=new_text,
                        cited_support_ids=p.cited_support_ids,
                        claims=new_claims,
                        generation_origin=p.generation_origin,
                    )

            # 2. Check for duplicate paragraph across current section and previous sections
            p_norm = _normalize_for_dedup(p.text)
            p_stems = {_stem(w.lower()) for w in tok_re.findall(p_norm) if len(w) >= 3}
            is_dup = False
            for existing in all_prior_paragraphs + new_paras:
                e_norm = _normalize_for_dedup(existing.text)
                if p_norm == e_norm or (len(p_norm) >= 20 and p_norm in e_norm):
                    is_dup = True
                    break
                e_stems = {_stem(w.lower()) for w in tok_re.findall(e_norm) if len(w) >= 3}
                if len(p_stems) >= 4 and len(e_stems) >= 4:
                    p_nums = set(re.findall(r"\b\d+\b", p.text))
                    e_nums = set(re.findall(r"\b\d+\b", existing.text))
                    if not (p_nums and e_nums and p_nums != e_nums):
                        overlap = len(p_stems & e_stems) / len(p_stems)
                        if p_stems.issubset(e_stems) or overlap >= 0.85:
                            is_dup = True
                            break

            if is_dup and len(sec.paragraphs) > 1:
                changed = True
                logger.info(
                    "Omitted duplicate cross-section or in-section paragraph: %.60s...",
                    p.text[:60],
                )
            else:
                new_paras.append(p)

        if not new_paras and sec.paragraphs:
            new_paras.append(sec.paragraphs[0])

        all_prior_paragraphs.extend(new_paras)
        new_sections.append(
            ArticleSection(
                heading=sec.heading,
                heading_support_ids=sec.heading_support_ids,
                heading_claims=sec.heading_claims,
                paragraphs=tuple(new_paras),
                cited_evidence_ids=sec.cited_evidence_ids,
                heading_generation_origin=sec.heading_generation_origin,
            )
        )

    if not changed:
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
        word_count=draft.word_count,
        title_generation_origin=draft.title_generation_origin,
        lead_generation_origin=draft.lead_generation_origin,
    )


_QUOTE_RE = re.compile(r"\u00ab[^\u00bb]+\u00bb")


def _detect_chat_roll_paragraphs(
    draft: StructuredArticleDraft,
    max_quotes_per_paragraph: int = 3,
) -> list[tuple[int, int, int]]:
    """Detect paragraphs with excessive consecutive direct quotes (AGENTS.md §0.6).

    Returns list of (section_idx, paragraph_idx, quote_count) for paragraphs
    exceeding the quote threshold. These are logged as warnings for editorial review.
    """
    violations: list[tuple[int, int, int]] = []
    for si, sec in enumerate(draft.sections):
        for pi, para in enumerate(sec.paragraphs):
            quote_count = len(_QUOTE_RE.findall(para.text))
            if quote_count > max_quotes_per_paragraph:
                violations.append((si, pi, quote_count))
                logger.warning(
                    "Chat-roll detected: section %d paragraph %d has %d direct quotes "
                    "(max %d per AGENTS.md 0.6). Consider rewriting with indirect speech. "
                    "Text preview: %.80s...",
                    si,
                    pi,
                    quote_count,
                    max_quotes_per_paragraph,
                    para.text[:80],
                )
    return violations


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
    all_prior_paragraphs: list[ArticleParagraph] = []
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
            kept_sentences: list[str] = []
            if len(sentences) > 1:
                for s in sentences:
                    s_clean = s.strip()
                    is_bad = bool(bad_texts) and any(
                        bad == s_clean or bad in s_clean or s_clean in bad for bad in bad_texts
                    )
                    if is_bad:
                        pruned_count += 1
                    else:
                        kept_sentences.append(s_clean)

                # Deduplicate identical sentences within the paragraph
                seen_sent_norms: set[str] = set()
                deduped_kept: list[str] = []
                for s in kept_sentences:
                    s_norm = _normalize_for_dedup(s)
                    if s_norm in seen_sent_norms:
                        pruned_count += 1
                        continue
                    seen_sent_norms.add(s_norm)
                    deduped_kept.append(s)
                kept_sentences = deduped_kept

            has_unsupported_name = any(
                getattr(iss, "code", "") == "UNSUPPORTED_PROPER_NAME"
                for iss in claim_violations
                if iss.unit_id == p_id
            )

            # If no sentences kept (or single-sentence invalid paragraph), synthesize from support evidence
            # unless it has an unverified proper name which must fail closed per AGENTS.md 0.7 & test_case_8.
            if not kept_sentences and not has_unsupported_name and context and p.cited_support_ids:
                from src.publication.article_recovery import _clean_support_text_for_reader

                existing_norms = {
                    _normalize_for_dedup(para.text) for s in new_sections for para in s.paragraphs
                } | {_normalize_for_dedup(para.text) for para in new_paragraphs}

                from src.publication.article_claims import _stem

                tok_re = re.compile(r"[\w-]+", re.UNICODE)
                sec_stems = {
                    _stem(w.lower())
                    for para in new_paragraphs
                    for w in tok_re.findall(para.text)
                    if len(w) >= 3
                }

                sup_texts = [
                    context.support_by_id[sid].text
                    for sid in p.cited_support_ids
                    if sid in context.support_by_id and context.support_by_id[sid].text
                ]
                safe_s = []
                for st in sup_texts[:2]:
                    norm = _normalize_for_dedup(st)
                    if norm in existing_norms:
                        continue
                    st_stems = {_stem(w.lower()) for w in tok_re.findall(norm) if len(w) >= 3}
                    if len(st_stems) >= 2 and st_stems.issubset(sec_stems):
                        # Topic already thoroughly covered in this section
                        continue
                    cleaned = _clean_support_text_for_reader(st)
                    if cleaned:
                        safe_s.append(cleaned)

                if safe_s:
                    kept_sentences = safe_s
                    pruned_count += 1

            if kept_sentences:
                new_text = " ".join(kept_sentences)
                # Deduplicate against existing paragraphs in the section:
                # If new_text is substantially redundant or already expressed, omit it.
                new_norm = _normalize_for_dedup(new_text)
                is_duplicate = False
                for ep in new_paragraphs:
                    ep_norm = _normalize_for_dedup(ep.text)
                    if new_norm in ep_norm or ep_norm in new_norm:
                        is_duplicate = True
                        break
                    from src.publication.article_claims import _stem

                    tok_re = re.compile(r"[\w-]+", re.UNICODE)
                    new_stems = {_stem(w.lower()) for w in tok_re.findall(new_norm) if len(w) >= 3}
                    ep_stems = {_stem(w.lower()) for w in tok_re.findall(ep_norm) if len(w) >= 3}
                    if len(new_stems) >= 3 and new_stems.issubset(ep_stems):
                        is_duplicate = True
                        break

                if is_duplicate:
                    pruned_count += 1
                    continue

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

            if not is_develop_para and not has_unsupported_name and len(draft.sections) >= 2:
                # Do not prune entire paragraph if it is the only paragraph in the section,
                # as that would drop the whole section and its topic.
                if len(sec.paragraphs) > 1:
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
            # Deduplicate any duplicate paragraphs within the section and across earlier sections
            from src.publication.article_claims import _stem

            tok_re = re.compile(r"[\w-]+", re.UNICODE)
            deduped_paragraphs: list[ArticleParagraph] = []
            for para in new_paragraphs:
                p_norm = _normalize_for_dedup(para.text)
                dup = False
                for existing in all_prior_paragraphs + deduped_paragraphs:
                    e_norm = _normalize_for_dedup(existing.text)
                    if p_norm in e_norm or e_norm in p_norm:
                        dup = True
                        break
                    p_stems = {_stem(w.lower()) for w in tok_re.findall(p_norm) if len(w) >= 3}
                    e_stems = {_stem(w.lower()) for w in tok_re.findall(e_norm) if len(w) >= 3}
                    if len(p_stems) >= 3 and len(e_stems) >= 3:
                        overlap = len(p_stems & e_stems) / len(p_stems)
                        if p_stems.issubset(e_stems) or overlap >= 0.75:
                            dup = True
                            break
                if not dup or (len(new_paragraphs) == 1 and not deduped_paragraphs):
                    deduped_paragraphs.append(para)
                else:
                    pruned_count += 1

            if deduped_paragraphs:
                all_prior_paragraphs.extend(deduped_paragraphs)
                new_sections.append(
                    ArticleSection(
                        heading=sec.heading,
                        heading_support_ids=sec.heading_support_ids,
                        heading_claims=sec.heading_claims,
                        paragraphs=tuple(deduped_paragraphs),
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
        writer_validation: ArticleValidationResult | None = None,
        quality_report: ArticleReaderQualityReport | None = None,
        quality_report_after_edit: ArticleReaderQualityReport | None = None,
        material_projection: ArticleMaterialProjection | None = None,
        place_resolver: Any | None = None,
    ) -> ArticleFinalizationResult:
        """Validate writer output, trigger recovery if needed, and assert final invariants.

        ``writer_validation`` is produced immediately after parsing the writer
        response. Reusing it avoids repeating the expensive Evidence Boundary
        pass when no writer-side repair changed the draft.
        """
        validation_context = (
            materialize_article_validation_context(context, material_projection)
            if material_projection is not None
            else context
        )
        # 1. Handle writer failure / error
        if writer_error is not None or writer_draft is None:
            if attempt_observer:
                err_meta: dict[str, Any] = {
                    "writer_status": "failed",
                    "exception_type": type(writer_error).__name__
                    if writer_error
                    else "EmptyWriterResponse",
                }
                safe_writer_metadata = _safe_writer_metadata(writer_metadata)
                if safe_writer_metadata:
                    err_meta["writer_attempt"] = safe_writer_metadata
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_writer_rejected",
                    metadata=err_meta,
                )
            if not getattr(editorial_config, "article_allow_deterministic_fallback", False):
                raise ArticlePublicationRejected(
                    reason="writer_failed",
                    message=(
                        "Article writer failed: "
                        f"{type(writer_error).__name__ if writer_error else 'EmptyWriterResponse'}"
                    ),
                    metadata={
                        "stage": "writer",
                        "exception_type": type(writer_error).__name__
                        if writer_error
                        else "EmptyWriterResponse",
                        "writer_attempt": _safe_writer_metadata(writer_metadata),
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
                writer_metadata=writer_metadata,
                quality_report_before_edit=quality_report,
                quality_report_after_edit=quality_report_after_edit,
                material_projection=material_projection,
                place_resolver=place_resolver,
            )

        # 2. Validate writer draft
        if writer_validation is None:
            writer_validation = validate_article_draft(
                writer_draft,
                context,
                config=editorial_config,
                length_profile=length_profile,
                material_projection=material_projection,
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
                    writer_draft, quote_violations, validation_context
                )
                repaired_val = validate_article_draft(
                    repaired_draft,
                    context,
                    config=editorial_config,
                    length_profile=length_profile,
                    material_projection=material_projection,
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
                    validation_context,
                    coverage_plan=coverage_plan,
                )
                repaired_val = validate_article_draft(
                    repaired_draft,
                    context,
                    config=editorial_config,
                    length_profile=length_profile,
                    material_projection=material_projection,
                )
                if repaired_val.is_valid or len(repaired_val.violations) < len(
                    writer_validation.violations
                ):
                    logger.info(
                        "Article writer draft repaired by pruning unsupported claim atom(s) from multi-sentence paragraph(s)"
                    )
                    writer_draft = repaired_draft
                    writer_validation = repaired_val
                else:
                    logger.warning(
                        "Article writer draft pruning attempted but repaired draft still invalid: %s",
                        list(repaired_val.violations),
                    )

        if not writer_validation.is_valid:
            # Deterministic heading repair: sanitize headings with phantom topics, invalid support policies or question overclaims
            heading_violations = [
                iss
                for iss in writer_validation.issues
                if iss.blocking
                and iss.unit_id.startswith("H")
                and iss.code
                in (
                    "PHANTOM_HEADING_TOPIC",
                    "INVALID_SUPPORT_POLICY",
                    "QUESTION_CONTEXT_OVERCLAIM",
                    "UNKNOWN_SUPPORT_ID",
                    "UNKNOWN_CLAIM_SUPPORT_ID",
                )
            ]
            if heading_violations:
                repaired_draft = _sanitize_phantom_heading_topics(
                    writer_draft, heading_violations, context=validation_context
                )
                repaired_val = validate_article_draft(
                    repaired_draft,
                    context,
                    config=editorial_config,
                    length_profile=length_profile,
                    material_projection=material_projection,
                )
                if repaired_val.is_valid or len(repaired_val.violations) < len(
                    writer_validation.violations
                ):
                    logger.info(
                        "Article writer draft repaired by sanitizing %d heading violation(s)",
                        len(heading_violations),
                    )
                    writer_draft = repaired_draft
                    writer_validation = repaired_val

        if not writer_validation.is_valid:
            # Deterministic title repair: sanitize or adopt verified section heading and clean up supports
            title_violations = [
                iss for iss in writer_validation.issues if iss.blocking and iss.unit_id == "TITLE"
            ]
            if title_violations:
                candidate_title = None
                candidate_sups = writer_draft.title_support_ids
                if writer_draft.sections and writer_draft.sections[0].heading:
                    candidate_title = writer_draft.sections[0].heading
                    candidate_sups = writer_draft.sections[0].heading_support_ids or candidate_sups
                if not candidate_title or candidate_title.upper() in ("[DELETE]", "DELETE", ""):
                    candidate_title = re.sub(r"\[.*?\]|\b\d+\b", "", writer_draft.title).strip()
                if candidate_title:
                    candidate_title = re.sub(r"\s+", " ", candidate_title).strip(" -:;,.")
                    from src.publication.article_validator import _CONTINUATION_RE

                    if not _CONTINUATION_RE.search(candidate_title):
                        clean_sups = tuple(
                            sid
                            for sid in candidate_sups
                            if sid in validation_context.support_by_id
                            and validation_context.support_by_id[sid].publication_use == "PUBLISH"
                            and validation_context.support_by_id[sid].temporal_role
                            == "CURRENT_WINDOW"
                        )
                        if not clean_sups:
                            clean_sups = tuple(
                                s.support_id
                                for s in validation_context.supports
                                if s.publication_use == "PUBLISH"
                                and s.temporal_role == "CURRENT_WINDOW"
                            )[:2]
                        candidate_sups = clean_sups or candidate_sups
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
                        material_projection=material_projection,
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
                    "factual_validation": _compact_validation_metadata(writer_validation),
                }
                safe_writer_metadata = _safe_writer_metadata(writer_metadata)
                if safe_writer_metadata:
                    val_meta["writer_attempt"] = safe_writer_metadata
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_validation_rejected",
                    metadata=val_meta,
                )
            if not getattr(editorial_config, "article_allow_deterministic_fallback", False):
                raise ArticlePublicationRejected(
                    reason="validation_failed",
                    message="Article writer draft failed Evidence Boundary validation",
                    metadata={
                        "stage": "writer_validation",
                        "factual_validation": _compact_validation_metadata(writer_validation),
                        "quality_before_edit": (
                            _compact_quality_metadata(quality_report)
                            if quality_report is not None
                            else None
                        ),
                        "quality_after_edit": (
                            _compact_quality_metadata(quality_report_after_edit)
                            if quality_report_after_edit is not None
                            else None
                        ),
                        "editor_attempt_count": _safe_writer_metadata(writer_metadata).get(
                            "editor_retry_count", 0
                        ),
                        "patched_unit_ids": _safe_writer_metadata(writer_metadata).get(
                            "editor_patched_unit_ids", []
                        ),
                        "evidence_boundary_passed": False,
                        "quality_gate_passed": None,
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
                writer_metadata=writer_metadata,
                quality_report_before_edit=quality_report,
                quality_report_after_edit=quality_report_after_edit,
                material_projection=material_projection,
                place_resolver=place_resolver,
            )

        # 3. Writer draft is valid; apply deterministic structural improvements
        # 3a. Deduplicate identical sentences within paragraphs and duplicate cross-section paragraphs
        writer_draft = _deduplicate_draft_content(writer_draft)

        # 3b. Merge single-sentence orphan paragraphs (AGENTS.md §0.9)
        writer_draft = _merge_orphan_paragraphs(writer_draft)

        # Structural finalization changes the reader-facing draft. Never reuse
        # the validation result from the pre-merge object: the exact object
        # rendered below must pass the Evidence Boundary itself.
        final_validation = validate_article_draft(
            writer_draft,
            context,
            config=editorial_config,
            length_profile=length_profile,
            material_projection=material_projection,
        )
        if not final_validation.is_valid:
            logger.warning(
                "Finalized article draft failed re-validation: %s",
                list(final_validation.violations),
            )
            if attempt_observer:
                final_val_meta: dict[str, Any] = {
                    "writer_status": "rejected",
                    "factual_validation": _compact_validation_metadata(final_validation),
                    "stage": "post_finalization_validation",
                }
                safe_writer_metadata = _safe_writer_metadata(writer_metadata)
                if safe_writer_metadata:
                    final_val_meta["writer_attempt"] = safe_writer_metadata
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_validation_rejected",
                    metadata=final_val_meta,
                )
            if not getattr(editorial_config, "article_allow_deterministic_fallback", False):
                raise ArticlePublicationRejected(
                    reason="validation_failed",
                    message=("Finalized article draft failed Evidence Boundary validation"),
                    metadata={
                        "factual_validation": _compact_validation_metadata(final_validation),
                        "stage": "post_finalization_validation",
                        "quality_before_edit": (
                            _compact_quality_metadata(quality_report)
                            if quality_report is not None
                            else None
                        ),
                        "quality_after_edit": (
                            _compact_quality_metadata(quality_report_after_edit)
                            if quality_report_after_edit is not None
                            else None
                        ),
                        "editor_attempt_count": _safe_writer_metadata(writer_metadata).get(
                            "editor_retry_count", 0
                        ),
                        "patched_unit_ids": _safe_writer_metadata(writer_metadata).get(
                            "editor_patched_unit_ids", []
                        ),
                        "evidence_boundary_passed": False,
                        "quality_gate_passed": None,
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
                writer_metadata=writer_metadata,
                quality_report_before_edit=quality_report,
                quality_report_after_edit=quality_report_after_edit,
                material_projection=material_projection,
                place_resolver=place_resolver,
            )
        writer_validation = final_validation

        # Reader-quality checks run on the exact post-deduplication and
        # post-orphan-merge object that will be rendered.  Factual validation
        # remains separate; quality findings never masquerade as claims.
        final_quality = diagnose_article_quality(
            writer_draft,
            coverage_plan,
            context,
            material_projection=material_projection,
            place_resolver=place_resolver,
        )
        if final_quality.blocking_findings:
            logger.info(
                "Finalized article draft failed reader-quality gate: %s",
                [
                    f"{finding.code}:{finding.unit_id}"
                    for finding in final_quality.blocking_findings
                ],
            )
            if attempt_observer:
                quality_meta: dict[str, Any] = {
                    "writer_status": "rejected",
                    **_quality_rejection_metadata(
                        final_quality,
                        quality_report_before_edit=quality_report,
                        quality_report_after_edit=quality_report_after_edit,
                        writer_metadata=writer_metadata,
                        validation=writer_validation,
                    ),
                    "stage": "post_finalization_quality",
                }
                safe_writer_metadata = _safe_writer_metadata(writer_metadata)
                if safe_writer_metadata:
                    quality_meta["writer_attempt"] = safe_writer_metadata
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_quality_rejected",
                    metadata=quality_meta,
                )
            raise ArticlePublicationRejected(
                reason="quality_failed",
                message=(
                    "Finalized article draft failed reader-quality validation: "
                    f"{[finding.code for finding in final_quality.blocking_findings]}"
                ),
                metadata=_quality_rejection_metadata(
                    final_quality,
                    quality_report_before_edit=quality_report,
                    quality_report_after_edit=quality_report_after_edit,
                    writer_metadata=writer_metadata,
                    validation=writer_validation,
                ),
            )

        # 3b. Detect chat-roll patterns for editorial logging (AGENTS.md §0.6)
        _detect_chat_roll_paragraphs(writer_draft, max_quotes_per_paragraph=2)

        # 3c. Diagnose coverage
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
                quality_report=final_quality,
                quality_report_before_edit=quality_report,
                quality_report_after_edit=quality_report_after_edit,
                material_projection=material_projection,
            )
            safe_writer_metadata = _safe_writer_metadata(writer_metadata)
            if safe_writer_metadata:
                meta["writer_attempt"] = safe_writer_metadata
                for key, value in safe_writer_metadata.items():
                    if key not in meta:
                        meta[key] = value
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

        # Coverage is a quality diagnostic, not a second factual-verification
        # gate.  Do not append raw fragments or reject a grounded city-life
        # article merely because the writer did not mention every BRIEF/WEAVE
        # item in a single cohesive narrative.
        else:
            logger.info(
                "Accepting grounded article with partial coverage "
                "(coverage=%.2f, missing=%d); coverage recorded diagnostically",
                ai_diag.story_coverage,
                len(ai_diag.uncovered_story_ids),
            )
            trace = build_article_claim_trace(writer_draft, context)
            meta = _build_final_metadata(
                winning_kind="event_article_writer",
                writer_status="passed",
                recovery_mode="none",
                coverage_plan=coverage_plan,
                ai_covered_story_ids=ai_covered,
                supplemented_story_ids=(),
                final_covered_story_ids=ai_covered,
                ai_diag=ai_diag,
                final_diag=ai_diag,
                trace=trace,
                quality_report=final_quality,
                quality_report_before_edit=quality_report,
                quality_report_after_edit=quality_report_after_edit,
                material_projection=material_projection,
            )
            meta["coverage_only_diagnostic"] = True
            safe_writer_metadata = _safe_writer_metadata(writer_metadata)
            if safe_writer_metadata:
                meta["writer_attempt"] = safe_writer_metadata
                for key, value in safe_writer_metadata.items():
                    if key not in meta:
                        meta[key] = value
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
        writer_metadata: dict[str, Any] | None = None,
        quality_report_before_edit: ArticleReaderQualityReport | None = None,
        quality_report_after_edit: ArticleReaderQualityReport | None = None,
        material_projection: ArticleMaterialProjection | None = None,
        place_resolver: Any | None = None,
    ) -> ArticleFinalizationResult:
        fb_attempt_id = 0
        if attempt_observer:
            fb_attempt_id = await attempt_observer.attempt_started("deterministic_fallback")

        try:
            fallback_context = context
            fallback_plan = coverage_plan
            if material_projection is not None:
                fallback_context, fallback_plan = _materialize_fallback_projection(
                    context, coverage_plan, material_projection
                )
                if not fallback_plan.stories:
                    raise ArticlePublicationRejected(
                        reason="quality_failed",
                        message="Deterministic fallback has no surviving projected article material",
                        metadata={
                            "stage": "fallback_material_projection",
                            "material_projection": material_projection.to_metadata(),
                        },
                    )
            fallback = self.composer.render_full_fallback(
                fallback_context,
                fallback_plan,
                max_sections=editorial_config.article_max_sections,
            )
            fb_validation = validate_article_draft(
                fallback,
                context,
                config=editorial_config,
                length_profile=length_profile,
                material_projection=material_projection,
            )
            if not fb_validation.is_valid:
                raise ArticleFinalizationInvariantError(
                    f"Deterministic fallback failed validation: {list(fb_validation.violations)}"
                )

            fallback_quality = diagnose_article_quality(
                fallback,
                fallback_plan,
                fallback_context,
                material_projection=material_projection,
                place_resolver=place_resolver,
            )
            if fallback_quality.blocking_findings:
                raise ArticlePublicationRejected(
                    reason="quality_failed",
                    message=(
                        "Deterministic fallback failed reader-quality validation: "
                        f"{[finding.code for finding in fallback_quality.blocking_findings]}"
                    ),
                    metadata={
                        "quality": fallback_quality.to_metadata(),
                        "stage": "fallback_quality",
                    },
                )

            # The rendered fallback uses projected text, while the published
            # trace must still resolve provenance against the immutable source
            # context.  Validation above has already gated suppressed material.
            trace = build_article_claim_trace(fallback, context)
            final_diag = diagnose_article_coverage(
                fallback, fallback_plan, context=fallback_context
            )
            final_covered = tuple(final_diag.covered_story_ids)

            if (
                set(final_covered) != set(fallback_plan.story_ids)
                or final_diag.story_coverage != 1.0
            ):
                raise ArticleFinalizationInvariantError(
                    f"Deterministic fallback coverage incomplete: {final_covered} vs {fallback_plan.story_ids}"
                )

            meta = _build_final_metadata(
                winning_kind="event_article_deterministic_fallback",
                writer_status=writer_status,
                recovery_mode="full_fallback",
                coverage_plan=fallback_plan,
                ai_covered_story_ids=ai_covered_story_ids,
                supplemented_story_ids=(),
                final_covered_story_ids=final_covered,
                ai_diag=ai_diag,
                final_diag=final_diag,
                trace=trace,
                quality_report=fallback_quality,
                quality_report_before_edit=quality_report_before_edit,
                quality_report_after_edit=quality_report_after_edit,
                material_projection=material_projection,
            )
            safe_writer_metadata = _safe_writer_metadata(writer_metadata)
            if safe_writer_metadata:
                meta["writer_attempt"] = safe_writer_metadata
                for key, value in safe_writer_metadata.items():
                    if key not in meta:
                        meta[key] = value
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
                    metadata={"exception_type": type(exc).__name__},
                )
            if isinstance(exc, ArticlePublicationRejected):
                raise
            if isinstance(exc, ArticleFinalizationInvariantError):
                raise
            raise ArticleFinalizationInvariantError(
                f"Failed to finalize deterministic article fallback: {exc}"
            ) from exc
