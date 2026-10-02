"""Read-only replay of an existing frozen article publication run."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from src.article_generator import ArticleGenerator
from src.config_loader import Config
from src.publication.article_finalization import (
    ArticleAssessmentCheckpoint,
    ArticleCheckpointObserver,
    _assessment_input_value,
)
from src.publication.article_models import StructuredArticleDraft
from src.publication.article_writer_context import ARTICLE_WRITER_CONTEXT_VERSION
from src.publication.errors import ArticleFinalizationInvariantError, ArticlePublicationRejected
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.policies import ARTICLE_PUBLICATION_TYPES
from src.publication.repository import PublicationRepository
from src.runtime import get_runtime
from src.timezones import get_timezone, normalize_timezone_name

logger = logging.getLogger(__name__)


ArticlePreviewStatus = Literal["accepted", "rejected", "failed"]

_CHECKPOINT_STAGES = frozenset(
    {
        "writer_candidate",
        "writer",
        "prepared_candidate",
        "prepared",
        "editor_base",
        "editor_candidate",
        "editor",
        "finalization_candidate",
        "finalization",
    }
)
_VALIDATION_CODES = frozenset(
    {
        "EMPTY_TITLE",
        "EMPTY_LEAD",
        "SECTION_COUNT_OUT_OF_BOUNDS",
        "WORD_COUNT_OUT_OF_BOUNDS",
        "REPORTING_WINDOW_EXPANSION",
        "INTERNAL_HANDLE_LEAK",
        "LEAKED_META_OMISSION",
        "CHAT_KITCHEN_LEAK",
        "REPEATED_CONTENT_LOOP",
        "MISSING_CLAIM_ATOMS",
        "MISSING_CLAIM_SUPPORT",
        "CLAIM_SUPPORT_MISMATCH",
        "UNKNOWN_SUPPORT_ID",
        "UNKNOWN_CLAIM_SUPPORT_ID",
        "UNSUPPORTED_PROXIMITY_RELATION",
        "INVALID_SUPPORT_POLICY",
        "HISTORICAL_CONTEXT_UNFRAMED",
        "FUTURE_CONTEXT_UNFRAMED",
        "QUESTION_CONTEXT_OVERCLAIM",
        "UNSUPPORTED_DIRECT_QUOTE",
        "UNSUPPORTED_PROPER_NAME",
        "UNSUPPORTED_CRITICAL_TERM",
        "UNSUPPORTED_CONCRETE_CLAIM",
        "UNSUPPORTED_CAUSAL_RELATION",
        "UNSUPPORTED_CLAIM_ATOM",
        "CLAIM_LEXICAL_DIVERGENCE",
        "PHANTOM_HEADING_TOPIC",
        "MISSING_SUPPORT:title",
        "MISSING_SUPPORT:lead",
        "MISSING_SUPPORT:heading",
        "MISSING_SUPPORT:paragraph",
    }
)


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _draft_hash(draft: StructuredArticleDraft) -> str:
    from src.publication.article_editor import _draft_fingerprint

    return _draft_fingerprint(draft)


def _draft_unit_ids(draft: StructuredArticleDraft) -> frozenset[str]:
    ids = {"TITLE", "LEAD", "DRAFT"}
    paragraph_count = 0
    for index, section in enumerate(draft.sections, 1):
        ids.add(f"H{index:03d}")
        for _paragraph in section.paragraphs:
            paragraph_count += 1
            ids.add(f"P{paragraph_count:03d}")
    return frozenset(ids)


@dataclass(frozen=True)
class ArticlePreviewCandidate:
    """In-memory candidate prose paired with the checkpoint assessment, if any."""

    run_id: int
    title: str
    lead: str
    body: str
    checkpoint_stage: str
    publication_type: str | None = None
    edition_slug: str | None = None
    snapshot_at: dt.datetime | None = None
    draft: StructuredArticleDraft | None = field(default=None, repr=False, compare=False)
    assessment: ArticleAssessmentCheckpoint | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def markdown(self) -> str:
        """Render the article as Markdown without adding synthetic prose."""
        parts = [f"# {self.title}"] if self.title else []
        if self.lead and not (self.body and self.body.startswith(self.lead)):
            parts.extend(["", self.lead])
        if self.body:
            parts.extend(["", self.body])
        return "\n".join(parts).rstrip() + "\n"


@dataclass(frozen=True)
class ArticleRunPreviewOutcome:
    """Typed dry-run result; the error is retained but excluded from safe metadata."""

    status: ArticlePreviewStatus
    candidate: ArticlePreviewCandidate | None
    diagnostics: dict[str, object]
    error: Exception | None = field(default=None, repr=False, compare=False)


@dataclass
class _CapturedAttempt:
    attempt_id: int
    kind: str
    provider: str | None = None
    model: str | None = None
    prompt_hash: str | None = None
    status: str = "started"
    error_kind: str | None = None
    writer_metadata: dict[str, Any] = field(default_factory=dict)
    final_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _CapturedCheckpoint:
    stage: str
    draft: StructuredArticleDraft
    assessment: ArticleAssessmentCheckpoint | None


class _MemoryCheckpointObserver:
    """Keep exact checkpoint drafts and assessments in process memory only."""

    def __init__(self, *, source_identity: str | None = None) -> None:
        self.checkpoints: list[_CapturedCheckpoint] = []
        self.assessment_pair_mismatch_count = 0
        self.source_identity = source_identity
        self.expected_input_fingerprint: str | None = None
        self.registered_units: dict[str, frozenset[str]] = {}

    def bind_assessment_inputs(self, input_fingerprint: str) -> None:
        if not _valid_hash(input_fingerprint):
            raise ArticleFinalizationInvariantError("Invalid assessment input fingerprint")
        self.expected_input_fingerprint = input_fingerprint

    def __call__(
        self,
        stage: str,
        draft: StructuredArticleDraft,
        assessment: ArticleAssessmentCheckpoint | None,
    ) -> None:
        captured_assessment = assessment
        captured_draft = draft
        if stage not in _CHECKPOINT_STAGES:
            raise ArticleFinalizationInvariantError("Unknown article checkpoint stage")
        if assessment is not None:
            if (
                self.expected_input_fingerprint is not None
                and self.source_identity is not None
                and assessment.matches(
                    draft, self.expected_input_fingerprint, source_identity=self.source_identity
                )
            ):
                captured_draft = assessment.draft
            else:
                captured_assessment = None
                self.assessment_pair_mismatch_count += 1
        self.registered_units[_draft_hash(captured_draft)] = _draft_unit_ids(captured_draft)
        self.checkpoints.append(
            _CapturedCheckpoint(
                stage=stage,
                draft=captured_draft,
                assessment=captured_assessment,
            )
        )

    @property
    def last_assessed(self) -> _CapturedCheckpoint | None:
        return next(
            (item for item in reversed(self.checkpoints) if item.assessment is not None), None
        )

    def final_checkpoint(self) -> _CapturedCheckpoint:
        if not self.checkpoints or self.checkpoints[-1].stage != "finalization":
            raise ArticleFinalizationInvariantError("No authoritative finalization checkpoint")
        checkpoint = self.checkpoints[-1]
        if checkpoint.assessment is None:
            raise ArticleFinalizationInvariantError("Finalization checkpoint is unassessed")
        return checkpoint

    def diagnostics(self) -> list[dict[str, object]]:
        return [
            {
                "stage": checkpoint.stage,
                "assessment_available": checkpoint.assessment is not None,
                "draft_fingerprint": _draft_hash(checkpoint.draft),
                **(
                    {"assessment": _assessment_metadata(checkpoint.assessment)}
                    if checkpoint.assessment is not None
                    else {}
                ),
            }
            for checkpoint in self.checkpoints
        ]


class _MemoryGenerationAttemptObserver:
    """Capture attempt diagnostics in process memory without database writes."""

    def __init__(self, checkpoints: _MemoryCheckpointObserver | None = None) -> None:
        self._next_id = 0
        self._attempts: dict[int, _CapturedAttempt] = {}
        self.checkpoints = checkpoints

    async def attempt_started(
        self,
        kind: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        self._next_id += 1
        attempt = _CapturedAttempt(
            attempt_id=self._next_id,
            kind=kind,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            writer_metadata=_safe_writer_metadata(metadata, self.checkpoints),
        )
        self._attempts[attempt.attempt_id] = attempt
        return attempt.attempt_id

    async def attempt_finished(
        self,
        attempt_id: int,
        status: str,
        *,
        error_kind: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        attempt = self._attempts.get(attempt_id)
        if attempt is None:
            return
        attempt.status = status
        attempt.error_kind = error_kind
        attempt.final_metadata = _safe_final_metadata(metadata, self.checkpoints)

    def to_metadata(self) -> dict[str, object]:
        return {
            "attempt_count": len(self._attempts),
            "attempts": [
                {
                    "kind": attempt.kind,
                    "status": attempt.status,
                    **({"provider": attempt.provider} if attempt.provider else {}),
                    **({"model": attempt.model} if attempt.model else {}),
                    **({"prompt_hash": attempt.prompt_hash} if attempt.prompt_hash else {}),
                    **({"error_kind": attempt.error_kind} if attempt.error_kind else {}),
                    **({"writer": attempt.writer_metadata} if attempt.writer_metadata else {}),
                    **({"final": attempt.final_metadata} if attempt.final_metadata else {}),
                }
                for attempt in self._attempts.values()
            ],
        }


def _safe_structural_operations(
    value: Any,
    checkpoints: _MemoryCheckpointObserver | None = None,
    pass_outcomes: Any = None,
) -> list[dict[str, Any]]:
    """Allow only operation codes and syntactically valid pass-local IDs."""
    from src.publication.article_editor import ARTICLE_STRUCTURAL_OUTCOME_REASONS

    if not isinstance(value, list):
        return []
    pass_bases = {
        outcome["pass_index"]: outcome["base_fingerprint"]
        for outcome in _safe_editor_outcomes(pass_outcomes, checkpoints, pass_outcomes=True)
    }
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        reason = item.get("reason")
        if (
            not isinstance(status, str)
            or status not in {"proposed", "applied", "rejected", "skipped"}
            or not isinstance(reason, str)
            or reason not in ARTICLE_STRUCTURAL_OUTCOME_REASONS
        ):
            continue
        base = pass_bases.get(item.get("attempt"))
        registered = (
            checkpoints.registered_units.get(base, frozenset())
            if checkpoints and base
            else frozenset()
        )
        compact: dict[str, Any] = {"status": status, "reason": reason}
        if isinstance(item.get("attempt"), int) and item["attempt"] in {1, 2}:
            compact["attempt"] = item["attempt"]
        operation_id = item.get("operation_id")
        if isinstance(operation_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", operation_id):
            compact["operation_id"] = operation_id
        if isinstance(item.get("kind"), str) and item["kind"] in {
            "move",
            "recompose",
            "create_section",
        }:
            compact["kind"] = item["kind"]
        sources = item.get("source_unit_ids")
        if isinstance(sources, list) and all(
            isinstance(source, str) and source in registered and re.fullmatch(r"P[0-9]{3,}", source)
            for source in sources
        ):
            compact["source_unit_ids"] = sources
        for key, pattern in (
            ("destination_section_id", r"(?:H[0-9]{3,}|new:[A-Za-z0-9_-]{1,64})"),
            ("destination_before_unit_id", r"P[0-9]{3,}"),
        ):
            destination = item.get(key)
            if destination is None or (
                isinstance(destination, str)
                and re.fullmatch(pattern, destination)
                and (destination in registered or destination.startswith("new:"))
            ):
                compact[key] = destination
        result.append(compact)
    return result


def _safe_editor_outcomes(
    value: Any, checkpoints: _MemoryCheckpointObserver | None, *, pass_outcomes: bool = False
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    from src.publication.article_editor import (
        ARTICLE_EDITOR_OUTCOME_REASONS,
        ARTICLE_EDITOR_PASS_REASONS,
        ARTICLE_EDITOR_UNIT_STATUSES,
    )
    from src.publication.article_quality_policy import ARTICLE_QUALITY_FINDING_POLICIES

    allowed_codes = _VALIDATION_CODES | ARTICLE_QUALITY_FINDING_POLICIES.keys()
    compact: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        pass_index = item.get("pass_index")
        if (
            not isinstance(pass_index, int)
            or isinstance(pass_index, bool)
            or pass_index not in (1, 2)
        ):
            continue
        base = item.get("base_fingerprint")
        if not isinstance(base, str) or not _valid_hash(base):
            continue
        registered = (
            checkpoints.registered_units.get(base, frozenset()) if checkpoints else frozenset()
        )
        record: dict[str, Any] = {"pass_index": item["pass_index"], "base_fingerprint": base}
        reason = item.get("reason")
        allowed_reasons = (
            ARTICLE_EDITOR_PASS_REASONS if pass_outcomes else ARTICLE_EDITOR_OUTCOME_REASONS
        )
        if not isinstance(reason, str) or reason not in allowed_reasons:
            continue
        record["reason"] = reason
        if not pass_outcomes:
            unit_id, status = item.get("unit_id"), item.get("status")
            if not isinstance(unit_id, str) or unit_id not in registered:
                continue
            if not isinstance(status, str) or status not in ARTICLE_EDITOR_UNIT_STATUSES:
                continue
            record.update(unit_id=unit_id, status=status)
        for key in (
            "required_support_count",
            "shown_support_count",
            "requested_unit_count",
            "applied_unit_count",
            "deferred_unit_count",
            "unresolved_target_count",
            "unknown_unit_count",
        ):
            count = item.get(
                key, item.get("unknown_unit_id_count") if key == "unknown_unit_count" else None
            )
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                record[key] = count
        elapsed = item.get("elapsed_seconds")
        if (
            isinstance(elapsed, (int, float))
            and not isinstance(elapsed, bool)
            and math.isfinite(elapsed)
            and elapsed >= 0
        ):
            record["elapsed_seconds"] = elapsed
        if _valid_hash(item.get("result_fingerprint")):
            record["result_fingerprint"] = item["result_fingerprint"]
        codes = item.get("issue_codes")
        if isinstance(codes, list):
            record["issue_codes"] = [
                code for code in codes if isinstance(code, str) and code in allowed_codes
            ]
        for key in ("requested_unit_ids", "applied_unit_ids", "deferred_unit_ids"):
            ids = item.get(key)
            if isinstance(ids, list):
                record[key] = [
                    unit_id for unit_id in ids if isinstance(unit_id, str) and unit_id in registered
                ]
        compact.append(record)
    return compact


def _safe_validation_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    if isinstance(value.get("is_valid"), bool):
        result["is_valid"] = value["is_valid"]
    for key in ("issue_count", "violation_count"):
        if isinstance(value.get(key), int) and not isinstance(value[key], bool):
            result[key] = value[key]
    issues = value.get("issues", value.get("blocking_issues", []))
    result["issues"] = []
    if isinstance(issues, (list, tuple)):
        for issue in issues:
            if not isinstance(issue, dict) or issue.get("code") not in _VALIDATION_CODES:
                continue
            unit = issue.get("unit_id")
            if not isinstance(unit, str) or not re.fullmatch(
                r"(?:TITLE|LEAD|DRAFT|[HP][0-9]{3,}|)", unit
            ):
                continue
            result["issues"].append(
                {
                    "code": issue["code"],
                    "unit_id": unit,
                    "severity": issue.get("severity")
                    if issue.get("severity") in {"error", "warning"}
                    else "error",
                    "blocking": issue.get("blocking")
                    if isinstance(issue.get("blocking"), bool)
                    else True,
                }
            )
    return result


def _safe_writer_metadata(
    value: Any, checkpoints: _MemoryCheckpointObserver | None = None
) -> dict[str, Any]:
    """Keep only safe invocation and provider-attempt counters and durations."""
    if not isinstance(value, dict):
        return {}

    scalar_keys = {
        "attempt",
        "attempt_number",
        "logical_writer_invocation",
        "logical_editor_invocation",
        "writer_invocation_count",
        "editor_invocation_count",
        "editor_invocation_limit",
        "generation_timeout_seconds",
        "writer_stage_elapsed_seconds",
        "pass_elapsed_seconds",
        "writer_response_count",
        "semantic_transition_count",
        "semantic_recovery_exhausted",
        "context_character_count",
        "expected_support_count",
        "exposed_support_count",
        "evidence_record_count",
        "evidence_fact_record_count",
        "quote_allowlist_count",
    }
    result = {
        key: value[key]
        for key in scalar_keys
        if key in value and isinstance(value[key], (int, float))
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
        "rendered_packet_representation": {"full", "compact"},
    }
    from src.ai_providers import get_allowed_ai_models

    for key in ("model", "actual_model"):
        item = value.get(key)
        if isinstance(item, str) and item in get_allowed_ai_models():
            result[key] = item
    for key in ("provider", "actual_provider"):
        item = value.get(key)
        if isinstance(item, str) and item in {
            "openrouter",
            "openai",
            "deepseek",
            "gemini",
            "anthropic",
        }:
            result[key] = item
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
        "parsed_word_count",
        "parsed_section_count",
        "prompt_chars",
        "context_chars",
    ):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            result[key] = item
    for key, allowed_values in safe_choice_values.items():
        item = value.get(key)
        if isinstance(item, str) and item in allowed_values:
            result[key] = item

    for key in (
        "context_hash",
        "prompt_hash",
        "context_sha256",
        "expected_support_ids_sha256",
        "exposed_support_ids_sha256",
        "quote_allowlist_sha256",
    ):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item):
            result[key] = item

    format_findings = value.get("writer_response_format_findings")
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

    operations = _safe_structural_operations(
        value.get("structural_operations"), checkpoints, value.get("editor_pass_outcomes")
    )
    if operations:
        result["structural_operations"] = operations
    for key, is_pass in (("editor_unit_outcomes", False), ("editor_pass_outcomes", True)):
        outcomes = _safe_editor_outcomes(value.get(key), checkpoints, pass_outcomes=is_pass)
        if outcomes:
            result[key] = outcomes
    profile = value.get("length_profile")
    if isinstance(profile, dict):
        result["length_profile"] = {
            key: profile[key]
            for key in (
                "target_min_words",
                "target_max_words",
                "target_min_sections",
                "target_max_sections",
                "hard_min_words",
                "hard_max_words",
                "thematic_line_count",
                "develop_line_count",
                "detail_anchor_count",
            )
            if isinstance(profile.get(key), int)
        }
        if isinstance(profile.get("richness"), str) and profile["richness"] in {
            "thin",
            "standard",
            "rich",
        }:
            result["length_profile"]["richness"] = profile["richness"]

    def compact_attempt(value: Any) -> dict[str, int | float | bool] | None:
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

    for key in ("provider_attempts", "writer_provider_attempts"):
        compact = compact_attempt(value.get(key))
        if compact is not None:
            result[key] = compact

    for key in ("editor_provider_attempts",):
        attempts = value.get(key)
        if isinstance(attempts, list):
            compact_attempts = [
                compact for item in attempts[:2] if (compact := compact_attempt(item)) is not None
            ]
            if compact_attempts:
                result[key] = compact_attempts
    return result


def _safe_quality_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    from src.publication.article_quality import ARTICLE_READER_QUALITY_VERSION
    from src.publication.article_quality_policy import ARTICLE_QUALITY_FINDING_POLICIES

    findings = []
    for item in (
        value.get("findings", []) if isinstance(value.get("findings"), (list, tuple)) else []
    ):
        if not isinstance(item, dict):
            continue
        code, unit = item.get("code"), item.get("unit_id")
        if not isinstance(code, str) or code not in ARTICLE_QUALITY_FINDING_POLICIES:
            continue
        if not isinstance(unit, str) or not re.fullmatch(
            r"(?:TITLE|LEAD|DRAFT|ARTICLE|[HP][0-9]{3,}|story:[0-9]+)", unit
        ):
            continue
        policy = ARTICLE_QUALITY_FINDING_POLICIES[code]
        findings.append(
            {
                "code": code,
                "unit_id": unit,
                "severity": policy.severity,
                "finding_class": policy.finding_class,
                "repair_scope": policy.repair_scope,
                "publication_effect": policy.publication_effect,
            }
        )
    result: dict[str, Any] = {"version": ARTICLE_READER_QUALITY_VERSION, "findings": findings}
    count = value.get("finding_count")
    result["finding_count"] = (
        count
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
        else len(findings)
    )
    if isinstance(value.get("needs_edit"), bool):
        result["needs_edit"] = value["needs_edit"]
    for key, allowed in (
        ("counts_by_code", ARTICLE_QUALITY_FINDING_POLICIES),
        ("counts_by_severity", {"warning", "repair", "blocking"}),
    ):
        counts = value.get(key)
        if isinstance(counts, dict):
            result[key] = {
                code: number
                for code, number in counts.items()
                if isinstance(code, str)
                and code in allowed
                and isinstance(number, int)
                and not isinstance(number, bool)
                and number >= 0
            }
    return result


def _safe_final_metadata(
    value: Any, checkpoints: _MemoryCheckpointObserver | None = None
) -> dict[str, Any]:
    """Keep versioned quality and composition diagnostics, never article/source text."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {
        key: value[key]
        for key in (
            "planned_story_count",
            "ai_covered_story_count",
            "supplemented_story_count",
            "final_covered_story_count",
            "ai_story_coverage",
            "final_story_coverage",
            "evidence_boundary_passed",
            "quality_gate_passed",
            "coverage_only_diagnostic",
            "planner_model_call_count",
            "planner_validation_repair_used",
        )
        if key in value and isinstance(value[key], (int, float, bool, type(None)))
    }
    for key, allowed in {
        "status": {"accepted", "rejected", "failed", "passed", "succeeded"},
        "winning_kind": {"writer", "editor", "event_article_writer"},
        "writer_status": {"passed", "rejected", "failed"},
        "recovery_mode": {"none", "supplement", "full_fallback"},
    }.items():
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            result[key] = item
    result.update(_safe_writer_metadata(value, checkpoints))
    if value.get("stage") in {
        "writer",
        "writer_validation",
        "post_finalization_validation",
        "post_finalization_quality",
        "finalization",
        "finalization_assessment",
        "assessment",
    }:
        result["stage"] = value["stage"]
    validation = _safe_validation_metadata(value.get("factual_validation"))
    if validation:
        result["factual_validation"] = validation
    for key in (
        "reader_quality",
        "quality_before_edit",
        "quality_after_edit",
        "quality_after_finalization",
    ):
        quality = _safe_quality_metadata(value.get(key))
        if quality is not None:
            result[key] = quality

    composition = value.get("composition")
    if isinstance(composition, dict):
        result["composition"] = {
            key: composition[key]
            for key in ("line_count", "group_count", "bundle_count")
            if isinstance(composition.get(key), int)
            and not isinstance(composition[key], bool)
            and composition[key] >= 0
        }

    writer = _safe_writer_metadata(value.get("writer_attempt"), checkpoints)
    if writer:
        result["writer_attempt"] = writer

    coverage = value.get("coverage")
    if isinstance(coverage, dict):
        result["coverage"] = {
            key: coverage[key]
            for key in (
                "planned_story_count",
                "covered_story_count",
                "develop_story_coverage",
                "weave_story_coverage",
                "brief_story_coverage",
                "planned_detail_support_count",
                "covered_detail_support_count",
                "detail_support_coverage",
            )
            if isinstance(coverage.get(key), (int, float))
        }
    return result


