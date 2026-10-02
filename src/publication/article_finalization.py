"""Article finalizer managing single-call validation, deterministic recovery, and claim tracing."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from time import perf_counter
from typing import Any, Callable, Literal, Mapping, Protocol
from uuid import uuid4

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
    _PRIVATE_SECTOR_RE,
    ARTICLE_READER_QUALITY_VERSION,
    ArticleReaderQualityReport,
    _citable_support_ids,
    _has_specific_source_area,
    _has_unresolved_private_sector_location,
    _span_is_quoted_name,
    _supported_provider_mentions,
    diagnose_article_quality,
)
from src.publication.article_quality import (
    _QUOTE_RE as _QUALITY_QUOTE_RE,
)
from src.publication.article_quality_policy import (
    ARTICLE_QUALITY_FINDING_POLICIES,
    ARTICLE_WHOLE_DRAFT_FINDING_CODES,
    article_quality_policy,
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


# Bump when Evidence Boundary validation semantics change. Reader-quality has
# its own version; the canonical policy content is fingerprinted separately.
ARTICLE_ASSESSMENT_VALIDATOR_VERSION = "article_evidence_boundary_v1"


def _assessment_input_value(value: Any) -> Any:
    """Serialize actual validation inputs, retaining full provenance/options."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _assessment_input_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _assessment_input_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_assessment_input_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_assessment_input_value(item) for item in value), key=repr)
    if isinstance(value, re.Pattern):
        return {"pattern": value.pattern, "flags": value.flags}
    # Unknown opaque inputs cannot prove equality, even for the same instance:
    # hidden mutable state might have changed. Force fresh assessment instead.
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}", "opaque": uuid4().hex}


def article_assessment_input_fingerprint(
    context: ArticleEditorialContext,
    coverage_plan: ArticleCoveragePlan | None,
    config: PublicationEditorialConfig | None,
    length_profile: ArticleLengthProfile | None,
    material_projection: ArticleMaterialProjection | None,
    place_resolver: Any | None,
) -> str:
    """Identity of all inputs consumed by evidence, quality and coverage checks.

    Profile *content* protects against changes to aliases/geography without an
    edition-id change. No report may be reused using rendered-text equality.
    """
    from src.publication.article_validator import resolve_article_place_resolver

    evidence_resolver = resolve_article_place_resolver(context)

    def resolver_input(resolver: Any) -> Any:
        if resolver is None:
            return None
        profile = getattr(resolver, "_profile", None)
        return {
            "type": f"{type(resolver).__module__}.{type(resolver).__qualname__}",
            "profile": profile if profile is not None else resolver,
        }

    inputs = {
        "context": context,
        "coverage_plan": coverage_plan,
        "config": config or PublicationEditorialConfig(),
        "length_profile": length_profile,
        "material_projection": material_projection,
        "geography": resolver_input(place_resolver),
        "evidence_geography": resolver_input(evidence_resolver),
        "validator_version": ARTICLE_ASSESSMENT_VALIDATOR_VERSION,
        "quality_version": ARTICLE_READER_QUALITY_VERSION,
        "policy": ARTICLE_QUALITY_FINDING_POLICIES,
    }
    serialized = json.dumps(
        _assessment_input_value(inputs),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ArticleAssessmentCheckpoint:
    """Exact assessed candidate, safe or blocked; kept in memory for preview.

    Observers must never persist full prose in ordinary production metadata.
    ``publishable`` is only about this checkpoint and these fingerprinted inputs.
    """

    draft: StructuredArticleDraft
    validation: ArticleValidationResult
    quality: ArticleReaderQualityReport
    input_fingerprint: str
    coverage: ArticleCoverageDiagnostics | None = None

    def matches(self, draft: StructuredArticleDraft, input_fingerprint: str) -> bool:
        return self.draft == draft and self.input_fingerprint == input_fingerprint

    @property
    def publishable(self) -> bool:
        return self.validation.is_valid and not self.quality.blocking_findings


# Optional synchronous in-memory preview hook; no production persistence.
ArticleCheckpointObserver = Callable[
    [str, StructuredArticleDraft, ArticleAssessmentCheckpoint | None], None
]


def _unresolved_whole_draft_findings(
    *reports: ArticleReaderQualityReport | None,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            finding.code
            for report in reports
            if report is not None
            for finding in report.blocking_findings
            if finding.code in ARTICLE_WHOLE_DRAFT_FINDING_CODES
            and article_quality_policy(finding.code).publication_effect == "block_publication"
        )
    )


