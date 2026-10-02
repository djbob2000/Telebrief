"""Preparation, exact assessment, and immutable finalization for Event-First articles."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any, Callable, Literal, Mapping, Protocol
from uuid import uuid4

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_coverage_diagnostics import (
    ArticleCoverageDiagnostics,
    diagnose_article_coverage,
)
from src.publication.article_length import ArticleLengthProfile
from src.publication.article_material import (
    ArticleMaterialProjection,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
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
from src.publication.article_quality_policy import (
    ARTICLE_QUALITY_FINDING_POLICIES,
    article_quality_policy,
)
from src.publication.article_trace import (
    ArticleClaimTraceUnit,
    build_article_claim_trace,
)
from src.publication.article_validator import (
    ArticleValidationResult,
    validate_article_draft,
)
from src.publication.article_writer_context import ARTICLE_WRITER_CONTEXT_VERSION
from src.publication.errors import (
    ArticlePublicationRejected,
)

logger = logging.getLogger(__name__)

# Bump when Evidence Boundary validation semantics change. Reader-quality has
# its own version; the canonical policy content is fingerprinted separately.
ARTICLE_ASSESSMENT_VALIDATOR_VERSION = "article_evidence_boundary_v2"


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
    *,
    source_identity: str | None = None,
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
        "source_identity": source_identity,
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
    source_identity: str | None = None

    def matches(
        self,
        draft: StructuredArticleDraft,
        input_fingerprint: str,
        *,
        source_identity: str | None = None,
    ) -> bool:
        return (
            self.draft == draft
            and self.input_fingerprint == input_fingerprint
            and self.source_identity == source_identity
        )

    @property
    def publishable(self) -> bool:
        return self.validation.is_valid and not self.quality.blocking_findings


# Optional synchronous in-memory preview hook; no production persistence.
ArticleCheckpointObserver = Callable[
    [str, StructuredArticleDraft, ArticleAssessmentCheckpoint | None], None
]
ArticleAssessmentInputObserver = Callable[[str], None]


async def assess_article_draft(
    draft: StructuredArticleDraft,
    context: ArticleEditorialContext,
    *,
    coverage_plan: ArticleCoveragePlan | None,
    editorial_config: PublicationEditorialConfig | None,
    length_profile: ArticleLengthProfile | None = None,
    material_projection: ArticleMaterialProjection | None = None,
    place_resolver: Any | None = None,
    source_identity: str | None = None,
    input_observer: ArticleAssessmentInputObserver | None = None,
) -> ArticleAssessmentCheckpoint:
    """Assess one exact draft with the shared, provider-free assessment path."""
    fingerprint = await asyncio.to_thread(
        article_assessment_input_fingerprint,
        context,
        coverage_plan,
        editorial_config,
        length_profile,
        material_projection,
        place_resolver,
        source_identity=source_identity,
    )
    if input_observer is not None:
        input_observer(fingerprint)

    validation_work = asyncio.to_thread(
        validate_article_draft,
        draft,
        context,
        config=editorial_config,
        length_profile=length_profile,
        material_projection=material_projection,
    )
    coverage_work: Awaitable[ArticleCoverageDiagnostics | None]
    if coverage_plan is None:
        quality_work = asyncio.sleep(0, result=ArticleReaderQualityReport())
        coverage_work = asyncio.sleep(0, result=None)
    else:
        quality_work = asyncio.to_thread(
            diagnose_article_quality,
            draft,
            coverage_plan,
            context,
            material_projection=material_projection,
            place_resolver=place_resolver,
        )
        coverage_work = asyncio.to_thread(
            diagnose_article_coverage,
            draft,
            coverage_plan,
            context=context,
            excluded_story_ids=(
                material_projection.suppressed_story_ids if material_projection is not None else ()
            ),
        )
    validation, quality, coverage = await asyncio.gather(
        validation_work,
        quality_work,
        coverage_work,
    )
    return ArticleAssessmentCheckpoint(
        draft=draft,
        validation=validation,
        quality=quality,
        input_fingerprint=fingerprint,
        coverage=coverage,
        source_identity=source_identity,
    )


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


def _safe_editor_outcomes(value: Any, *, pass_outcomes: bool = False) -> list[dict[str, Any]]:
    """Retain only fixed-shape editor outcomes with safe codes and identifiers."""
    if not isinstance(value, list):
        return []
    from src.publication.article_editor import (
        ARTICLE_EDITOR_OUTCOME_REASONS,
        ARTICLE_EDITOR_PASS_REASONS,
        ARTICLE_EDITOR_UNIT_STATUSES,
    )

    unit_id_re = re.compile(r"(?:TITLE|LEAD|DRAFT|H[0-9]{3,}|P[0-9]{3,})")
    code_re = re.compile(r"[A-Z][A-Z0-9_]*(?::[A-Z0-9_]+)?")
    reason_codes = ARTICLE_EDITOR_PASS_REASONS if pass_outcomes else ARTICLE_EDITOR_OUTCOME_REASONS
    compact: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        pass_index = item.get("pass_index")
        reason = item.get("reason")
        base_fingerprint = item.get("base_fingerprint")
        if (
            isinstance(pass_index, bool)
            or pass_index not in (1, 2)
            or not isinstance(reason, str)
            or reason not in reason_codes
            or not isinstance(base_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", base_fingerprint) is None
        ):
            continue
        record: dict[str, Any] = {
            "pass_index": pass_index,
            "reason": reason,
            "base_fingerprint": base_fingerprint,
        }
        if not pass_outcomes:
            unit_id = item.get("unit_id")
            status = item.get("status")
            if (
                not isinstance(unit_id, str)
                or unit_id_re.fullmatch(unit_id) is None
                or not isinstance(status, str)
                or status not in ARTICLE_EDITOR_UNIT_STATUSES
            ):
                continue
            record.update(unit_id=unit_id, status=status)
        else:
            # The editor stores the same value in both fields during migration;
            # retain one canonical safe reason.
            if item.get("outcome") not in (None, reason):
                continue
        for key in (
            "required_support_count",
            "shown_support_count",
            "requested_unit_count",
            "applied_unit_count",
            "deferred_unit_count",
            "unresolved_target_count",
            "unknown_unit_count",
        ):
            count = item.get(key)
            if key == "unknown_unit_count" and count is None:
                count = item.get("unknown_unit_id_count")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                record[key] = count
        elapsed = item.get("elapsed_seconds")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and elapsed >= 0:
            record["elapsed_seconds"] = elapsed
        result_fingerprint = item.get("result_fingerprint")
        if isinstance(result_fingerprint, str) and re.fullmatch(
            r"[0-9a-f]{64}", result_fingerprint
        ):
            record["result_fingerprint"] = result_fingerprint
        issue_codes = item.get("issue_codes")
        if isinstance(issue_codes, list):
            record["issue_codes"] = [
                code for code in issue_codes if isinstance(code, str) and code_re.fullmatch(code)
            ]
        for key in ("requested_unit_ids", "applied_unit_ids", "deferred_unit_ids"):
            ids = item.get(key)
            if isinstance(ids, list):
                record[key] = [
                    unit_id
                    for unit_id in ids
                    if isinstance(unit_id, str) and unit_id_re.fullmatch(unit_id)
                ]
        compact.append(record)
    return compact


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
        "evidence_fact_record_count",
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
        "article_writer_context_version": {ARTICLE_WRITER_CONTEXT_VERSION},
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
    for key, is_pass_outcomes in (
        ("editor_unit_outcomes", False),
        ("editor_pass_outcomes", True),
    ):
        outcomes = _safe_editor_outcomes(writer_metadata.get(key), pass_outcomes=is_pass_outcomes)
        if outcomes:
            result[key] = outcomes
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
        metadata["writer_attempt"] = safe_writer_metadata
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


_DIRECT_QUOTE_RE = re.compile(r"(?P<opening>«|“|„|\")(?P<text>[^«»“”„\"]+)(?P<closing>»|”|\")")
_SOURCE_ATTRIBUTION_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            r"\bпо сообщениям\s+(?:в|из)\s+(?:(?:местных|городских)\s+)?чат(?:е|ах|ов)\b",
            re.IGNORECASE,
        ),
        "по сообщениям жителей",
        "по сообщениям",
    ),
    (
        re.compile(
            r"\bпо сообщению\s+(?:в|из)\s+(?:(?:местном|городском)\s+)?чате\b",
            re.IGNORECASE,
        ),
        "по сообщению жителя",
        "по сообщению",
    ),
    (
        re.compile(
            r"\bкак\s+сообщают\s+в\s+(?:(?:местном|городском)\s+)?чат(?:е|ах)\b",
            re.IGNORECASE,
        ),
        "как сообщают жители",
        "как сообщается",
    ),
    (
        re.compile(
            r"\bкак\s+сообщили\s+в\s+(?:(?:местном|городском)\s+)?чат(?:е|ах)\b",
            re.IGNORECASE,
        ),
        "как сообщили жители",
        "как сообщалось",
    ),
    (
        re.compile(
            r"\bв\s+(?:(?:местном|городском)\s+)?чат(?:е|ах)\s+сообщают\b",
            re.IGNORECASE,
        ),
        "жители сообщают",
        "в сообщениях говорится",
    ),
    (
        re.compile(
            r"\bв\s+(?:(?:местном|городском)\s+)?чате\s+сообщили\b",
            re.IGNORECASE,
        ),
        "жители сообщили",
        "в сообщении сообщили",
    ),
    (
        re.compile(
            r"\b(?:участники|пользователи)\s+(?:(?:местного|городского)\s+)?чата\s+"
            r"сообщают\b",
            re.IGNORECASE,
        ),
        "жители сообщают",
        "в сообщениях говорится",
    ),
)
_UNSPECIFIED_DISTRICT_NOTE_RE = re.compile(
    r"\bрайон\s+в\s+сообщении\s+не\s+указан\b", re.IGNORECASE
)


def _direct_quote_content_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    for match in _DIRECT_QUOTE_RE.finditer(text):
        opening = match.group("opening")
        closing = match.group("closing")
        expected_closing = {"«": "»", "“": "”", "„": "“", '"': '"'}.get(opening)
        if closing == expected_closing:
            spans.append((match.start("text"), match.end("text")))
    # An unmatched opening quote makes the remaining text unsafe to edit as a name.
    last_open = max((text.rfind(mark) for mark in ("«", "“", "„", '"')), default=-1)
    last_close = max((text.rfind(mark) for mark in ("»", "”", '"')), default=-1)
    if last_open > last_close:
        spans.append((last_open + 1, len(text)))
    return tuple(spans)


def _span_is_inside_direct_quote(
    span: tuple[int, int], quote_spans: Sequence[tuple[int, int]]
) -> bool:
    start, end = span
    return any(quote_start <= start and end <= quote_end for quote_start, quote_end in quote_spans)


def _span_is_in_question(text: str, span: tuple[int, int]) -> bool:
    start, end = span
    left = max((text.rfind(mark, 0, start) for mark in (".", "!", "?", "…")), default=-1) + 1
    right_candidates = [
        position for mark in (".", "!", "?", "…") if (position := text.find(mark, end)) >= 0
    ]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    return "?" in text[left:right]


def _deduplicate_overlapping_spans(
    spans: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
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


def _wrap_supported_organization_names(
    text: str,
    support_ids: Sequence[str],
    context: ArticleEditorialContext,
    place_resolver: Any | None,
) -> str:
    if not text:
        return text
    from src.publication.article_quality import supported_organization_name_mentions

    quote_spans = _direct_quote_content_spans(text)
    mentions = list(_supported_provider_mentions(text, support_ids, context, place_resolver))
    mentions.extend(
        supported_organization_name_mentions(text, support_ids, context, place_resolver)
    )
    spans = _deduplicate_overlapping_spans(
        tuple(
            span
            for _name, span in mentions
            if 0 <= span[0] < span[1] <= len(text)
            and not _span_is_quoted_name(text, span)
            and not _span_is_inside_direct_quote(span, quote_spans)
        )
    )
    if not spans:
        return text
    insertions: dict[int, list[tuple[int, str]]] = {}
    for start, end in spans:
        insertions.setdefault(start, []).append((2, "«"))
        insertions.setdefault(end, []).append((0, "»"))
    for position in sorted(insertions, reverse=True):
        addition = "".join(value for _order, value in sorted(insertions[position]))
        text = text[:position] + addition + text[position:]
    return text


def _all_supported_roles_are_community(
    support_ids: Sequence[str], context: ArticleEditorialContext
) -> bool:
    supports = [context.support_by_id[sid] for sid in support_ids if sid in context.support_by_id]
    return bool(supports) and all(support.source_roles == ("community",) for support in supports)


def _attribution_sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for sentence in _split_sentences_safe(text):
        start = text.find(sentence, cursor)
        if start < 0:
            continue
        end = start + len(sentence)
        spans.append((start, end))
        cursor = end
    return tuple(spans)


def _normalize_attribution_sentence(text: str) -> str:
    return " ".join(text.split()).casefold()


def _source_attribution_edits(
    text: str,
    claims: Sequence[ArticleClaimAtom],
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
) -> tuple[tuple[re.Pattern[str], str], ...]:
    if not claims:
        return ()
    quote_spans = _direct_quote_content_spans(text)
    sentence_spans = _attribution_sentence_spans(text)
    edits: list[tuple[re.Pattern[str], str]] = []
    for pattern, community_text, neutral_text in _SOURCE_ATTRIBUTION_RULES:
        text_matches = [
            match
            for match in pattern.finditer(text)
            if not _span_is_inside_direct_quote(match.span(), quote_spans)
            and not _span_is_in_question(text, match.span())
        ]
        if not text_matches:
            continue

        matches_by_sentence: dict[tuple[int, int], list[re.Match[str]]] = {}
        for match in text_matches:
            containing_span = next(
                (
                    span
                    for span in sentence_spans
                    if span[0] <= match.start() and match.end() <= span[1]
                ),
                None,
            )
            if containing_span is None:
                matches_by_sentence = {}
                break
            matches_by_sentence.setdefault(containing_span, []).append(match)
        if not matches_by_sentence:
            continue

        attribution_claim_indexes: set[int] = set()
        for claim_index, claim in enumerate(claims):
            claim_quote_spans = _direct_quote_content_spans(claim.text)
            if any(
                not _span_is_inside_direct_quote(match.span(), claim_quote_spans)
                and not _span_is_in_question(claim.text, match.span())
                for match in pattern.finditer(claim.text)
            ):
                attribution_claim_indexes.add(claim_index)

        replacements: set[str] = set()
        bound_claim_indexes: set[int] = set()
        exact_mapping = True
        for (start, end), sentence_matches in matches_by_sentence.items():
            sentence = text[start:end]
            normalized_sentence = _normalize_attribution_sentence(sentence)
            matching_claims: list[tuple[ArticleClaimAtom, tuple[str, ...]]] = []
            for claim in claims:
                if _normalize_attribution_sentence(claim.text) != normalized_sentence:
                    continue
                claim_quote_spans = _direct_quote_content_spans(claim.text)
                claim_matches = [
                    match
                    for match in pattern.finditer(claim.text)
                    if not _span_is_inside_direct_quote(match.span(), claim_quote_spans)
                    and not _span_is_in_question(claim.text, match.span())
                ]
                if len(claim_matches) != len(sentence_matches):
                    continue
                claim_citable_ids = _citable_support_ids(
                    claim.cited_support_ids, context, material_projection
                )
                if claim_citable_ids:
                    matching_claims.append((claim, claim_citable_ids))
            if len(matching_claims) != 1:
                # Bind the phrase to the exact prose sentence its atom represents;
                # a similar phrase elsewhere in the unit is insufficient.
                exact_mapping = False
                break
            claim_index = next(
                index for index, claim in enumerate(claims) if claim is matching_claims[0][0]
            )
            bound_claim_indexes.add(claim_index)
            _claim, citable_ids = matching_claims[0]
            replacements.add(
                community_text
                if _all_supported_roles_are_community(citable_ids, context)
                else neutral_text
            )
        if not exact_mapping or bound_claim_indexes != attribution_claim_indexes:
            # Applying the phrase to every atom in the paragraph is safe only
            # when every atom it would change was bound to an exact prose sentence.
            continue
        if len(replacements) != 1:
            # Distinct assertions with distinct roles cannot be represented safely
            # by a unit-wide substitution.
            continue
        edits.append((pattern, replacements.pop()))
    return tuple(edits)


def _capitalize_like(source: str, replacement: str) -> str:
    if source and source[0].isupper() and replacement:
        return replacement[0].upper() + replacement[1:]
    return replacement


def _apply_attribution_edits(
    text: str,
    edits: Sequence[tuple[re.Pattern[str], str]],
    *,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
    support_ids: Sequence[str],
) -> tuple[str, bool]:
    if not edits or not _citable_support_ids(support_ids, context, material_projection):
        return text, False
    changed = False
    for pattern, replacement in edits:
        # Earlier substitutions can change offsets. Recompute against the current
        # text so later patterns still leave direct speech untouched.
        quote_spans = _direct_quote_content_spans(text)
        source_text = text

        def replace_attribution(
            match: re.Match[str],
            *,
            source_text_snapshot: str = source_text,
            source_quote_spans: Sequence[tuple[int, int]] = quote_spans,
            replacement_text: str = replacement,
        ) -> str:
            nonlocal changed
            if _span_is_inside_direct_quote(
                match.span(), source_quote_spans
            ) or _span_is_in_question(source_text_snapshot, match.span()):
                return match.group(0)
            changed = True
            return _capitalize_like(match.group(0), replacement_text)

        text = pattern.sub(replace_attribution, text)
    return text, changed


def _add_unknown_district_context(
    text: str,
    support_ids: Sequence[str],
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
    place_resolver: Any | None,
) -> tuple[str, tuple[str, ...]]:
    if (
        not text
        or place_resolver is None
        or _UNSPECIFIED_DISTRICT_NOTE_RE.search(text)
        or not _has_unresolved_private_sector_location(text)
    ):
        return text, ()
    private_sector_support_ids = tuple(
        sid
        for sid in _citable_support_ids(support_ids, context, material_projection)
        if (support := context.support_by_id.get(sid)) is not None
        and _PRIVATE_SECTOR_RE.search(" ".join((support.text, support.source_text)))
    )
    if not private_sector_support_ids:
        return text, ()
    source_texts = [
        " ".join((context.support_by_id[sid].text, context.support_by_id[sid].source_text)).strip()
        for sid in private_sector_support_ids
    ]
    if _has_specific_source_area(source_texts, place_resolver):
        return text, ()

    quote_spans = _direct_quote_content_spans(text)
    visible_private_sector = False
    quoted_private_sector = False
    for match in _PRIVATE_SECTOR_RE.finditer(text):
        if _span_is_inside_direct_quote(match.span(), quote_spans):
            quoted_private_sector = True
            continue
        visible_private_sector = True
        text = text[: match.end()] + " (район в сообщении не указан)" + text[match.end() :]
        # The phrase is unique in the unit in normal prose; avoid a second
        # replacement against shifted offsets.
        break
    if visible_private_sector:
        return text, private_sector_support_ids
    if quoted_private_sector:
        stripped_text = text.rstrip()
        ends_with_terminal_mark = stripped_text.endswith((".", "!", "?", "…"))
        if (
            stripped_text.endswith(("»", "”", '"'))
            and len(stripped_text) > 1
            and stripped_text[-2] in ".!?…"
        ):
            ends_with_terminal_mark = True
        separator = " " if ends_with_terminal_mark else ". "
        return (
            stripped_text + separator + "Район в сообщении не указан.",
            private_sector_support_ids,
        )
    return text, ()


def _prepare_text_and_claims(
    text: str,
    claims: Sequence[ArticleClaimAtom],
    support_ids: Sequence[str],
    *,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
    place_resolver: Any | None,
    clarify_unknown_district: bool,
) -> tuple[str, tuple[ArticleClaimAtom, ...], tuple[str, ...]]:
    all_unit_support_ids = tuple(
        dict.fromkeys((*support_ids, *(sid for claim in claims for sid in claim.cited_support_ids)))
    )
    citable_unit_ids = _citable_support_ids(all_unit_support_ids, context, material_projection)
    prepared_text = _wrap_supported_organization_names(
        text, citable_unit_ids, context, place_resolver
    )
    added_claim_supports: tuple[str, ...] = ()
    if clarify_unknown_district:
        prepared_text, added_claim_supports = _add_unknown_district_context(
            prepared_text,
            citable_unit_ids,
            context,
            material_projection,
            place_resolver,
        )

    formatted_claims = tuple(
        replace(claim, text=formatted_text) if formatted_text != claim.text else claim
        for claim in claims
        for formatted_text in (
            _wrap_supported_organization_names(
                claim.text,
                _citable_support_ids(claim.cited_support_ids, context, material_projection),
                context,
                place_resolver,
            ),
        )
    )
    # Attribute only a phrase whose current Claim Atom already ties it to an
    # eligible cited support and exactly matches the prose sentence. Otherwise
    # the editor receives the unchanged text.
    attribution_edits = _source_attribution_edits(
        prepared_text,
        formatted_claims,
        context,
        material_projection,
    )
    prepared_text, _attribution_changed = _apply_attribution_edits(
        prepared_text,
        attribution_edits,
        context=context,
        material_projection=material_projection,
        support_ids=citable_unit_ids,
    )

    new_claims: list[ArticleClaimAtom] = []
    for claim in formatted_claims:
        claim_text = claim.text
        claim_citable_ids = _citable_support_ids(
            claim.cited_support_ids, context, material_projection
        )
        rewritten_claim, attribution_changed = _apply_attribution_edits(
            claim_text,
            attribution_edits,
            context=context,
            material_projection=material_projection,
            support_ids=claim_citable_ids,
        )
        if attribution_changed:
            new_claims.append(
                replace(
                    claim,
                    text=rewritten_claim,
                    cited_support_ids=claim_citable_ids,
                )
            )
        else:
            new_claims.append(claim)

    if added_claim_supports:
        note_claim = ArticleClaimAtom(
            text="Район в сообщении не указан.",
            cited_support_ids=added_claim_supports,
        )
        if not any(claim.text == note_claim.text for claim in new_claims):
            new_claims.append(note_claim)
    return prepared_text, tuple(new_claims), added_claim_supports


def prepare_article_draft(
    draft: StructuredArticleDraft,
    *,
    context: ArticleEditorialContext,
    material_projection: ArticleMaterialProjection | None,
    place_resolver: Any | None,
) -> StructuredArticleDraft:
    """Apply only the closed, evidence-preserving article preparation edits."""
    title_supports = tuple(
        dict.fromkeys(
            (
                *draft.title_support_ids,
                *(sid for claim in draft.title_claims for sid in claim.cited_support_ids),
            )
        )
    )
    title, title_claims, _ = _prepare_text_and_claims(
        draft.title,
        draft.title_claims,
        title_supports,
        context=context,
        material_projection=material_projection,
        place_resolver=place_resolver,
        clarify_unknown_district=False,
    )
    lead_supports = tuple(
        dict.fromkeys(
            (
                *draft.lead_support_ids,
                *(sid for claim in draft.lead_claims for sid in claim.cited_support_ids),
            )
        )
    )
    lead, lead_claims, _ = _prepare_text_and_claims(
        draft.lead,
        draft.lead_claims,
        lead_supports,
        context=context,
        material_projection=material_projection,
        place_resolver=place_resolver,
        clarify_unknown_district=True,
    )
    sections: list[ArticleSection] = []
    changed = (
        title != draft.title
        or title_claims != draft.title_claims
        or lead != draft.lead
        or lead_claims != draft.lead_claims
    )
    for section in draft.sections:
        heading_supports = tuple(
            dict.fromkeys(
                (
                    *section.heading_support_ids,
                    *(sid for claim in section.heading_claims for sid in claim.cited_support_ids),
                )
            )
        )
        heading, heading_claims, _ = _prepare_text_and_claims(
            section.heading,
            section.heading_claims,
            heading_supports,
            context=context,
            material_projection=material_projection,
            place_resolver=place_resolver,
            clarify_unknown_district=False,
        )
        paragraphs: list[ArticleParagraph] = []
        for paragraph in section.paragraphs:
            paragraph_supports = tuple(
                dict.fromkeys(
                    (
                        *paragraph.cited_support_ids,
                        *(sid for claim in paragraph.claims for sid in claim.cited_support_ids),
                    )
                )
            )
            paragraph_text, paragraph_claims, note_supports = _prepare_text_and_claims(
                paragraph.text,
                paragraph.claims,
                paragraph_supports,
                context=context,
                material_projection=material_projection,
                place_resolver=place_resolver,
                clarify_unknown_district=True,
            )
            new_paragraph = replace(
                paragraph,
                text=paragraph_text,
                claims=paragraph_claims,
            )
            paragraphs.append(new_paragraph)
            changed = changed or new_paragraph != paragraph
        new_section = replace(
            section,
            heading=heading,
            heading_claims=heading_claims,
            paragraphs=tuple(paragraphs),
        )
        sections.append(new_section)
        changed = changed or new_section != section
    if not changed:
        return draft
    return replace(
        draft,
        title=title,
        title_claims=title_claims,
        lead=lead,
        lead_claims=lead_claims,
        sections=tuple(sections),
        word_count=0,
    )


class ArticleFinalizer:
    """Gate one exact assessed candidate without rewriting its prose."""

    def __init__(self, composer: Any | None = None) -> None:
        # Kept as a constructor compatibility parameter; deterministic composition
        # is no longer part of Event-First finalization.
        self.composer = composer

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
        source_identity: str | None = None,
        assessment_input_observer: ArticleAssessmentInputObserver | None = None,
    ) -> ArticleFinalizationResult:
        """Assess or reuse an exact checkpoint, then accept or reject it unchanged.

        Loose validation and quality arguments remain available for diagnostic
        compatibility. Only a checkpoint matching the full draft, current inputs,
        and source identity can authorize assessment reuse.
        """
        if isinstance(writer_error, (TimeoutError, asyncio.CancelledError)):
            raise writer_error
        if writer_error is not None or writer_draft is None:
            if attempt_observer is not None:
                error_metadata: dict[str, Any] = {
                    "writer_status": "failed",
                    "exception_type": type(writer_error).__name__
                    if writer_error is not None
                    else "EmptyWriterResponse",
                    "stage": "writer",
                }
                safe_writer_metadata = _safe_writer_metadata(writer_metadata)
                if safe_writer_metadata:
                    error_metadata["writer_attempt"] = safe_writer_metadata
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_writer_rejected",
                    metadata=error_metadata,
                )
            raise ArticlePublicationRejected(
                reason="writer_failed",
                message=(
                    "Article writer failed: "
                    f"{type(writer_error).__name__ if writer_error is not None else 'EmptyWriterResponse'}"
                ),
                metadata={
                    "stage": "writer",
                    "exception_type": type(writer_error).__name__
                    if writer_error is not None
                    else "EmptyWriterResponse",
                    "writer_attempt": _safe_writer_metadata(writer_metadata),
                },
            )

        if checkpoint_observer is not None:
            # This is deliberately unassessed. If identity construction or any
            # validator fails, preview can still show the exact candidate safely.
            checkpoint_observer("finalization_candidate", writer_draft, None)

        try:
            input_fingerprint = await asyncio.to_thread(
                article_assessment_input_fingerprint,
                context,
                coverage_plan,
                editorial_config,
                length_profile,
                material_projection,
                place_resolver,
                source_identity=source_identity,
            )
            if writer_assessment is not None and writer_assessment.matches(
                writer_draft,
                input_fingerprint,
                source_identity=source_identity,
            ):
                if assessment_input_observer is not None:
                    assessment_input_observer(input_fingerprint)
                assessment = writer_assessment
                if assessment.coverage is None:
                    coverage = await asyncio.to_thread(
                        diagnose_article_coverage,
                        writer_draft,
                        coverage_plan,
                        context=context,
                        excluded_story_ids=(
                            material_projection.suppressed_story_ids
                            if material_projection is not None
                            else ()
                        ),
                    )
                    assessment = replace(assessment, coverage=coverage)
            else:
                assessment = await assess_article_draft(
                    writer_draft,
                    context,
                    coverage_plan=coverage_plan,
                    editorial_config=editorial_config,
                    length_profile=length_profile,
                    material_projection=material_projection,
                    place_resolver=place_resolver,
                    source_identity=source_identity,
                    input_observer=assessment_input_observer,
                )
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception as exc:
            if attempt_observer is not None:
                try:
                    await attempt_observer.attempt_finished(
                        writer_attempt_id,
                        status="failed",
                        error_kind="article_assessment_failed",
                        metadata={
                            "stage": "finalization_assessment",
                            "exception_type": type(exc).__name__,
                        },
                    )
                except (TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as observer_error:
                    # Preserve the original assessment failure while recording
                    # the observer's safe exception type for operations.
                    logger.warning(
                        "Failed to close article attempt after assessment error (%s: %s)",
                        type(exc).__name__,
                        type(observer_error).__name__,
                    )
            raise

        # This is the sole authoritative finalization checkpoint. It precedes
        # both successful and rejected terminal attempt/publication outcomes.
        if checkpoint_observer is not None:
            checkpoint_observer("finalization", assessment.draft, assessment)

        if not assessment.validation.is_valid:
            rejection_metadata: dict[str, Any] = {
                "stage": "post_finalization_validation",
                "factual_validation": _compact_validation_metadata(assessment.validation),
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
            }
            safe_writer_metadata = _safe_writer_metadata(writer_metadata)
            if safe_writer_metadata:
                rejection_metadata["writer_attempt"] = safe_writer_metadata
            if attempt_observer is not None:
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_validation_rejected",
                    metadata={
                        "writer_status": "rejected",
                        **rejection_metadata,
                    },
                )
            raise ArticlePublicationRejected(
                reason="validation_failed",
                message="Final article draft failed Evidence Boundary validation",
                metadata=rejection_metadata,
            )

        if assessment.quality.blocking_findings:
            rejection_metadata = _quality_rejection_metadata(
                assessment.quality,
                quality_report_before_edit=quality_report,
                quality_report_after_edit=quality_report_after_edit,
                writer_metadata=writer_metadata,
                validation=assessment.validation,
            )
            rejection_metadata["stage"] = "post_finalization_quality"
            if attempt_observer is not None:
                await attempt_observer.attempt_finished(
                    writer_attempt_id,
                    status="failed",
                    error_kind="article_quality_rejected",
                    metadata={
                        "writer_status": "rejected",
                        **rejection_metadata,
                    },
                )
            raise ArticlePublicationRejected(
                reason="quality_failed",
                message=(
                    "Final article draft failed reader-quality validation: "
                    f"{[finding.code for finding in assessment.quality.blocking_findings]}"
                ),
                metadata=rejection_metadata,
            )

        final_draft = assessment.draft
        final_coverage = assessment.coverage
        if final_coverage is None:
            # ``coverage_plan`` is required by this compatibility entry point,
            # so absence indicates an incomplete caller-owned checkpoint.
            final_coverage = await asyncio.to_thread(
                diagnose_article_coverage,
                final_draft,
                coverage_plan,
                context=context,
                excluded_story_ids=(
                    material_projection.suppressed_story_ids
                    if material_projection is not None
                    else ()
                ),
            )
            assessment = replace(assessment, coverage=final_coverage)
        trace = build_article_claim_trace(final_draft, context)
        covered_story_ids = tuple(final_coverage.covered_story_ids)
        eligible_story_ids = set(coverage_plan.story_ids) - set(
            material_projection.suppressed_story_ids if material_projection is not None else ()
        )
        metadata = _build_final_metadata(
            winning_kind="event_article_writer",
            writer_status="passed",
            recovery_mode="none",
            coverage_plan=coverage_plan,
            ai_covered_story_ids=covered_story_ids,
            supplemented_story_ids=(),
            final_covered_story_ids=covered_story_ids,
            ai_diag=final_coverage,
            final_diag=final_coverage,
            trace=trace,
            quality_report=assessment.quality,
            quality_report_before_edit=quality_report,
            quality_report_after_edit=quality_report_after_edit,
            material_projection=material_projection,
        )
        if set(covered_story_ids) != eligible_story_ids:
            # Story coverage remains a reader-readiness diagnostic, never a veto.
            metadata["coverage_only_diagnostic"] = True
        safe_writer_metadata = _safe_writer_metadata(writer_metadata)
        if safe_writer_metadata:
            metadata["writer_attempt"] = safe_writer_metadata
            for key, value in safe_writer_metadata.items():
                metadata.setdefault(key, value)
        if attempt_observer is not None:
            await attempt_observer.attempt_finished(
                writer_attempt_id,
                status="succeeded",
                metadata=metadata,
            )
        return ArticleFinalizationResult(
            draft=final_draft,
            claim_trace=trace,
            writer_status="passed",
            recovery_mode="none",
            ai_covered_story_ids=covered_story_ids,
            supplemented_story_ids=(),
            final_covered_story_ids=covered_story_ids,
            metadata=metadata,
        )