async def build_article_preview_from_run(
    run_id: int,
    *,
    config: Config,
    expected_edition_slug: str | None = None,
) -> ArticleRunPreviewOutcome:
    """Replay sealed inputs and return an in-memory accepted/rejected/failed result."""
    checkpoints = _MemoryCheckpointObserver()
    attempts = _MemoryGenerationAttemptObserver(checkpoints)
    run_details: dict[str, object] = {"run_id": run_id}
    generator: ArticleGenerator | None = None

    try:
        runtime = get_runtime()
        repo = PublicationRepository()
        adapter = EventEditorialAdapter(uow=runtime.uow, repo=repo)

        async with runtime.uow.transaction() as conn:
            run = await repo.get_run_by_id(conn, run_id)
            if run is None:
                raise ValueError(f"publication run {run_id} not found")
            if run.publication_type not in ARTICLE_PUBLICATION_TYPES:
                raise ValueError(
                    f"publication run {run_id} has unsupported article preview type "
                    f"{run.publication_type!r}"
                )
            run_details.update(
                {
                    "publication_type": run.publication_type,
                    "snapshot_at": run.snapshot_at.isoformat(),
                }
            )

            edition_cursor = await conn.execute(
                "SELECT slug, timezone FROM editions WHERE id = %s",
                (run.edition_id,),
            )
            edition_row = await edition_cursor.fetchone()
            if edition_row is None or not edition_row[0]:
                raise ValueError(f"publication run {run_id} has no edition")
            edition_slug = str(edition_row[0]).strip()
            run_details["edition_slug"] = edition_slug
            if expected_edition_slug is not None and expected_edition_slug != edition_slug:
                raise ValueError(
                    f"requested edition {expected_edition_slug!r} conflicts with frozen run "
                    f"edition {edition_slug!r}"
                )
            timezone_name = str(edition_row[1]).strip() if edition_row[1] else ""
            if not timezone_name:
                raise ValueError(f"publication run {run_id} edition is missing timezone")
            try:
                timezone_name = normalize_timezone_name(timezone_name)
                get_timezone(timezone_name)
            except ValueError as exc:
                raise ValueError(
                    f"publication run {run_id} edition has invalid timezone {timezone_name!r}"
                ) from exc
            run_details["edition_timezone"] = timezone_name

            inputs = await repo.load_sealed_inputs(conn, run_id)
            if not inputs:
                raise ValueError(f"publication run {run_id} has no sealed inputs")
            frozen = await adapter.adapt_inputs_on(
                conn,
                run_id,
                inputs=inputs,
                include_anchor_publications=False,
            )

        article_context = getattr(frozen.analysis, "article_context", None)
        if article_context is not None and article_context.edition_timezone != timezone_name:
            # Guard the adapter's legacy UTC default: replay must use the stored
            # edition zone, and missing timezone data must never pass silently.
            frozen = replace(
                frozen,
                analysis=replace(
                    frozen.analysis,
                    article_context=replace(article_context, edition_timezone=timezone_name),
                ),
            )

        identity_payload = {
            "run": run_details,
            "sealed_inputs": inputs,
            "article_context": getattr(frozen.analysis, "article_context", None),
        }
        source_identity = hashlib.sha256(
            json.dumps(
                _assessment_input_value(identity_payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        checkpoints.source_identity = source_identity

        # Frozen replay is a dry-run: isolate config from the caller and disable
        # production prompt/draft artifact writes on this generator instance.
        preview_config = deepcopy(config)
        preview_config.settings.article.save_debug_artifacts = False
        generator = ArticleGenerator(config=preview_config, logger=logger)
        checkpoint_observer: ArticleCheckpointObserver = checkpoints
        await generator.generate_from_frozen_input(
            frozen,
            attempt_observer=attempts,
            checkpoint_observer=checkpoint_observer,
            source_identity=source_identity,
            assessment_input_observer=checkpoints.bind_assessment_inputs,
        )

        captured = checkpoints.final_checkpoint()
        if captured.assessment is None or not captured.assessment.publishable:
            raise ArticleFinalizationInvariantError(
                "Accepted preview lacks a publishable assessment"
            )
        candidate = _candidate_from_checkpoint(run_details, captured)
        return ArticleRunPreviewOutcome(
            status="accepted",
            candidate=candidate,
            diagnostics=_preview_diagnostics(
                run_details,
                attempts,
                checkpoints,
                generator=generator,
                status="accepted",
            ),
        )
    except ArticlePublicationRejected as exc:
        try:
            captured = checkpoints.final_checkpoint()
        except ArticleFinalizationInvariantError as invariant_error:
            return ArticleRunPreviewOutcome(
                status="failed",
                candidate=_candidate_from_checkpoint(
                    run_details, checkpoints.checkpoints[-1] if checkpoints.checkpoints else None
                ),
                diagnostics=_preview_diagnostics(
                    run_details,
                    attempts,
                    checkpoints,
                    generator=generator,
                    status="failed",
                    error=invariant_error,
                ),
                error=exc,
            )
        return ArticleRunPreviewOutcome(
            status="rejected",
            candidate=_candidate_from_checkpoint(run_details, captured),
            diagnostics=_preview_diagnostics(
                run_details,
                attempts,
                checkpoints,
                generator=generator,
                status="rejected",
                error=exc,
            ),
            error=exc,
        )
    except Exception as exc:
        return ArticleRunPreviewOutcome(
            status="failed",
            candidate=_candidate_from_checkpoint(
                run_details,
                checkpoints.checkpoints[-1] if checkpoints.checkpoints else None,
            ),
            diagnostics=_preview_diagnostics(
                run_details,
                attempts,
                checkpoints,
                generator=generator,
                status="failed",
                error=exc,
            ),
            error=exc,
        )


def _candidate_from_checkpoint(
    run_details: dict[str, object],
    checkpoint: _CapturedCheckpoint | None,
) -> ArticlePreviewCandidate | None:
    if checkpoint is None:
        return None
    draft = checkpoint.draft
    return ArticlePreviewCandidate(
        run_id=_preview_run_id(run_details),
        publication_type=(
            str(run_details["publication_type"]) if "publication_type" in run_details else None
        ),
        edition_slug=(str(run_details["edition_slug"]) if "edition_slug" in run_details else None),
        snapshot_at=_snapshot_from_run_details(run_details),
        title=draft.title,
        lead=draft.lead,
        body=draft.render_markdown(preserve_text=True),
        checkpoint_stage=checkpoint.stage,
        draft=draft,
        assessment=checkpoint.assessment,
    )


def _snapshot_from_run_details(run_details: dict[str, object]) -> dt.datetime | None:
    snapshot_at = run_details.get("snapshot_at")
    if not isinstance(snapshot_at, str):
        return None
    try:
        return dt.datetime.fromisoformat(snapshot_at)
    except ValueError:
        return None


def _preview_run_id(run_details: dict[str, object]) -> int:
    run_id = run_details.get("run_id")
    if isinstance(run_id, int) and not isinstance(run_id, bool):
        return run_id
    raise ValueError("preview outcome is missing its run ID")


def _assessment_metadata(assessment: ArticleAssessmentCheckpoint) -> dict[str, object]:
    validation = assessment.validation
    result: dict[str, object] = {
        "publishable": assessment.publishable,
        "draft_fingerprint": _draft_hash(assessment.draft),
        "input_fingerprint": assessment.input_fingerprint,
        "source_identity": assessment.source_identity,
        "validation": _safe_validation_metadata(
            {
                "is_valid": validation.is_valid,
                "issue_count": len(validation.issues),
                "issues": [
                    {
                        "code": issue.code,
                        "unit_id": issue.unit_id,
                        "severity": issue.severity,
                        "blocking": issue.blocking,
                    }
                    for issue in validation.issues
                ],
            }
        ),
    }
    quality = _safe_quality_metadata(assessment.quality.to_metadata())
    if quality is not None:
        result["reader_quality"] = quality
    if assessment.coverage is not None:
        coverage = assessment.coverage
        result["coverage"] = {
            "planned_story_count": coverage.planned_story_count,
            "covered_story_count": coverage.covered_story_count,
            "story_coverage": coverage.story_coverage,
            "develop_story_coverage": coverage.develop_story_coverage,
            "weave_story_coverage": coverage.weave_story_coverage,
            "brief_story_coverage": coverage.brief_story_coverage,
            "planned_detail_support_count": coverage.planned_detail_support_count,
            "covered_detail_support_count": coverage.covered_detail_support_count,
            "detail_support_coverage": coverage.detail_support_coverage,
        }
    return result


def _preview_diagnostics(
    run_details: dict[str, object],
    attempts: _MemoryGenerationAttemptObserver,
    checkpoints: _MemoryCheckpointObserver,
    *,
    status: ArticlePreviewStatus,
    generator: ArticleGenerator | None = None,
    error: Exception | None = None,
) -> dict[str, object]:
    diagnostics: dict[str, object] = {
        "schema_version": "article-run-preview-v3",
        "status": status,
        **run_details,
        "generation": {
            "attempts": attempts.to_metadata(),
            "checkpoints": checkpoints.diagnostics(),
            "assessment_pair_mismatch_count": checkpoints.assessment_pair_mismatch_count,
            "source_identity": checkpoints.source_identity,
            "latest_candidate_assessed": bool(
                checkpoints.checkpoints and checkpoints.checkpoints[-1].assessment is not None
            ),
            "last_assessed_checkpoint": (
                {
                    "stage": checkpoints.last_assessed.stage,
                    "draft_fingerprint": _draft_hash(checkpoints.last_assessed.draft),
                }
                if checkpoints.last_assessed is not None
                else None
            ),
        },
    }
    if generator is not None:
        provider_attempts = _compact_generation_provider_attempts(
            getattr(generator, "last_generation_provider_attempts", None)
        )
        if provider_attempts is not None:
            diagnostics["provider_attempts"] = provider_attempts
    if error is not None:
        failure: dict[str, object] = {"exception_type": type(error).__name__}
        from src.publication.article_writer_context import ArticleWriterContextBudgetError

        if isinstance(error, ArticleWriterContextBudgetError):
            failure.update(error.to_metadata())
        if isinstance(error, ArticlePublicationRejected):
            if error.reason in {
                "writer_failed",
                "validation_failed",
                "quality_failed",
                "factually_invalid",
                "reader_quality_rejected",
                "article_validation_failed",
                "article_reader_quality_failed",
                "no_substantive_material",
                "unusable_writer_response",
            }:
                failure["reason"] = error.reason
            if error.error_kind in {
                "article_publication_rejected",
                "article_generation_failed",
                "evidence_boundary_failure",
                "reader_quality_failure",
                "writer_failure",
                "article_writer_rejected",
                "article_validation_rejected",
                "article_quality_rejected",
            }:
                failure["error_kind"] = error.error_kind
            safe_metadata = _safe_final_metadata(error.metadata, checkpoints)
            if safe_metadata:
                failure["rejection_metadata"] = safe_metadata
        diagnostics["failure"] = failure
    return diagnostics


def _compact_generation_provider_attempts(value: Any) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None

    def compact(item: Any) -> dict[str, int | float | bool] | None:
        if not isinstance(item, dict):
            return None
        return {
            key: item[key]
            for key in (
                "slot_attempt_count",
                "transport_attempt_count",
                "complete",
                "elapsed_seconds",
            )
            if key in item and isinstance(item[key], (int, float, bool))
        }

    result: dict[str, object] = {}
    if "writer" in value:
        result["writer"] = compact(value.get("writer"))
    editor_attempts = value.get("editor")
    if isinstance(editor_attempts, list):
        result["editor"] = [
            compact_attempt
            for item in editor_attempts[:2]
            if (compact_attempt := compact(item)) is not None
        ]
    return result


__all__ = [
    "ArticlePreviewCandidate",
    "ArticlePreviewStatus",
    "ArticleRunPreviewOutcome",
    "build_article_preview_from_run",
]