def _reject_structural_quality_fallback(findings: Sequence[str], *, stage: str) -> None:
    if not findings:
        return
    raise ArticlePublicationRejected(
        reason="quality_failed",
        message=(
            "Article has unresolved whole-draft quality findings; deterministic fallback is blocked: "
            f"{list(findings)}"
        ),
        metadata={"stage": stage, "unresolved_whole_draft_findings": list(findings)},
    )


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
    planned_story_count = final_diag.planned_story_count
    ai_story_coverage = (
        ai_diag.story_coverage
        if ai_diag is not None
        else len(ai_covered_story_ids) / planned_story_count
        if planned_story_count
        else 1.0
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
        readiness_findings = [
            finding
            for finding in quality_report.findings
            if article_quality_policy(finding.code).publication_effect == "readiness_incomplete"
        ]
        meta["editorial_acceptance"] = {
            "status": "incomplete" if readiness_findings else "complete",
            "diagnostic_codes": list(dict.fromkeys(f.code for f in readiness_findings)),
            "unresolved_story_count": len(readiness_findings),
            "unresolved_story_unit_ids": list(dict.fromkeys(f.unit_id for f in readiness_findings)),
            "coverage_veto_applied": False,
        }
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
                "support_ids": list(finding.support_ids),
                "finding_class": article_quality_policy(finding.code).finding_class,
                "repair_scope": article_quality_policy(finding.code).repair_scope,
                "publication_effect": article_quality_policy(finding.code).publication_effect,
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
        "blocking_issues": [
            {
                "code": issue.code,
                "unit_id": issue.unit_id,
                "message": _safe_validation_finding_message(issue.code),
                "support_ids": list(issue.support_ids),
            }
            for issue in result.issues
            if issue.blocking
        ],
    }


def _safe_validation_finding_message(code: str) -> str:
    """Describe a validation failure without copying draft or source text."""
    if code.startswith("UNSUPPORTED_"):
        return "A reader-facing claim is not grounded in its cited evidence."
    if code in {"UNKNOWN_SUPPORT_ID", "UNKNOWN_CLAIM_SUPPORT_ID"}:
        return "The draft references a support ID unavailable in the article context."
    if code in {"MISSING_SUPPORT:title", "MISSING_SUPPORT:lead", "MISSING_SUPPORT:paragraph"}:
        return "A reader-facing unit is missing its required evidence citation."
    if code == "CLAIM_SUPPORT_MISMATCH":
        return "Unit citations and claim-level citations do not match."
    if code == "PHANTOM_HEADING_TOPIC":
        return "A section heading promises a topic that its body does not cover."
    if code == "SECTION_COUNT_OUT_OF_BOUNDS":
        return "The article has too few or too many sections."
    if code == "WORD_COUNT_OUT_OF_BOUNDS":
        return "The article is outside its configured word-count range."
    return f"Evidence Boundary finding: {code}."


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
            if isinstance(code, str) and isinstance(unit_id, str) and isinstance(severity, str):
                policy = article_quality_policy(code)
                compact_findings.append(
                    {
                        "code": code,
                        "unit_id": unit_id,
                        "severity": severity,
                        "finding_class": policy.finding_class,
                        "repair_scope": policy.repair_scope,
                        "publication_effect": policy.publication_effect,
                    }
                )
    compact: dict[str, Any] = {
        "version": value.get("version")
        if isinstance(value.get("version"), str)
        else ARTICLE_READER_QUALITY_VERSION,
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
        "as_of",
        "as_of_utc",
        "edition_timezone",
        "editor_retry_count",
        "writer_invocation_count",
        "editor_invocation_count",
        "editor_invocation_limit",
        "generation_timeout_seconds",
        "writer_stage_elapsed_seconds",
        "writer_response_count",
        "semantic_transition_count",
        "semantic_recovery_exhausted",
        "context_character_count",
        "expected_support_count",
        "exposed_support_count",
        "evidence_record_count",
        "quote_allowlist_count",
        "editor_patched_unit_ids",
        "editor_failure_type",
        "editor_fallback_to_original",
        "editor_outcome",
        "coverage_retry_suppressed",
        "structural_recomposition",
    }
    result: dict[str, Any] = {
        key: writer_metadata[key] for key in scalar_keys if key in writer_metadata
    }

    safe_choice_values = {
        "writer_response_disposition": {"usable", "repairable", "unusable"},
        "writer_response_reason": {
            "empty_response",
            "whole_response_refusal",
            "whole_response_service_message",
            "no_reader_prose",
            "format_repair_needed",
            "parsed_markdown",
            "legacy_json",
            "legacy_json_format_repair_needed",
            "legacy_json_unusable",
        },
        "semantic_recovery_reason": {
            "first_response_accepted",
            "recovery_response_accepted",
            "writer_response_unusable",
            "semantic_rejection_limit_reached",
            "semantic_recovery_disabled",
            "no_remaining_provider_slots",
            "recovery_slots_transport_failed",
            "no_recovery_response",
        },
        "article_writer_context_version": {"event-article-context-v2-evidence-inventory"},
        "article_narrative_prompt_version": {"event-article-narrative-v13"},
        "article_writer_prompt_version": {"v17"},
    }
    for key, allowed_values in safe_choice_values.items():
        value = writer_metadata.get(key)
        if isinstance(value, str) and value in allowed_values:
            result[key] = value

    safe_hash_keys = (
        "context_hash",
        "prompt_hash",
        "context_sha256",
        "expected_support_ids_sha256",
        "exposed_support_ids_sha256",
        "quote_allowlist_sha256",
    )
    for key in safe_hash_keys:
        value = writer_metadata.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            result[key] = value

    format_findings = writer_metadata.get("writer_response_format_findings")
    allowed_format_findings = {
        "EMPTY_RESPONSE",
        "BARE_TITLE_NORMALIZED",
        "MISSING_TITLE",
        "MISSING_LEAD",
        "MISSING_SECTIONS",
        "WHOLE_RESPONSE_REFUSAL",
        "SERVICE_MESSAGE",
        "NO_READER_PROSE",
    }
    if isinstance(format_findings, list):
        result["writer_response_format_findings"] = [
            finding
            for finding in format_findings[:7]
            if isinstance(finding, str) and finding in allowed_format_findings
        ]

    def compact_provider_attempts(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {
            key: value[key]
            for key in (
                "slot_attempt_count",
                "transport_attempt_count",
                "complete",
                "elapsed_seconds",
            )
            if key in value and isinstance(value[key], (int, float, bool))
        }

    writer_counts = compact_provider_attempts(writer_metadata.get("writer_provider_attempts"))
    if writer_counts is not None:
        result["writer_provider_attempts"] = writer_counts
    editor_counts = writer_metadata.get("editor_provider_attempts")
    if isinstance(editor_counts, list):
        result["editor_provider_attempts"] = [
            compact
            for item in editor_counts[:2]
            if (compact := compact_provider_attempts(item)) is not None
        ]
    for key in ("quality", "quality_before_edit", "quality_after_edit"):
        compact_quality = _compact_quality_value(writer_metadata.get(key))
        if compact_quality is not None:
            result[key] = compact_quality
    recomposition = writer_metadata.get("structural_recomposition")
    if isinstance(recomposition, dict):
        result["structural_recomposition"] = {
            key: recomposition[key]
            for key in (
                "attempted",
                "attempt_count",
                "before_findings",
                "after_findings",
                "resolved",
                "error_type",
            )
            if key in recomposition
        }
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
        {
            "code": finding.code,
            "unit_id": finding.unit_id,
            "severity": finding.severity,
            "message": _safe_quality_finding_message(finding.code),
            "support_ids": list(finding.support_ids),
        }
        for finding in report.blocking_findings
    ]
    metadata: dict[str, Any] = {
        "stage": "post_finalization_quality",
        "quality_version": ARTICLE_READER_QUALITY_VERSION,
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
            "structural_recomposition",
        ):
            if key in safe_writer_metadata:
                metadata[key] = safe_writer_metadata[key]
    return metadata


def _safe_quality_finding_message(code: str) -> str:
    """Return a useful diagnostic label without embedding article/source prose."""
    return article_quality_policy(code).description


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
                if len(_QUALITY_QUOTE_RE.findall(merged_text)) > 2:
                    # This merge has no cited-support/profile context to distinguish
                    # quoted names from speech, so use the raw quote-span count as a
                    # conservative upper bound. The quality gate below classifies
                    # grounded names; this guard only prevents merging three or more
                    # quoted spans into one paragraph.
                    merged_paras.append(p)
                    logger.info(
                        "Kept orphan paragraph separate to avoid creating a quote roll: %.60s...",
                        p.text[:60],
                    )
                    continue
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


def _remove_duplicate_headings(draft: StructuredArticleDraft) -> StructuredArticleDraft:
    """Hide repeated headings while preserving every paragraph and its evidence."""
    normalized_title = _normalize_for_dedup(draft.title)
    seen = {normalized_title} if normalized_title else set()
    sections = []
    changed = False
    for section in draft.sections:
        normalized_heading = _normalize_for_dedup(section.heading)
        if normalized_heading and normalized_heading in seen:
            sections.append(
                replace(
                    section,
                    heading="",
                    heading_support_ids=(),
                    heading_claims=(),
                )
            )
            changed = True
            logger.info(
                "Removed duplicate heading '%s' while preserving its section content",
                section.heading,
            )
            continue
        if normalized_heading:
            seen.add(normalized_heading)
        sections.append(section)
    return replace(draft, sections=tuple(sections)) if changed else draft


_DIRECT_QUOTE_PAIRS = {"«": "»", "“": "”", "„": "“", '"': '"'}


def _direct_quote_content_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return content ranges enclosed by direct-quote marks in reader prose."""
    stack: list[tuple[str, int]] = []
    spans: list[tuple[int, int]] = []
    for index, character in enumerate(text):
        if stack:
            expected_closing, content_start = stack[-1]
            if character == expected_closing:
                stack.pop()
                if not stack:
                    spans.append((content_start, index))
                continue
            if character in _DIRECT_QUOTE_PAIRS and character != expected_closing:
                stack.append((_DIRECT_QUOTE_PAIRS[character], index + 1))
            continue
        if character in _DIRECT_QUOTE_PAIRS:
            stack.append((_DIRECT_QUOTE_PAIRS[character], index + 1))

    # An unmatched opening quote makes the remaining text unsafe to edit as a name.
    if stack:
        spans.append((stack[0][1], len(text)))
    return tuple(spans)


def _span_is_inside_direct_quote(
    span: tuple[int, int], quote_spans: Sequence[tuple[int, int]]
) -> bool:
    start, end = span
    return any(quote_start <= start and end <= quote_end for quote_start, quote_end in quote_spans)


def _deduplicate_overlapping_spans(
    spans: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    """Keep the longest exact source-backed spelling for each overlapping mention."""
    ordered = sorted(set(spans))
    groups: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    current_end = -1
    for span in ordered:
        start, end = span
        if current and start >= current_end:
            groups.append(current)
            current = []
        current.append(span)
        current_end = max(current_end, end)
    if current:
        groups.append(current)
    return tuple(
        min(group, key=lambda span: (-(span[1] - span[0]), span[0], span[1])) for group in groups
    )


def _normalize_grounded_paragraph_prose(
    paragraph: ArticleParagraph,
    *,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
    place_resolver: Any | None,
) -> ArticleParagraph:
    """Apply narrow, source-grounded provider typography and location framing."""
    text = paragraph.text
    if not text:
        return paragraph

    cited_support_ids = tuple(
        dict.fromkeys(
            (
                *paragraph.cited_support_ids,
                *(sid for claim in paragraph.claims for sid in claim.cited_support_ids),
            )
        )
    )
    citable_support_ids = _citable_support_ids(cited_support_ids, context, material_projection)
    quoted_spans = _direct_quote_content_spans(text)
    insertions: dict[int, list[tuple[int, str]]] = {}

    provider_spans = _deduplicate_overlapping_spans(
        tuple(
            span
            for _entity_id, span in _supported_provider_mentions(
                text,
                citable_support_ids,
                context,
                place_resolver,
            )
            if 0 <= span[0] < span[1] <= len(text)
            and not _span_is_quoted_name(text, span)
            and not _span_is_inside_direct_quote(span, quoted_spans)
        )
    )
    for start, end in provider_spans:
        insertions.setdefault(start, []).append((2, "«"))
        insertions.setdefault(end, []).append((0, "»"))

    private_sector_source_texts = []
    for support_id in citable_support_ids:
        support = context.support_by_id.get(support_id)
        if support is None:
            continue
        source_text = " ".join((support.text, support.source_text)).strip()
        if _PRIVATE_SECTOR_RE.search(source_text):
            private_sector_source_texts.append(source_text)

    if (
        place_resolver is not None
        and _has_unresolved_private_sector_location(text)
        and private_sector_source_texts
        and not _has_specific_source_area(private_sector_source_texts, place_resolver)
    ):
        visible_private_sector = False
        quoted_private_sector = False
        for match in _PRIVATE_SECTOR_RE.finditer(text):
            span = match.span()
            if _span_is_inside_direct_quote(span, quoted_spans):
                quoted_private_sector = True
            else:
                visible_private_sector = True
                insertions.setdefault(span[1], []).append((1, " (район в сообщении не указан)"))
        if quoted_private_sector and not visible_private_sector:
            stripped_text = text.rstrip()
            ends_with_terminal_mark = stripped_text.endswith((".", "!", "?", "…"))
            if (
                stripped_text.endswith(("»", "”", '"'))
                and len(stripped_text) > 1
                and stripped_text[-2] in ".!?…"
            ):
                ends_with_terminal_mark = True
            separator = " " if ends_with_terminal_mark else ". "
            insertions.setdefault(len(stripped_text), []).append(
                (1, f"{separator}Район в сообщении не указан.")
            )

    if not insertions:
        return paragraph

    for position in sorted(insertions, reverse=True):
        addition = "".join(value for _order, value in sorted(insertions[position]))
        text = text[:position] + addition + text[position:]
    return replace(paragraph, text=text)


def _normalize_grounded_article_prose(
    draft: StructuredArticleDraft,
    *,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
    place_resolver: Any | None,
) -> StructuredArticleDraft:
    """Normalize only citable provider names and unlocalized private-sector prose."""
    changed = False
    sections: list[ArticleSection] = []
    for section in draft.sections:
        paragraphs = tuple(
            _normalize_grounded_paragraph_prose(
                paragraph,
                context=context,
                material_projection=material_projection,
                place_resolver=place_resolver,
            )
            for paragraph in section.paragraphs
        )
        if paragraphs != section.paragraphs:
            changed = True
            sections.append(replace(section, paragraphs=paragraphs))
        else:
            sections.append(section)
    return replace(draft, sections=tuple(sections), word_count=0) if changed else draft


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
                if any(len(token) >= 3 for token in re.findall(r"[\w-]+", prefix)):
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
                    logger.info(
                        "Re-anchored heading %s supports from section paragraphs: %s",
                        h_id,
                        cur_sups,
                    )

            if section_changed:
                new_claims = (
                    (ArticleClaimAtom(text=cur_heading, cited_support_ids=cur_sups),)
                    if cur_heading and cur_sups
                    else ()
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
        writer_assessment: ArticleAssessmentCheckpoint | None = None,
        checkpoint_observer: ArticleCheckpointObserver | None = None,
    ) -> ArticleFinalizationResult:
        """Validate writer output, trigger recovery if needed, and assert final invariants.

        Only ``writer_assessment`` proves ownership of the exact structured
        draft and input fingerprint. Loose validation/quality arguments remain
        accepted for compatibility and diagnostics but cannot authorize reuse.
        """
        validation_context = (
            materialize_article_validation_context(context, material_projection)
            if material_projection is not None
            else context
        )
        if isinstance(writer_error, TimeoutError):
            raise writer_error
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
            _reject_structural_quality_fallback(
                _unresolved_whole_draft_findings(quality_report, quality_report_after_edit),
                stage="writer_error_fallback",
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

        input_fingerprint = await asyncio.to_thread(
            article_assessment_input_fingerprint,
            context,
            coverage_plan,
            editorial_config,
            length_profile,
            material_projection,
            place_resolver,
        )
        if writer_assessment is not None and writer_assessment.matches(
            writer_draft, input_fingerprint
        ):
            writer_validation = writer_assessment.validation
        else:
            # Legacy loose reports lack proof of their draft/input ownership.
            writer_validation = None

        # 2. Validate writer draft
        if writer_validation is None:
            writer_validation = await asyncio.to_thread(
                validate_article_draft,
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
                repaired_val = await asyncio.to_thread(
                    validate_article_draft,
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
                repaired_draft = await asyncio.to_thread(
                    _prune_unsupported_paragraph_claims,
                    writer_draft,
                    claim_violations,
                    validation_context,
                    coverage_plan=coverage_plan,
                )
                repaired_val = await asyncio.to_thread(
                    validate_article_draft,
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
                repaired_draft = await asyncio.to_thread(
                    _sanitize_phantom_heading_topics,
                    writer_draft,
                    heading_violations,
                    context=validation_context,
                )
                repaired_val = await asyncio.to_thread(
                    validate_article_draft,
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
                    repaired_val = await asyncio.to_thread(
                        validate_article_draft,
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
                            "editor_retry_count",
                            0,
                        ),
                        "patched_unit_ids": _safe_writer_metadata(writer_metadata).get(
                            "editor_patched_unit_ids", []
                        ),
                        "evidence_boundary_passed": False,
                        "quality_gate_passed": None,
                    },
                )
            _reject_structural_quality_fallback(
                _unresolved_whole_draft_findings(quality_report, quality_report_after_edit),
                stage="writer_validation_fallback",
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
        writer_draft = _remove_duplicate_headings(writer_draft)
        writer_draft = await asyncio.to_thread(
            _normalize_grounded_article_prose,
            writer_draft,
            context=context,
            material_projection=material_projection,
            place_resolver=place_resolver,
        )

        if checkpoint_observer is not None:
            checkpoint_observer("finalization_candidate", writer_draft, None)

        # Structural finalization may change the reader-facing draft. Reuse
        # validation only if the full structured value remains identical;
        # otherwise the rendered value must pass a fresh Evidence Boundary check.
        evidence_started = perf_counter()
        reused_writer_validation = writer_assessment is not None and writer_assessment.matches(
            writer_draft, input_fingerprint
        )
        if reused_writer_validation and writer_assessment is not None:
            final_validation = writer_assessment.validation
        else:
            final_validation = await asyncio.to_thread(
                validate_article_draft,
                writer_draft,
                context,
                config=editorial_config,
                length_profile=length_profile,
                material_projection=material_projection,
            )
        logger.info(
            "Final article Evidence Boundary check: reused_writer_result=%s elapsed=%.3fs",
            reused_writer_validation,
            perf_counter() - evidence_started,
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
                            "editor_retry_count",
                            0,
                        ),
                        "patched_unit_ids": _safe_writer_metadata(writer_metadata).get(
                            "editor_patched_unit_ids", []
                        ),
                        "evidence_boundary_passed": False,
                        "quality_gate_passed": None,
                    },
                )
            _reject_structural_quality_fallback(
                _unresolved_whole_draft_findings(quality_report, quality_report_after_edit),
                stage="post_finalization_validation_fallback",
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
        quality_started = perf_counter()
        # Only a checkpoint establishes equality of the complete draft,
        # support mappings and every validation/quality input.
        if writer_assessment is not None and writer_assessment.matches(
            writer_draft, input_fingerprint
        ):
            final_quality = writer_assessment.quality
            reused_quality_report = True
        else:
            final_quality = await asyncio.to_thread(
                diagnose_article_quality,
                writer_draft,
                coverage_plan,
                context,
                material_projection=material_projection,
                place_resolver=place_resolver,
            )
            reused_quality_report = False
        logger.info(
            "Final article reader-quality check: reused_post_edit_report=%s elapsed=%.3fs",
            reused_quality_report,
            perf_counter() - quality_started,
        )
        excluded_story_ids = (
            material_projection.suppressed_story_ids if material_projection is not None else ()
        )
        if (
            writer_assessment is not None
            and writer_assessment.matches(writer_draft, input_fingerprint)
            and writer_assessment.coverage is not None
        ):
            ai_diag = writer_assessment.coverage
        else:
            ai_diag = await asyncio.to_thread(
                diagnose_article_coverage,
                writer_draft,
                coverage_plan,
                context=context,
                excluded_story_ids=excluded_story_ids,
            )
        final_assessment = ArticleAssessmentCheckpoint(
            writer_draft,
            final_validation,
            final_quality,
            input_fingerprint,
            ai_diag,
        )
        if checkpoint_observer is not None:
            checkpoint_observer("finalization", writer_draft, final_assessment)
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

        ai_covered = tuple(ai_diag.covered_story_ids)

        eligible_story_ids = set(coverage_plan.story_ids) - set(excluded_story_ids)
        if set(ai_covered) == eligible_story_ids:
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
            fb_validation = await asyncio.to_thread(
                validate_article_draft,
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

            fallback_quality = await asyncio.to_thread(
                diagnose_article_quality,
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
