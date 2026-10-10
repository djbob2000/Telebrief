"""Targeted editorial copy-editor and fact-checker for structured article drafts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from time import perf_counter
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, cast

from src.ai_providers import AIProvider, capture_provider_attempts
from src.publication.article_context import ArticleEditorialContext, _support_framing
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_finalization import (
    ArticleAssessmentCheckpoint,
    ArticleAssessmentInputObserver,
    ArticleCheckpointObserver,
    article_assessment_input_fingerprint,
    assess_article_draft,
)
from src.publication.article_material import (
    ArticleMaterialProjection,
    materialize_article_validation_context,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
    _normalize_homoglyphs,
    _split_sentences_safe,
    _strip_internal_handles,
)
from src.publication.article_quality import (
    ArticleReaderQualityFinding,
    ArticleReaderQualityReport,
    _citable_support_ids,
    _direct_speech_spans,
    _projected_support_themes,
)
from src.publication.article_quality_policy import (
    ArticleQualityPolicyError,
    article_quality_policy,
)
from src.publication.article_validator import ArticleValidationResult
from src.publication.article_writer_context import sanitize_writer_source_text

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_MAX_HEADING_SECTION_PARAGRAPH_CHARS = 1_500
_MAX_HEADING_SECTION_CONTEXT_CHARS = 18_000
_MAX_EDITOR_SUPPORTS = 64
_MAX_EDITOR_SUPPORT_CONTEXT_CHARS = 32_000
_MAX_EDITOR_SUPPORT_PACKET_CHARS = 4_000
_MAX_TITLE_LEAD_REPAIR_SUPPORTS = 6
ARTICLE_EDITOR_UNIT_STATUSES = frozenset(
    {
        "applied",
        "no_change",
        "not_returned",
        "rejected",
        "rolled_back",
        "deferred_budget",
        "missing_required_support",
    }
)
ARTICLE_EDITOR_OUTCOME_REASONS = frozenset(
    {
        "accepted_change",
        "text_unchanged",
        "patch_not_returned",
        "patch_rejected",
        "required_support_missing",
        "support_budget_exceeded",
        "support_packet_exceeded",
        "prompt_budget_exceeded",
        "new_blocker_quarantined",
        "assessment_error_atomic_rollback",
        "editor_pass_error_atomic_rollback",
        "structural_batch_rollback",
        "no_measurable_progress",
        "deadline_exhausted",
    }
)
ARTICLE_EDITOR_PASS_REASONS = frozenset(
    {
        "completed",
        "unparseable_response",
        "invalid_response_shape",
        "unknown_unit_ids",
        "unlocalizable_finding",
        "no_eligible_units",
        "no_measurable_progress",
        "deadline_exhausted",
        "deferred_units_remain",
        "all_targets_resolved",
        "assessment_error_atomic_rollback",
        "editor_pass_error_atomic_rollback",
    }
)
ARTICLE_STRUCTURAL_OUTCOME_REASONS = frozenset(
    {
        "no_authorized_structural_sources",
        "required_support_budget_exceeded",
        "required_support_unavailable",
        "required_support_packet_exceeded",
        "complete_article_context_budget_exceeded",
        "invalid_operation_payload",
        "stale_base_fingerprint",
        "base_topology_changed",
        "duplicate_operation_id",
        "conflicting_source_operations",
        "unauthorized_or_unknown_source",
        "conflicting_text_and_structural_edits",
        "invalid_move",
        "missing_recomposed_paragraphs",
        "invalid_new_section",
        "new_section_not_authorized",
        "new_section_theme_has_existing_destination",
        "unauthorized_or_unknown_destination",
        "conflicting_heading_and_structural_edits",
        "invalid_insertion_anchor",
        "conflicting_or_unresolved_insertion_anchor",
        "direct_quote_words_changed",
        "replacement_not_grounded_in_source_supports",
        "new_heading_not_grounded_in_source_supports",
        "paragraph_identity_integrity_failed",
        "new_blocker_atomic_rollback",
        "assessment_error_atomic_rollback",
        "awaiting_assessment",
        "candidate_assessed",
        "structure_unchanged",
        "editor_pass_error_atomic_rollback",
        "empty_section_heading_fact_requires_preservation",
    }
)

_STRUCTURAL_FINDING_CODES = frozenset(
    {"THEME_MISMATCHED_SECTION", "UNCLASSIFIED_STORY_IN_CONNECTIVITY_SECTION"}
)


@dataclass(frozen=True)
class ArticleStructuralOperation:
    """An explicit operation; claims and provenance are always assigned locally."""

    operation_id: str
    kind: Literal["move", "recompose", "create_section"]
    source_unit_ids: tuple[str, ...]
    destination_section_id: str | None
    destination_before_unit_id: str | None
    heading: str | None
    paragraph_texts: tuple[str, ...]


@dataclass(frozen=True)
class _ArticlePassRegistry:
    """Immutable positional identities and permissions for one exact editor base."""

    base_fingerprint: str
    sections: Mapping[str, ArticleSection]
    paragraphs: Mapping[str, ArticleParagraph]
    paragraph_sections: Mapping[str, str]
    source_support_ids: Mapping[str, tuple[str, ...]]
    destinations: Mapping[str, frozenset[str]]
    new_section_sources: frozenset[str]


def _draft_fingerprint(draft: StructuredArticleDraft) -> str:
    return hashlib.sha256(
        json.dumps(asdict(draft), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _paragraph_support_ids(paragraph: ArticleParagraph) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *paragraph.cited_support_ids,
                *(sid for c in paragraph.claims for sid in c.cited_support_ids),
            )
        )
    )


_NAME_QUOTE_WORD_RE = re.compile(r"^(?:[A-ZА-ЯЁЇІЄҐ][\w'’-]*|\d[\w-]*)$")


def _is_inflected_existing_name(quote: str, original: str) -> bool:
    """Return whether a quoted span is a short proper name already in the unit.

    Names of shops, organisations and places in typographic quotes are not
    direct speech; the editor may decline them. Speech fragments (lowercase
    words, punctuation, more than three words) never qualify.
    """
    from src.publication.article_claims import _stem

    words = quote.split()
    if not 1 <= len(words) <= 3 or not all(_NAME_QUOTE_WORD_RE.match(word) for word in words):
        return False
    original_stems = {_stem(token.casefold()) for token in re.findall(r"\w+", original)}
    return all(_stem(token.casefold()) in original_stems for token in re.findall(r"\w+", quote))


def _preserves_existing_direct_quotes(original: str, replacement: str) -> bool:
    """Allow removing speech; preserve its exact words and permit typographic names.

    A new quote may wrap text that already appeared unquoted in the original
    unit (for example, the provider-name typography repair). A shortened or
    altered existing quote is rejected unless that exact wording independently
    appeared outside any original quote.
    """
    original_spans = _direct_speech_spans(original)
    original_quotes = Counter(span.content for span in original_spans)
    replacement_quotes = Counter(span.content for span in _direct_speech_spans(replacement))
    extra_quotes = replacement_quotes - original_quotes
    for quote in extra_quotes:
        if _is_inflected_existing_name(quote, original):
            # «Амстор» → «Амстора»: name typography, not altered direct speech.
            continue
        quote_spans = tuple(match.span() for match in re.finditer(re.escape(quote), original))
        if not any(
            not any(
                old_quote.full_span[0] <= start and end <= old_quote.full_span[1]
                for old_quote in original_spans
            )
            for start, end in quote_spans
        ):
            return False
    return True


def _reground_support_ids(
    text: str,
    context: ArticleEditorialContext,
    allowed_support_ids: tuple[str, ...] | list[str] | None = None,
    *,
    minimum_shared_stems: int = 2,
) -> tuple[str, ...]:
    """Return only support packets with a concrete lexical anchor in ``text``.

    Editor patches replace reader-facing prose, so citations from the old
    paragraph cannot be carried forward.  This deliberately has no
    best-match fallback: an unmatched patch must remain unsupported and be
    rejected by the normal fail-closed validator.
    """
    from src.publication.article_claims import _stem
    from src.publication.article_semantic_support import _EDITORIAL_GLUE, _STOPWORDS

    token_re = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)

    def distinctive_stems(value: str) -> set[str]:
        stems: set[str] = set()
        for token in token_re.findall(value or ""):
            normalized = token.casefold().replace("ё", "е")
            if len(normalized) < 3 or normalized in _STOPWORDS:
                continue
            stem = _stem(normalized)
            if stem in _EDITORIAL_GLUE or normalized in _EDITORIAL_GLUE:
                continue
            stems.add(stem)
        return stems

    text_stems = distinctive_stems(text)
    text_numbers = set(re.findall(r"\b\d+\b", text))
    matched: list[str] = []
    allowed = set(allowed_support_ids) if allowed_support_ids is not None else None
    for support in context.supports:
        if allowed is not None and support.support_id not in allowed:
            continue
        if support.publication_use != "PUBLISH":
            continue
        support_text = f"{support.text} {support.source_text}"
        shared_stems = text_stems & distinctive_stems(support_text)
        support_numbers = set(re.findall(r"\b\d+\b", support_text))
        if (
            len(shared_stems) >= minimum_shared_stems
            or (shared_stems and text_numbers & support_numbers)
            or len(text_numbers & support_numbers) >= 2
        ):
            matched.append(support.support_id)
    return tuple(dict.fromkeys(matched))


class ArticleEditor:
    """Targeted fact-checking editor that fixes isolated validation issues without full draft rewrite."""

    def __init__(
        self,
        provider: AIProvider,
        model: str,
        *,
        temperature: float = 0.2,
        max_output_tokens: int = 32768,
        reasoning_effort: str | None = "none",
    ) -> None:
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = min(max_output_tokens, 32768)
        self.reasoning_effort = reasoning_effort
        self.last_attempt_count = 0
        self.last_provider_attempts: list[dict[str, Any]] = []
        self.last_assessment: ArticleAssessmentCheckpoint | None = None
        self.last_patched_unit_ids: tuple[str, ...] = ()
        self.last_quality_report = ArticleReaderQualityReport()
        self.last_structural_operations: list[dict[str, Any]] = []
        self.last_unit_outcomes: list[dict[str, Any]] = []
        self.last_pass_outcomes: list[dict[str, Any]] = []

    async def edit_draft(
        self,
        draft: StructuredArticleDraft,
        validation_result: ArticleValidationResult,
        context: ArticleEditorialContext,
        *,
        config: Any | None = None,
        length_profile: Any | None = None,
        attempt_observer: Any | None = None,
        save_debug_artifact: Callable[[str, Any], None] | None = None,
        debug_artifact_prefix: str = "article_editor",
        max_attempts: int = 2,
        quality_report: ArticleReaderQualityReport | None = None,
        coverage_plan: ArticleCoveragePlan | None = None,
        material_projection: ArticleMaterialProjection | None = None,
        place_resolver: Any | None = None,
        assessment: ArticleAssessmentCheckpoint | None = None,
        checkpoint_observer: ArticleCheckpointObserver | None = None,
        source_identity: str | None = None,
        assessment_input_observer: ArticleAssessmentInputObserver | None = None,
    ) -> tuple[StructuredArticleDraft, ArticleValidationResult]:
        """Apply targeted editorial corrections to units with blocking validation issues."""
        current_draft = draft
        current_val = validation_result
        current_quality = quality_report or ArticleReaderQualityReport()
        self.last_quality_report = current_quality
        patched_unit_ids: list[str] = []
        self.last_provider_attempts = []
        self.last_attempt_count = 0
        self.last_patched_unit_ids = ()
        self.last_structural_operations = []
        self.last_unit_outcomes = []
        self.last_pass_outcomes = []
        no_op_patch_signatures: dict[str, set[str]] = {}
        attempted_target_signatures: dict[tuple[Any, ...], int] = {}
        previous_attempt_feedback: dict[tuple[Any, ...], dict[str, Any]] = {}
        validation_context = (
            materialize_article_validation_context(context, material_projection)
            if material_projection is not None
            else context
        )

        max_attempts = min(max_attempts, 2)
        input_fingerprint = await asyncio.to_thread(
            article_assessment_input_fingerprint,
            context,
            coverage_plan,
            config,
            length_profile,
            material_projection,
            place_resolver,
            source_identity=source_identity,
        )
        input_observer_notified = False

        def observe_assessment_inputs(fingerprint: str) -> None:
            nonlocal input_observer_notified
            if assessment_input_observer is not None:
                assessment_input_observer(fingerprint)
                input_observer_notified = True

        known_assessments: list[ArticleAssessmentCheckpoint] = []

        def matching_assessment(
            candidate: StructuredArticleDraft,
        ) -> ArticleAssessmentCheckpoint | None:
            return next(
                (
                    checkpoint
                    for checkpoint in reversed(known_assessments)
                    if checkpoint.matches(
                        candidate,
                        input_fingerprint,
                        source_identity=source_identity,
                    )
                ),
                None,
            )

        async def assess_candidate(
            candidate: StructuredArticleDraft,
        ) -> ArticleAssessmentCheckpoint:
            matched = matching_assessment(candidate)
            if matched is not None:
                if not input_observer_notified:
                    observe_assessment_inputs(input_fingerprint)
                self.last_assessment = matched
                return matched
            checkpoint = await assess_article_draft(
                candidate,
                context,
                coverage_plan=coverage_plan,
                editorial_config=config,
                length_profile=length_profile,
                material_projection=material_projection,
                place_resolver=place_resolver,
                source_identity=source_identity,
                input_observer=observe_assessment_inputs,
            )
            if not checkpoint.matches(
                candidate, input_fingerprint, source_identity=source_identity
            ):
                raise RuntimeError("assessment API returned a checkpoint for different inputs")
            known_assessments.append(checkpoint)
            self.last_assessment = checkpoint
            return checkpoint

        if assessment is not None and assessment.matches(
            draft, input_fingerprint, source_identity=source_identity
        ):
            known_assessments.append(assessment)
        initial_checkpoint = matching_assessment(draft)
        if initial_checkpoint is None and max_attempts > 0:
            initial_checkpoint = await assess_candidate(draft)
        if initial_checkpoint is not None:
            current_val = initial_checkpoint.validation
            current_quality = initial_checkpoint.quality
            self.last_assessment = initial_checkpoint
        else:
            self.last_assessment = None
        self.last_quality_report = current_quality

        def blocking_keys(
            validation: ArticleValidationResult, quality: ArticleReaderQualityReport
        ) -> dict[tuple[Any, ...], str]:
            return {
                **{
                    (
                        "evidence",
                        issue.code,
                        issue.unit_id,
                        tuple(sorted(issue.support_ids)),
                        issue.claim_text,
                        repr(issue.unsupported_claims),
                    ): issue.unit_id
                    for issue in validation.issues
                    if issue.blocking
                },
                **{
                    (
                        "quality",
                        finding.code,
                        finding.unit_id,
                        tuple(sorted(finding.support_ids)),
                        finding.message,
                    ): finding.unit_id
                    for finding in quality.blocking_findings
                },
            }

        def actionable_keys(
            validation: ArticleValidationResult, quality: ArticleReaderQualityReport
        ) -> set[tuple[Any, ...]]:
            return {
                (issue.code, issue.unit_id, tuple(sorted(issue.support_ids)))
                for issue in validation.issues
                if issue.blocking
            } | {
                (finding.code, localized.unit_id, tuple(sorted(localized.support_ids)))
                for finding in quality.repair_findings
                for localized in self._localize_article_quality_finding(current_draft, finding)
            }

        def target_signature(unit: Mapping[str, Any]) -> tuple[Any, ...]:
            return (
                unit.get("unit_type", ""),
                tuple(sorted({getattr(issue, "code", "") for issue in unit.get("issues", ())})),
                tuple(
                    sorted(
                        {
                            sid
                            for issue in unit.get("issues", ())
                            for sid in getattr(issue, "support_ids", ())
                        }
                    )
                ),
            )

        def safe_unit_outcome(
            *,
            pass_index: int,
            unit_id: str,
            status: str,
            reason: str,
            issues: list[Any],
            required_support_count: int,
            shown_support_count: int,
            base_fingerprint: str,
            result_fingerprint: str,
        ) -> dict[str, Any]:
            if status not in ARTICLE_EDITOR_UNIT_STATUSES:
                status = "rejected"
            if reason not in ARTICLE_EDITOR_OUTCOME_REASONS:
                reason = "patch_rejected"
            return {
                "pass_index": pass_index,
                "unit_id": unit_id,
                "status": status,
                "reason": reason,
                "issue_codes": sorted({getattr(issue, "code", "") for issue in issues}),
                "required_support_count": required_support_count,
                "shown_support_count": shown_support_count,
                "base_fingerprint": base_fingerprint,
                "result_fingerprint": result_fingerprint,
            }

        def append_pass_outcome(
            *,
            pass_index: int,
            reason: str,
            requested: int,
            applied: int,
            deferred: int,
            unresolved: int,
            elapsed: float,
            base_fingerprint: str,
            unknown_unit_id_count: int = 0,
        ) -> None:
            if reason not in ARTICLE_EDITOR_PASS_REASONS:
                reason = "completed"
            self.last_pass_outcomes.append(
                {
                    "pass_index": pass_index,
                    "outcome": reason,
                    "reason": reason,
                    "requested_unit_count": requested,
                    "applied_unit_count": applied,
                    "deferred_unit_count": deferred,
                    "unresolved_target_count": unresolved,
                    "elapsed_seconds": round(elapsed, 3),
                    "base_fingerprint": base_fingerprint,
                    "unknown_unit_id_count": unknown_unit_id_count,
                }
            )

        for attempt in range(1, max_attempts + 1):
            attempt_started = perf_counter()
            base_fingerprint = _draft_fingerprint(current_draft)
            if checkpoint_observer is not None:
                checkpoint_observer("editor_base", current_draft, self.last_assessment)
            all_blocking_issues = [iss for iss in current_val.issues if iss.blocking]
            blocking_issues = [
                iss for iss in all_blocking_issues if iss.unit_id not in ("DRAFT", "ARTICLE", "")
            ]
            unlocalizable_count = sum(
                issue.unit_id in ("DRAFT", "ARTICLE", "") for issue in all_blocking_issues
            )
            all_quality_issues = list(current_quality.repair_findings)
            quality_issues = [
                localized
                for finding in all_quality_issues
                for localized in self._localize_article_quality_finding(current_draft, finding)
            ]
            unlocalizable_count += sum(
                1
                for finding in all_quality_issues
                if not self._localize_article_quality_finding(current_draft, finding)
            )
            if not blocking_issues and not quality_issues:
                reason = "unlocalizable_finding" if unlocalizable_count else "all_targets_resolved"
                append_pass_outcome(
                    pass_index=attempt,
                    reason=reason,
                    requested=0,
                    applied=0,
                    deferred=0,
                    unresolved=unlocalizable_count,
                    elapsed=perf_counter() - attempt_started,
                    base_fingerprint=base_fingerprint,
                )
                break

            # Group blocking issues by unit_id
            issues_by_unit: dict[str, list[Any]] = {}
            for iss in blocking_issues:
                issues_by_unit.setdefault(iss.unit_id, []).append(iss)
            for finding in quality_issues:
                if finding.unit_id not in ("DRAFT", ""):
                    issues_by_unit.setdefault(finding.unit_id, []).append(finding)

            logger.info(
                "ArticleEditor pass %d/%d targeting %d problematic unit(s): %s",
                attempt,
                max_attempts,
                len(issues_by_unit),
                list(issues_by_unit.keys()),
            )

            all_unit_data = self._build_unit_contexts(
                current_draft,
                issues_by_unit,
                validation_context,
                material_projection=material_projection,
            )
            registered_unit_ids = {unit["unit_id"] for unit in all_unit_data}
            unlocalizable_count += sum(
                unit_id not in registered_unit_ids for unit_id in issues_by_unit
            )
            eligible_units, omitted_units = self._bound_prompt_supports(all_unit_data)
            for omitted in omitted_units:
                self.last_unit_outcomes.append(
                    safe_unit_outcome(
                        pass_index=attempt,
                        unit_id=omitted["unit_id"],
                        status=omitted["selection_status"],
                        reason=omitted["selection_reason"],
                        issues=omitted["issues"],
                        required_support_count=omitted["required_support_count"],
                        shown_support_count=omitted["shown_support_count"],
                        base_fingerprint=base_fingerprint,
                        result_fingerprint=base_fingerprint,
                    )
                )

            def second_pass_attempt_rank(unit: Mapping[str, Any]) -> int:
                prior = attempted_target_signatures.get(target_signature(unit))
                return 0 if prior is None else prior

            ordered_units = sorted(
                enumerate(eligible_units),
                key=lambda pair: (
                    self._repair_priority(pair[1]),
                    second_pass_attempt_rank(pair[1]) if attempt > 1 else 0,
                    pair[0],
                ),
            )
            from src.publication.article_writer_context import ARTICLE_WRITER_CONTEXT_MAX_CHARS

            system_prompt = self._build_system_prompt()
            prompt_data: list[dict[str, Any]] = []
            prompt_deferred_units: list[dict[str, Any]] = []
            for _, unit in ordered_units:
                signature = target_signature(unit)
                feedback = (
                    dict(previous_attempt_feedback[signature])
                    if signature in previous_attempt_feedback
                    else None
                )
                candidate_units = [*prompt_data, {**unit, "prior_feedback": feedback}]
                feedback_by_unit = {
                    selected["unit_id"]: selected["prior_feedback"]
                    for selected in candidate_units
                    if selected.get("prior_feedback") is not None
                }
                if attempt > 1:
                    feedback_by_unit["_pass_summary"] = {
                        "previously_considered_units": len(attempted_target_signatures),
                        "previously_deferred_units": sum(
                            1
                            for item in self.last_unit_outcomes
                            if item["pass_index"] == attempt - 1
                            and item["status"] == "deferred_budget"
                        ),
                        "previously_rejected_units": sum(
                            1
                            for item in self.last_unit_outcomes
                            if item["pass_index"] == attempt - 1 and item["status"] == "rejected"
                        ),
                    }
                candidate_prompt = self._build_user_prompt(
                    candidate_units,
                    attempt=attempt,
                    max_passes=max_attempts,
                    previous_attempt_feedback=feedback_by_unit,
                )
                if (
                    len(system_prompt) + len(candidate_prompt) + 4 * self.max_output_tokens
                    <= ARTICLE_WRITER_CONTEXT_MAX_CHARS
                ):
                    prompt_data.append({**unit, "prior_feedback": feedback})
                else:
                    prompt_deferred_units.append(unit)
                    self.last_unit_outcomes.append(
                        safe_unit_outcome(
                            pass_index=attempt,
                            unit_id=unit["unit_id"],
                            status="deferred_budget",
                            reason="prompt_budget_exceeded",
                            issues=unit["issues"],
                            required_support_count=unit["required_support_count"],
                            shown_support_count=0,
                            base_fingerprint=base_fingerprint,
                            result_fingerprint=base_fingerprint,
                        )
                    )

            prompt_feedback = {
                unit["unit_id"]: unit["prior_feedback"]
                for unit in prompt_data
                if unit.get("prior_feedback") is not None
            }
            if attempt > 1:
                prompt_feedback["_pass_summary"] = {
                    "previously_considered_units": len(attempted_target_signatures),
                    "previously_deferred_units": sum(
                        1
                        for item in self.last_unit_outcomes
                        if item["pass_index"] == attempt - 1 and item["status"] == "deferred_budget"
                    ),
                    "previously_rejected_units": sum(
                        1
                        for item in self.last_unit_outcomes
                        if item["pass_index"] == attempt - 1 and item["status"] == "rejected"
                    ),
                }

            deferred_count = len(omitted_units) + len(prompt_deferred_units)
            if not prompt_data:
                reason = (
                    "unlocalizable_finding"
                    if unlocalizable_count and not all_unit_data
                    else "no_eligible_units"
                )
                append_pass_outcome(
                    pass_index=attempt,
                    reason=reason,
                    requested=0,
                    applied=0,
                    deferred=deferred_count,
                    unresolved=len(issues_by_unit) + unlocalizable_count,
                    elapsed=perf_counter() - attempt_started,
                    base_fingerprint=base_fingerprint,
                )
                logger.warning("ArticleEditor has no complete eligible unit context; stopping")
                if attempt < max_attempts and deferred_count:
                    continue
                break

            registry = self._build_pass_registry(
                current_draft,
                current_quality,
                validation_context,
                material_projection,
            )
            user_prompt = self._build_user_prompt(
                prompt_data,
                attempt=attempt,
                max_passes=max_attempts,
                previous_attempt_feedback=prompt_feedback,
            )
            structural_context, structural_context_reason = self._structural_prompt_context(
                current_draft, registry, validation_context, user_prompt, system_prompt
            )
            if structural_context is not None:
                user_prompt += structural_context
            elif registry.source_support_ids:
                self.last_structural_operations.append(
                    {
                        "attempt": attempt,
                        "status": "skipped",
                        "reason": structural_context_reason,
                        "source_unit_ids": sorted(registry.source_support_ids),
                    }
                )

            obs_att_id = 0
            if attempt_observer is not None:
                obs_att_id = await attempt_observer.attempt_started(
                    "repair",
                    provider=self.provider.__class__.__name__,
                    model=self.model,
                    metadata={
                        "strategy": "article_editor",
                        "attempt": attempt,
                        "structural_operations": list(self.last_structural_operations),
                        "units": list(issues_by_unit.keys()),
                        "violations": [
                            f"{getattr(iss, 'code', 'QUALITY')}:{getattr(iss, 'unit_id', '')}"
                            for iss in (*blocking_issues, *quality_issues)
                        ],
                    },
                )

            previous_draft = current_draft
            previous_actionable = actionable_keys(current_val, current_quality)
            previous_val = current_val
            previous_quality = current_quality
            previous_assessment = self.last_assessment
            previous_patched_unit_ids = list(patched_unit_ids)
            rollback_draft, rollback_val, rollback_quality = (
                previous_draft,
                previous_val,
                previous_quality,
            )
            rollback_assessment = previous_assessment
            rollback_patched_unit_ids = list(previous_patched_unit_ids)
            response: str | None = None
            unit_outcomes: dict[str, dict[str, str]] = {}
            requested_units = {unit["unit_id"] for unit in prompt_data}
            prompt_units_by_id = {unit["unit_id"]: unit for unit in prompt_data}
            _unknown_unit_count = 0
            try:
                self.last_attempt_count += 1
                configured_reasoning = (
                    getattr(config, "article_editor_reasoning_effort", None)
                    if config is not None
                    else None
                )
                resolved_reasoning = (
                    configured_reasoning
                    if configured_reasoning is not None
                    else self.reasoning_effort
                )
                with capture_provider_attempts(self.provider) as counts:
                    try:
                        response = await self.provider.chat_completion(
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            model=self.model,
                            temperature=self.temperature,
                            max_tokens=self.max_output_tokens,
                            reasoning_effort=resolved_reasoning,
                            response_format={"type": "json_object"},
                        )
                    finally:
                        self.last_provider_attempts.append(counts.to_metadata())
                operation_error: str | None = None
                try:
                    operations = self._parse_structural_operations(response)
                except ValueError as exc:
                    operations = ()
                    operation_error = str(exc)
                    self.last_structural_operations.append(
                        {
                            "attempt": attempt,
                            "status": "rejected",
                            "reason": operation_error,
                        }
                    )
                if operations:
                    self.last_structural_operations.extend(
                        self._operation_metadata(
                            operation, attempt, "proposed", "awaiting_assessment"
                        )
                        for operation in operations
                    )
                    if structural_context is None:
                        operation_error = structural_context_reason
                    elif (
                        self._response_object(response).get("base_fingerprint")
                        != registry.base_fingerprint
                    ):
                        operation_error = "stale_base_fingerprint"
                patches, parse_reason, _unknown_unit_count = self._parse_editor_response_details(
                    response, requested_units
                )
                unit_outcomes = {
                    unit_id: {
                        "status": "not_returned",
                        "reason": "patch_not_returned",
                        "attempted_text": "",
                    }
                    for unit_id in requested_units
                }
                if not patches and not operations and operation_error is None:
                    logger.warning("ArticleEditor returned no valid unit patches")
                    reason = (
                        parse_reason if parse_reason != "completed" else "no_measurable_progress"
                    )
                    for unit_id, unit in prompt_units_by_id.items():
                        signature = target_signature(unit)
                        attempted_target_signatures[signature] = 2
                        previous_attempt_feedback[signature] = {
                            "status": "not_returned",
                            "reason": "patch_not_returned",
                        }
                        self.last_unit_outcomes.append(
                            safe_unit_outcome(
                                pass_index=attempt,
                                unit_id=unit_id,
                                status="not_returned",
                                reason="patch_not_returned",
                                issues=unit["issues"],
                                required_support_count=unit["required_support_count"],
                                shown_support_count=unit["shown_support_count"],
                                base_fingerprint=base_fingerprint,
                                result_fingerprint=base_fingerprint,
                            )
                        )
                    append_pass_outcome(
                        pass_index=attempt,
                        reason=reason,
                        requested=len(requested_units),
                        applied=0,
                        deferred=deferred_count,
                        unresolved=len(issues_by_unit) + unlocalizable_count,
                        elapsed=perf_counter() - attempt_started,
                        base_fingerprint=base_fingerprint,
                        unknown_unit_id_count=_unknown_unit_count,
                    )
                    if attempt_observer is not None:
                        await attempt_observer.attempt_finished(
                            obs_att_id,
                            "failed",
                            error_kind="empty_patches",
                            metadata={
                                "unit_outcomes": self._compact_unit_outcomes(unit_outcomes),
                                "structural_operations": list(self.last_structural_operations),
                                "provider_attempts": self.last_provider_attempts[-1]
                                if self.last_provider_attempts
                                else None,
                                "logical_editor_invocation": self.last_attempt_count,
                                "pass_elapsed_seconds": round(perf_counter() - attempt_started, 3),
                            },
                        )
                    self._save_attempt_debug_artifact(
                        save_debug_artifact,
                        debug_artifact_prefix,
                        attempt,
                        user_prompt,
                        response,
                        current_draft,
                        unit_outcomes,
                        current_val,
                        current_quality,
                    )
                    if attempt < max_attempts and (deferred_count or not current_val.is_valid):
                        continue
                    break

                for unit_id, patch_text in tuple(patches.items()):
                    prompt_unit = prompt_units_by_id[unit_id]
                    patch_signature = self._patch_signature(
                        patch_text,
                        original_text=prompt_unit["text"],
                        support_ids=prompt_unit["prompt_support_ids"],
                    )
                    if patch_signature in no_op_patch_signatures.get(unit_id, set()):
                        unit_outcomes[unit_id] = {
                            "status": "no_op",
                            "reason": "repeated_identical_no_op_patch",
                            "attempted_text": patch_text,
                        }
                        del patches[unit_id]

                if not patches and not operations and operation_error is None:
                    logger.warning(
                        "ArticleEditor pass %d repeated only previously rejected/no-op patches; stopping",
                        attempt,
                    )
                    for unit_id, unit in prompt_units_by_id.items():
                        outcome = unit_outcomes.get(unit_id, {})
                        if outcome.get("status") == "no_op":
                            status, reason = "no_change", "text_unchanged"
                        elif outcome.get("status") == "rejected":
                            status, reason = "rejected", "patch_rejected"
                        else:
                            status, reason = "not_returned", "patch_not_returned"
                        signature = target_signature(unit)
                        attempted_target_signatures[signature] = 2
                        previous_attempt_feedback[signature] = {"status": status, "reason": reason}
                        self.last_unit_outcomes.append(
                            safe_unit_outcome(
                                pass_index=attempt,
                                unit_id=unit_id,
                                status=status,
                                reason=reason,
                                issues=unit["issues"],
                                required_support_count=unit["required_support_count"],
                                shown_support_count=unit["shown_support_count"],
                                base_fingerprint=base_fingerprint,
                                result_fingerprint=base_fingerprint,
                            )
                        )
                    append_pass_outcome(
                        pass_index=attempt,
                        reason=(
                            parse_reason
                            if parse_reason != "completed"
                            else "no_measurable_progress"
                        ),
                        requested=len(requested_units),
                        applied=0,
                        deferred=deferred_count,
                        unresolved=len(issues_by_unit) + unlocalizable_count,
                        elapsed=perf_counter() - attempt_started,
                        base_fingerprint=base_fingerprint,
                    )
                    if attempt_observer is not None:
                        await attempt_observer.attempt_finished(
                            obs_att_id,
                            "failed",
                            error_kind="repeated_no_op_patches",
                            metadata={
                                "unit_outcomes": self._compact_unit_outcomes(unit_outcomes),
                                "structural_operations": list(self.last_structural_operations),
                                "provider_attempts": self.last_provider_attempts[-1]
                                if self.last_provider_attempts
                                else None,
                                "logical_editor_invocation": self.last_attempt_count,
                                "pass_elapsed_seconds": round(perf_counter() - attempt_started, 3),
                            },
                        )
                    self._save_attempt_debug_artifact(
                        save_debug_artifact,
                        debug_artifact_prefix,
                        attempt,
                        user_prompt,
                        response,
                        current_draft,
                        unit_outcomes,
                        current_val,
                        current_quality,
                    )
                    if attempt < max_attempts and deferred_count:
                        continue
                    break

                current_draft = await asyncio.to_thread(
                    self.apply_patches,
                    current_draft,
                    patches,
                    context=validation_context,
                    preserve_unmatched_supports=False,
                    allowed_support_ids_by_unit={
                        unit["unit_id"]: tuple(unit["prompt_support_ids"]) for unit in prompt_data
                    },
                    patch_outcomes=unit_outcomes,
                )
                actually_changed = [
                    unit_id
                    for unit_id, outcome in unit_outcomes.items()
                    if outcome.get("status") == "applied"
                ]
                patched_unit_ids.extend(actually_changed)
                self.last_patched_unit_ids = tuple(dict.fromkeys(patched_unit_ids))
                for unit_id, outcome in unit_outcomes.items():
                    if outcome.get("status") != "applied":
                        prompt_unit = prompt_units_by_id[unit_id]
                        no_op_patch_signatures.setdefault(unit_id, set()).add(
                            self._patch_signature(
                                outcome.get("attempted_text", ""),
                                original_text=prompt_unit["text"],
                                support_ids=prompt_unit["prompt_support_ids"],
                            )
                        )

                async def evaluate_editor_draft(
                    candidate: StructuredArticleDraft,
                ) -> tuple[
                    ArticleValidationResult,
                    ArticleReaderQualityReport,
                    float,
                    float,
                    ArticleAssessmentCheckpoint,
                ]:
                    started = perf_counter()
                    checkpoint = await assess_candidate(candidate)
                    return (
                        checkpoint.validation,
                        checkpoint.quality,
                        perf_counter() - started,
                        0.0,
                        checkpoint,
                    )

                validation_elapsed = 0.0
                quality_elapsed = 0.0
                if actually_changed and checkpoint_observer is not None:
                    checkpoint_observer("editor_candidate", current_draft, None)
                if actually_changed:
                    (
                        current_val,
                        current_quality,
                        validation_elapsed,
                        quality_elapsed,
                        _candidate_assessment,
                    ) = await evaluate_editor_draft(current_draft)
                new_blockers = (
                    blocking_keys(current_val, current_quality).keys()
                    - blocking_keys(previous_val, previous_quality).keys()
                )
                quarantine_units = {
                    blocking_keys(current_val, current_quality)[key]
                    for key in new_blockers
                    if key[0] == "evidence"
                }
                for key in new_blockers:
                    if key[0] != "quality":
                        continue
                    finding = next(
                        finding
                        for finding in current_quality.blocking_findings
                        if (
                            "quality",
                            finding.code,
                            finding.unit_id,
                            tuple(sorted(finding.support_ids)),
                            finding.message,
                        )
                        == key
                    )
                    localized = self._localize_article_quality_finding(current_draft, finding)
                    # A support_units policy may explicitly identify the changed
                    # paragraphs behind an article-wide structural finding.
                    if localized:
                        quarantine_units.update(item.unit_id for item in localized)
                    else:
                        quarantine_units.add(finding.unit_id)
                if new_blockers:
                    # Only an explicit changed unit is safe to attribute. An
                    # ARTICLE/DRAFT finding or an unchanged unit rolls back the
                    # entire batch; no guessed subset or combinatorial search.
                    if quarantine_units <= set(actually_changed):
                        retained_patches = {
                            unit: text
                            for unit, text in patches.items()
                            if unit not in quarantine_units
                        }
                        retained_outcomes: dict[str, dict[str, str]] = {}
                        current_draft = await asyncio.to_thread(
                            self.apply_patches,
                            previous_draft,
                            retained_patches,
                            context=validation_context,
                            allowed_support_ids_by_unit={
                                unit["unit_id"]: tuple(unit["prompt_support_ids"])
                                for unit in prompt_data
                            },
                            patch_outcomes=retained_outcomes,
                        )
                        # Exactly one validation of the remainder, including
                        # the all-quarantined case; never validate per unit.
                        (
                            current_val,
                            current_quality,
                            elapsed_val,
                            elapsed_quality,
                            _retained_assessment,
                        ) = await evaluate_editor_draft(current_draft)
                        validation_elapsed += elapsed_val
                        quality_elapsed += elapsed_quality
                        if (
                            blocking_keys(current_val, current_quality).keys()
                            - blocking_keys(previous_val, previous_quality).keys()
                        ):
                            quarantine_units = set(actually_changed)
                            current_draft, current_val, current_quality = (
                                previous_draft,
                                previous_val,
                                previous_quality,
                            )
                    else:
                        quarantine_units = set(actually_changed)
                        current_draft, current_val, current_quality = (
                            previous_draft,
                            previous_val,
                            previous_quality,
                        )
                    for unit_id in quarantine_units:
                        unit_outcomes[unit_id]["status"] = "rejected"
                        unit_outcomes[unit_id]["reason"] = "new_blocker_quarantined"
                    actually_changed = [
                        unit for unit in actually_changed if unit not in quarantine_units
                    ]
                    patched_unit_ids = previous_patched_unit_ids + actually_changed
                    self.last_patched_unit_ids = tuple(dict.fromkeys(patched_unit_ids))

                # Legacy text patches have now passed their original quarantine
                # path. This exact checkpoint is the rollback base for structure.
                structure_base = current_draft
                structure_val = current_val
                structure_quality = current_quality
                structure_changed = False
                rollback_draft, rollback_val, rollback_quality = (
                    structure_base,
                    structure_val,
                    structure_quality,
                )
                rollback_assessment = matching_assessment(structure_base)
                if rollback_assessment is None:
                    rollback_assessment = await assess_candidate(structure_base)
                rollback_patched_unit_ids = list(patched_unit_ids)
                self.last_assessment = rollback_assessment
                if operations:
                    if operation_error is None:
                        try:
                            structural_candidate, origins = await asyncio.to_thread(
                                self.apply_structural_operations,
                                structure_base,
                                operations,
                                registry,
                                validation_context,
                                text_patched_unit_ids=frozenset(actually_changed),
                            )
                            if checkpoint_observer is not None:
                                checkpoint_observer("editor_candidate", structural_candidate, None)
                            (
                                candidate_val,
                                candidate_quality,
                                elapsed_val,
                                elapsed_quality,
                                _candidate_assessment,
                            ) = await evaluate_editor_draft(structural_candidate)
                            validation_elapsed += elapsed_val
                            quality_elapsed += elapsed_quality
                            base_blockers = self._mapped_blocking_keys(
                                structure_val, structure_quality, self._identity_map(structure_base)
                            )
                            candidate_blockers = self._mapped_blocking_keys(
                                candidate_val, candidate_quality, origins
                            )
                            if candidate_blockers - base_blockers:
                                operation_error = "new_blocker_atomic_rollback"
                            else:
                                current_draft, current_val, current_quality = (
                                    structural_candidate,
                                    candidate_val,
                                    candidate_quality,
                                )
                                structure_changed = structural_candidate != structure_base
                        except TimeoutError:
                            raise
                        except ValueError as exc:
                            # Only locally defined reason codes enter metadata.
                            operation_error = (
                                str(exc)
                                if str(exc) in ARTICLE_STRUCTURAL_OUTCOME_REASONS
                                else "assessment_error_atomic_rollback"
                            )
                        except Exception:
                            operation_error = "assessment_error_atomic_rollback"
                    if operation_error is not None:
                        current_draft, current_val, current_quality = (
                            structure_base,
                            structure_val,
                            structure_quality,
                        )
                        self.last_assessment = await assess_candidate(structure_base)
                    self.last_structural_operations.extend(
                        self._operation_metadata(
                            operation,
                            attempt,
                            "skipped"
                            if structural_context is None
                            else "rejected"
                            if operation_error
                            else "applied",
                            operation_error
                            or (
                                "candidate_assessed" if structure_changed else "structure_unchanged"
                            ),
                        )
                        for operation in operations
                    )
                    if structure_changed:
                        patched_unit_ids.extend(
                            f"op:{operation.operation_id}" for operation in operations
                        )
                        self.last_patched_unit_ids = tuple(dict.fromkeys(patched_unit_ids))
                    for operation in operations:
                        for source_unit_id in operation.source_unit_ids:
                            if source_unit_id not in unit_outcomes:
                                continue
                            if structure_changed:
                                unit_outcomes[source_unit_id] = {
                                    "status": "applied",
                                    "reason": "structural_operation_applied",
                                    "attempted_text": "",
                                }
                            elif operation_error is not None:
                                if unit_outcomes[source_unit_id].get("status") != "applied":
                                    unit_outcomes[source_unit_id] = {
                                        "status": "rolled_back",
                                        "reason": "structural_batch_rollback",
                                        "attempted_text": "",
                                    }
                            elif unit_outcomes[source_unit_id].get("status") == "not_returned":
                                unit_outcomes[source_unit_id] = {
                                    "status": "no_op",
                                    "reason": "structure_unchanged",
                                    "attempted_text": "",
                                }

                final_checkpoint = await assess_candidate(current_draft)
                current_val, current_quality = final_checkpoint.validation, final_checkpoint.quality
                self.last_quality_report = current_quality
                if checkpoint_observer is not None:
                    checkpoint_observer("editor", current_draft, final_checkpoint)
                made_progress = bool(
                    {key for key in previous_actionable if key[1] in requested_units}
                    - actionable_keys(current_val, current_quality)
                )
                if structure_changed:
                    # A split may need the second pass to move its isolated
                    # paragraph even before the thematic finding disappears.
                    made_progress = True
                    no_op_patch_signatures.clear()
                result_fingerprint = _draft_fingerprint(current_draft)
                for recorded_outcome in self.last_unit_outcomes:
                    if (
                        recorded_outcome["pass_index"] == attempt
                        and recorded_outcome["base_fingerprint"] == base_fingerprint
                    ):
                        recorded_outcome["result_fingerprint"] = result_fingerprint
                for unit_id, unit in prompt_units_by_id.items():
                    outcome = unit_outcomes.get(unit_id, {})
                    if outcome.get("status") == "applied":
                        status, reason = "applied", "accepted_change"
                        attempted_target_signatures[target_signature(unit)] = (
                            1 if made_progress else 2
                        )
                    elif outcome.get("status") == "no_op":
                        status, reason = "no_change", "text_unchanged"
                        attempted_target_signatures[target_signature(unit)] = 2
                    elif outcome.get("status") == "rejected":
                        status = "rejected"
                        reason = (
                            "new_blocker_quarantined"
                            if outcome.get("reason") == "new_blocker_quarantined"
                            else "patch_rejected"
                        )
                        attempted_target_signatures[target_signature(unit)] = 2
                    elif outcome.get("status") == "rolled_back":
                        status, reason = "rolled_back", "structural_batch_rollback"
                        attempted_target_signatures[target_signature(unit)] = 2
                    else:
                        status, reason = "not_returned", "patch_not_returned"
                        attempted_target_signatures[target_signature(unit)] = 2
                    previous_attempt_feedback[target_signature(unit)] = {
                        "status": status,
                        "reason": reason,
                    }
                    self.last_unit_outcomes.append(
                        safe_unit_outcome(
                            pass_index=attempt,
                            unit_id=unit_id,
                            status=status,
                            reason=reason,
                            issues=unit["issues"],
                            required_support_count=unit["required_support_count"],
                            shown_support_count=unit["shown_support_count"],
                            base_fingerprint=base_fingerprint,
                            result_fingerprint=result_fingerprint,
                        )
                    )
                applied_unit_ids = {
                    unit_id
                    for unit_id, outcome in unit_outcomes.items()
                    if outcome.get("status") == "applied"
                }
                if structure_changed:
                    applied_unit_ids.update(
                        unit_id for operation in operations for unit_id in operation.source_unit_ids
                    )
                applied_unit_count = len(applied_unit_ids)
                final_blocking_issues = [issue for issue in current_val.issues if issue.blocking]
                final_localized_quality = [
                    localized
                    for finding in current_quality.repair_findings
                    for localized in self._localize_article_quality_finding(current_draft, finding)
                ]
                final_unlocalizable_quality_count = sum(
                    not self._localize_article_quality_finding(current_draft, finding)
                    for finding in current_quality.repair_findings
                )
                unresolved_target_count = (
                    len(
                        {
                            issue.unit_id
                            for issue in final_blocking_issues
                            if issue.unit_id not in ("DRAFT", "ARTICLE", "")
                        }
                        | {localized.unit_id for localized in final_localized_quality}
                    )
                    + sum(
                        issue.unit_id in ("DRAFT", "ARTICLE", "") for issue in final_blocking_issues
                    )
                    + final_unlocalizable_quality_count
                )
                pass_reason = (
                    parse_reason
                    if parse_reason != "completed"
                    else "all_targets_resolved"
                    if unresolved_target_count == 0
                    else "deferred_units_remain"
                    if deferred_count
                    else "completed"
                    if made_progress
                    else "no_measurable_progress"
                )
                append_pass_outcome(
                    pass_index=attempt,
                    reason=pass_reason,
                    requested=len(requested_units),
                    applied=applied_unit_count,
                    deferred=deferred_count,
                    unresolved=unresolved_target_count,
                    elapsed=perf_counter() - attempt_started,
                    base_fingerprint=base_fingerprint,
                    unknown_unit_id_count=_unknown_unit_count,
                )
                logger.info(
                    "ArticleEditor pass %d timings: evidence_validation=%.2fs "
                    "quality=%.2fs pass_elapsed=%.2fs",
                    attempt,
                    validation_elapsed,
                    quality_elapsed,
                    perf_counter() - attempt_started,
                )

                if attempt_observer is not None:
                    is_clean = current_val.is_valid and not current_quality.needs_edit
                    status = "succeeded" if is_clean else "failed"
                    error_kind = None if is_clean else "remaining_violations"
                    await attempt_observer.attempt_finished(
                        obs_att_id,
                        status,
                        error_kind=error_kind,
                        metadata={
                            "editor_status": "succeeded" if is_clean else "partial",
                            "provider_attempts": self.last_provider_attempts[-1]
                            if self.last_provider_attempts
                            else None,
                            "logical_editor_invocation": self.last_attempt_count,
                            "pass_elapsed_seconds": round(perf_counter() - attempt_started, 3),
                            "requested_units": sorted(requested_units),
                            "patched_units": actually_changed,
                            "unit_outcomes": self._compact_unit_outcomes(unit_outcomes),
                            "structural_operations": list(self.last_structural_operations),
                            "remaining_violations": list(current_val.violations),
                            "remaining_quality_findings": [
                                f"{finding.code}:{finding.unit_id}"
                                for finding in current_quality.repair_findings
                            ],
                        },
                    )

                self._save_attempt_debug_artifact(
                    save_debug_artifact,
                    debug_artifact_prefix,
                    attempt,
                    user_prompt,
                    response,
                    current_draft,
                    unit_outcomes,
                    current_val,
                    current_quality,
                )

                if not (actually_changed or structure_changed) or not made_progress:
                    logger.info("ArticleEditor stopped after no measurable targeted progress")
                    if attempt < max_attempts and (deferred_count or not current_val.is_valid):
                        continue
                    break
                if current_val.is_valid and not current_quality.needs_edit:
                    logger.info("ArticleEditor successfully resolved all validation issues!")
                    break
                else:
                    remaining_issues = [
                        f"{issue.code}:{issue.unit_id}"
                        for issue in current_val.issues
                        if issue.blocking
                    ] + [
                        f"{finding.code}:{finding.unit_id}"
                        for finding in current_quality.repair_findings
                    ]
                    logger.warning(
                        "ArticleEditor pass %d left remaining issues: %s",
                        attempt,
                        remaining_issues[:10],
                    )

            except TimeoutError:
                current_draft, current_val, current_quality = (
                    rollback_draft,
                    rollback_val,
                    rollback_quality,
                )
                checkpoint = matching_assessment(rollback_draft) or rollback_assessment
                if checkpoint is not None and checkpoint.matches(
                    rollback_draft,
                    input_fingerprint,
                    source_identity=source_identity,
                ):
                    self.last_assessment = checkpoint
                else:
                    self.last_assessment = None
                self.last_quality_report = current_quality
                result_fingerprint = _draft_fingerprint(current_draft)
                for unit_id, unit in prompt_units_by_id.items():
                    outcome = unit_outcomes.get(unit_id, {})
                    if outcome.get("status") == "applied":
                        status, reason = "rolled_back", "deadline_exhausted"
                    else:
                        status, reason = "not_returned", "deadline_exhausted"
                    self.last_unit_outcomes.append(
                        safe_unit_outcome(
                            pass_index=attempt,
                            unit_id=unit_id,
                            status=status,
                            reason=reason,
                            issues=unit["issues"],
                            required_support_count=unit["required_support_count"],
                            shown_support_count=unit["shown_support_count"],
                            base_fingerprint=base_fingerprint,
                            result_fingerprint=result_fingerprint,
                        )
                    )
                append_pass_outcome(
                    pass_index=attempt,
                    reason="deadline_exhausted",
                    requested=len(requested_units),
                    applied=0,
                    deferred=deferred_count,
                    unresolved=len(issues_by_unit) + unlocalizable_count,
                    elapsed=perf_counter() - attempt_started,
                    base_fingerprint=base_fingerprint,
                    unknown_unit_id_count=_unknown_unit_count,
                )
                patched_unit_ids = rollback_patched_unit_ids
                self.last_patched_unit_ids = tuple(dict.fromkeys(patched_unit_ids))
                raise
            except Exception as exc:
                # Keep text, Evidence Boundary result, and quality report as
                # one transaction. A failed validation/diagnostic must not
                # return a patched draft paired with stale assessment data.
                current_draft = rollback_draft
                current_val = rollback_val
                current_quality = rollback_quality
                self.last_quality_report = current_quality
                checkpoint = matching_assessment(rollback_draft) or rollback_assessment
                if checkpoint is not None and checkpoint.matches(
                    rollback_draft,
                    input_fingerprint,
                    source_identity=source_identity,
                ):
                    self.last_assessment = checkpoint
                else:
                    self.last_assessment = None
                patched_unit_ids = rollback_patched_unit_ids
                for operation_outcome in self.last_structural_operations:
                    if operation_outcome.get("attempt") == attempt and operation_outcome.get(
                        "status"
                    ) in {"applied", "proposed"}:
                        operation_outcome["status"] = "rejected"
                        operation_outcome["reason"] = "editor_pass_error_atomic_rollback"
                self.last_patched_unit_ids = tuple(dict.fromkeys(patched_unit_ids))
                result_fingerprint = _draft_fingerprint(current_draft)
                for unit_id, unit in prompt_units_by_id.items():
                    outcome = unit_outcomes.get(unit_id, {})
                    if outcome.get("status") == "applied":
                        status, reason = "rolled_back", "editor_pass_error_atomic_rollback"
                    elif outcome.get("status") == "rejected":
                        status, reason = "rejected", "patch_rejected"
                    elif outcome.get("status") == "no_op":
                        status, reason = "no_change", "text_unchanged"
                    else:
                        status, reason = "not_returned", "editor_pass_error_atomic_rollback"
                    signature = target_signature(unit)
                    attempted_target_signatures[signature] = 2
                    previous_attempt_feedback[signature] = {"status": status, "reason": reason}
                    self.last_unit_outcomes.append(
                        safe_unit_outcome(
                            pass_index=attempt,
                            unit_id=unit_id,
                            status=status,
                            reason=reason,
                            issues=unit["issues"],
                            required_support_count=unit["required_support_count"],
                            shown_support_count=unit["shown_support_count"],
                            base_fingerprint=base_fingerprint,
                            result_fingerprint=result_fingerprint,
                        )
                    )
                logger.warning(
                    "ArticleEditor pass %d encountered error type %s",
                    attempt,
                    type(exc).__name__,
                )
                if not unit_outcomes:
                    unit_outcomes = {
                        unit_id: {
                            "status": "rejected",
                            "reason": f"editor_pass_error:{type(exc).__name__}",
                            "attempted_text": "",
                        }
                        for unit_id in requested_units
                    }
                append_pass_outcome(
                    pass_index=attempt,
                    reason="editor_pass_error_atomic_rollback",
                    requested=len(requested_units),
                    applied=0,
                    deferred=deferred_count,
                    unresolved=len(issues_by_unit) + unlocalizable_count,
                    elapsed=perf_counter() - attempt_started,
                    base_fingerprint=base_fingerprint,
                    unknown_unit_id_count=_unknown_unit_count,
                )
                if attempt_observer is not None:
                    await attempt_observer.attempt_finished(
                        obs_att_id,
                        "failed",
                        error_kind=type(exc).__name__,
                        metadata={
                            "unit_outcomes": self._compact_unit_outcomes(unit_outcomes),
                            "structural_operations": list(self.last_structural_operations),
                            "provider_attempts": self.last_provider_attempts[-1]
                            if self.last_provider_attempts
                            else None,
                            "logical_editor_invocation": self.last_attempt_count,
                            "pass_elapsed_seconds": round(perf_counter() - attempt_started, 3),
                        },
                    )
                self._save_attempt_debug_artifact(
                    save_debug_artifact,
                    debug_artifact_prefix,
                    attempt,
                    user_prompt,
                    response,
                    current_draft,
                    unit_outcomes,
                    current_val,
                    current_quality,
                )
                break

        return current_draft, current_val

    def _build_unit_contexts(
        self,
        draft: StructuredArticleDraft,
        issues_by_unit: Mapping[str, list[Any]],
        context: ArticleEditorialContext,
        *,
        material_projection: ArticleMaterialProjection | None = None,
    ) -> list[dict[str, Any]]:
        """Collect current text, cited supports, and issues for each target unit."""
        unit_data: list[dict[str, Any]] = []

        def editor_visible_support(support_id: str) -> bool:
            support = context.support_by_id.get(support_id)
            if support is None or support.publication_use != "PUBLISH":
                return False
            if material_projection is None:
                return bool(
                    sanitize_writer_source_text((support.text or "").strip())
                    or sanitize_writer_source_text((support.source_text or "").strip())
                )
            if (
                material_projection.actions_by_support_id.get(support_id)
                == "SUPPRESS_PROMOTION_ONLY"
            ):
                return False
            return bool(
                sanitize_writer_source_text(
                    material_projection.text_by_support_id.get(support_id, "").strip()
                )
            )

        def support_text(support_id: str) -> str:
            support = context.support_by_id.get(support_id)
            if support is None or not editor_visible_support(support_id):
                return ""
            fact = sanitize_writer_source_text((support.text or "").strip())
            if material_projection is not None:
                if (
                    material_projection.actions_by_support_id.get(support_id)
                    == "SUPPRESS_PROMOTION_ONLY"
                ):
                    return ""
                projected = sanitize_writer_source_text(
                    material_projection.text_by_support_id.get(support_id, "").strip()
                )
                if not projected:
                    return ""
                fact = projected
                source = ""
            else:
                source = sanitize_writer_source_text((support.source_text or "").strip())

            fields = [
                f"kind={support.support_kind}",
                f"evidence_kind={support.evidence_kind}",
                f"temporal_role={support.temporal_role}",
                f"source_roles={','.join(support.source_roles) if support.source_roles else 'unknown'}",
                f"framing={_support_framing(support)}",
            ]
            if support.story_id:
                fields.append(f"story_id={support.story_id}")
            for field_name, value in (
                ("observed_at", support.observed_at),
                ("effective_from", support.effective_from),
                ("effective_until", support.effective_until),
            ):
                if value is not None:
                    fields.append(f"{field_name}={value.isoformat()}")
            fields.append(f"fact={fact}")
            if source and source != fact:
                fields.append(f"primary_source={source}")
            parent_context = sanitize_writer_source_text(
                (support.reply_parent_context_text or "").strip()
            )
            if parent_context:
                fields.append(
                    "reply_parent_context (subject/place only; not a status or answer)="
                    f"{parent_context}"
                )
            return "\n".join(fields)

        def support_packets(support_ids: list[str] | tuple[str, ...]) -> list[dict[str, str]]:
            packets: list[dict[str, str]] = []
            for support_id in dict.fromkeys(support_ids):
                rendered = support_text(support_id)
                if rendered:
                    packets.append({"support_id": support_id, "text": rendered})
            return packets

        def current_body_repair_supports() -> list[str]:
            """Return a small current-window allowlist already cited by body prose."""
            body_support_ids: list[str] = []
            for section in draft.sections:
                for paragraph in section.paragraphs:
                    body_support_ids.extend(paragraph.cited_support_ids)
                    body_support_ids.extend(
                        support_id
                        for claim in paragraph.claims
                        for support_id in claim.cited_support_ids
                    )

            visible_current_ids: list[str] = []
            for support_id in dict.fromkeys(body_support_ids):
                support = context.support_by_id.get(support_id)
                if (
                    support is None
                    or support.publication_use != "PUBLISH"
                    or support.temporal_role != "CURRENT_WINDOW"
                    or not editor_visible_support(support_id)
                ):
                    continue
                visible_current_ids.append(support_id)
                if len(visible_current_ids) == _MAX_TITLE_LEAD_REPAIR_SUPPORTS:
                    break
            return visible_current_ids

        # Index units across draft
        # 1. Title
        def unit_supports(
            unit_ids: list[str],
            claim_ids: list[str],
            unit_issues: list[Any],
        ) -> list[str]:
            # Required support IDs from a quality finding must appear first so
            # the prompt's bounded five-support display cannot hide the exact
            # evidence that the editor is asked to restore.
            ids: list[str] = []
            for issue in unit_issues:
                ids.extend(getattr(issue, "support_ids", ()) or ())
            ids.extend(unit_ids)
            ids.extend(claim_ids)
            return list(dict.fromkeys(ids))

        def heading_section_context(
            section: ArticleSection,
        ) -> tuple[tuple[tuple[int, str, tuple[str, ...]], ...], int, int]:
            """Return bounded section prose for a heading repair.

            Heading/theme decisions need the actual section body, not only its
            heading citations. Include every paragraph slot, sanitize direct
            contact details, and make any text truncation explicit.
            """
            included: list[tuple[int, str, tuple[str, ...]]] = []
            remaining_chars = _MAX_HEADING_SECTION_CONTEXT_CHARS
            truncated_paragraphs = 0
            omitted_paragraphs = 0
            for paragraph_index, paragraph in enumerate(section.paragraphs, start=1):
                # Article text may itself contain a phone number or URL. Keep
                # editor context subject to the same privacy filter as source
                # excerpts; this does not alter the stored draft text.
                text = " ".join(sanitize_writer_source_text(paragraph.text).split()).strip()
                paragraph_support_ids = tuple(
                    support_id
                    for support_id in dict.fromkeys(
                        (
                            *paragraph.cited_support_ids,
                            *(
                                support_id
                                for claim in paragraph.claims
                                for support_id in claim.cited_support_ids
                            ),
                        )
                    )
                    if editor_visible_support(support_id)
                )
                was_truncated = False
                if len(text) > _MAX_HEADING_SECTION_PARAGRAPH_CHARS:
                    text = text[: _MAX_HEADING_SECTION_PARAGRAPH_CHARS - 3].rstrip() + "..."
                    was_truncated = True
                if len(text) > remaining_chars:
                    if remaining_chars > 3:
                        text = text[: remaining_chars - 3].rstrip() + "..."
                        was_truncated = True
                    else:
                        text = "[текст абзаца не показан: достигнут лимит контекста]"
                        omitted_paragraphs += 1
                if was_truncated:
                    truncated_paragraphs += 1
                if text.startswith("[текст абзаца не показан"):
                    included.append((paragraph_index, text, paragraph_support_ids))
                    continue
                included.append((paragraph_index, text, paragraph_support_ids))
                remaining_chars = max(0, remaining_chars - len(text))
            return tuple(included), omitted_paragraphs, truncated_paragraphs

        def section_heading_supports(
            section: ArticleSection,
            unit_issues: list[Any],
        ) -> list[str]:
            """Keep required heading evidence first, then all section evidence."""
            paragraph_support_ids: list[str] = []
            for paragraph in section.paragraphs:
                paragraph_support_ids.extend(paragraph.cited_support_ids)
                paragraph_support_ids.extend(
                    support_id
                    for claim in paragraph.claims
                    for support_id in claim.cited_support_ids
                )
            return unit_supports(
                [*section.heading_support_ids, *paragraph_support_ids],
                [
                    support_id
                    for claim in section.heading_claims
                    for support_id in claim.cited_support_ids
                ],
                unit_issues,
            )

        def heading_supports_for_prompt(
            section: ArticleSection,
            support_ids: list[str],
            unit_issues: list[Any],
        ) -> list[str]:
            """Show targeted supports first, then at least one per paragraph."""
            prioritized: list[str] = []
            for issue in unit_issues:
                prioritized.extend(getattr(issue, "support_ids", ()) or ())
            prioritized.extend(section.heading_support_ids)
            paragraph_support_ids: list[list[str]] = []
            for paragraph in section.paragraphs:
                ids = list(paragraph.cited_support_ids)
                ids.extend(
                    support_id
                    for claim in paragraph.claims
                    for support_id in claim.cited_support_ids
                )
                paragraph_support_ids.append(list(dict.fromkeys(ids)))
            prioritized.extend(ids[0] for ids in paragraph_support_ids if ids)
            prioritized.extend(support_id for ids in paragraph_support_ids for support_id in ids)
            prioritized.extend(support_ids)
            available = {
                support_id for support_id in support_ids if editor_visible_support(support_id)
            }
            return [
                support_id for support_id in dict.fromkeys(prioritized) if support_id in available
            ]

        def current_title_repair_supports() -> list[str]:
            """Find current-window article evidence for repairing a historical title.

            A title that cites only historical supports cannot be repaired by
            rephrasing those same supports: the validator will still correctly
            reject it for lacking current-window evidence. Offer the editor
            only relevant, current-window supports already used elsewhere in
            this draft, then constrain re-grounding to those same IDs.
            """
            from src.publication.article_claims import _stem
            from src.publication.article_semantic_support import _STOPWORDS

            draft_support_ids: list[str] = list(draft.cited_support_ids)
            for claim in (*draft.title_claims, *draft.lead_claims):
                draft_support_ids.extend(claim.cited_support_ids)
            for section in draft.sections:
                for claim in section.heading_claims:
                    draft_support_ids.extend(claim.cited_support_ids)
                for paragraph in section.paragraphs:
                    draft_support_ids.extend(paragraph.cited_support_ids)
                    for claim in paragraph.claims:
                        draft_support_ids.extend(claim.cited_support_ids)
            draft_support_ids = list(dict.fromkeys(draft_support_ids))

            historical_text = [draft.title]
            for support_id in unit_supports(
                list(draft.title_support_ids),
                [sid for claim in draft.title_claims for sid in claim.cited_support_ids],
                issues_by_unit.get("TITLE", []),
            ):
                support = context.support_by_id.get(support_id)
                if support and support.temporal_role == "HISTORICAL_CONTEXT":
                    historical_text.extend((support.text, support.source_text))

            token_re = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)

            def distinctive_stems(value: str) -> set[str]:
                stems: set[str] = set()
                for token in token_re.findall(value or ""):
                    normalized = token.casefold().replace("ё", "е")
                    if len(normalized) < 3 or normalized in _STOPWORDS:
                        continue
                    stems.add(_stem(normalized))
                return stems

            title_stems = distinctive_stems(" ".join(historical_text))
            if not title_stems:
                return []

            scored: list[tuple[int, str]] = []
            for support_id in draft_support_ids:
                support = context.support_by_id.get(support_id)
                if (
                    support is None
                    or support.publication_use != "PUBLISH"
                    or support.temporal_role != "CURRENT_WINDOW"
                ):
                    continue
                overlap = len(
                    title_stems & distinctive_stems(f"{support.text} {support.source_text}")
                )
                if overlap:
                    scored.append((overlap, support_id))

            # Keep the editor's task small and focused. The lexical filter is
            # only used to select candidate evidence; apply_patches still
            # performs the normal grounded, support-limited re-grounding.
            scored.sort(key=lambda item: (-item[0], draft_support_ids.index(item[1])))
            return [support_id for _, support_id in scored[:6]]

        if "TITLE" in issues_by_unit:
            title_issues = issues_by_unit["TITLE"]
            missing_title = any(
                getattr(issue, "code", "") == "EMPTY_TITLE" for issue in title_issues
            )
            title_has_unframed_history = any(
                getattr(issue, "code", "") == "HISTORICAL_CONTEXT_UNFRAMED"
                for issue in title_issues
            )
            if missing_title:
                # A missing title has no own citations. Offer only a few
                # current-window PUBLISH supports that the body already cites;
                # the same IDs will be the patch's exact grounding allowlist.
                t_sups = current_body_repair_supports()
            elif title_has_unframed_history:
                # The candidate title must be grounded in today's article
                # material. Keeping the old historical IDs in the allowed set
                # would let the title retain the stale claim and fail again.
                t_sups = current_title_repair_supports()
            else:
                t_sups = unit_supports(
                    list(draft.title_support_ids),
                    [sid for claim in draft.title_claims for sid in claim.cited_support_ids],
                    issues_by_unit["TITLE"],
                )
            unit_data.append(
                {
                    "unit_id": "TITLE",
                    "unit_type": "title",
                    "text": draft.title,
                    "support_ids": t_sups,
                    "support_packets": support_packets(t_sups),
                    "issues": title_issues,
                }
            )

        # 2. Lead
        if "LEAD" in issues_by_unit:
            lead_issues = issues_by_unit["LEAD"]
            missing_lead = any(getattr(issue, "code", "") == "EMPTY_LEAD" for issue in lead_issues)
            if missing_lead:
                # Do not mine the whole context for a lead: it may only use
                # visible current-window evidence already cited in the body.
                lead_sups = current_body_repair_supports()
            else:
                lead_sups = unit_supports(
                    list(draft.lead_support_ids),
                    [sid for claim in draft.lead_claims for sid in claim.cited_support_ids],
                    lead_issues,
                )
            unit_data.append(
                {
                    "unit_id": "LEAD",
                    "unit_type": "lead",
                    "text": draft.lead,
                    "support_ids": lead_sups,
                    "support_packets": support_packets(lead_sups),
                    "issues": lead_issues,
                }
            )

        # 3. Sections (Headings and Paragraphs)
        p_idx = 1
        for s_idx, sec in enumerate(draft.sections, start=1):
            h_id = f"H{s_idx:03d}"
            if h_id in issues_by_unit:
                h_sups = section_heading_supports(sec, issues_by_unit[h_id])
                h_prompt_sups = heading_supports_for_prompt(sec, h_sups, issues_by_unit[h_id])
                visible_h_sups = [sid for sid in h_sups if editor_visible_support(sid)]
                section_paragraphs, omitted_paragraphs, truncated_paragraph = (
                    heading_section_context(sec)
                )
                unit_data.append(
                    {
                        "unit_id": h_id,
                        "unit_type": "heading",
                        "text": sanitize_writer_source_text(sec.heading),
                        "reader_context": {
                            "article_title": sanitize_writer_source_text(draft.title),
                            "other_section_headings": tuple(
                                sanitize_writer_source_text(other_section.heading)
                                for other_index, other_section in enumerate(draft.sections, start=1)
                                if other_index != s_idx
                            ),
                            "section_paragraphs": section_paragraphs,
                            "section_paragraphs_omitted": omitted_paragraphs,
                            "section_paragraph_truncated": truncated_paragraph,
                        },
                        "support_ids": h_sups,
                        "support_packets": support_packets(h_prompt_sups),
                        "supports_omitted": max(0, len(visible_h_sups) - len(h_prompt_sups)),
                        "issues": issues_by_unit[h_id],
                    }
                )

            for paragraph_index, p in enumerate(sec.paragraphs):
                p_id = f"P{p_idx:03d}"
                if p_id in issues_by_unit:
                    raw_sups = unit_supports(
                        list(p.cited_support_ids),
                        [sid for claim in p.claims for sid in claim.cited_support_ids],
                        issues_by_unit[p_id],
                    )
                    p_sups = [sid for sid in raw_sups if support_text(sid)]
                    if not p_sups:
                        # An uncited paragraph may describe evidence cited
                        # nowhere in its section. Offer eligible supports
                        # lexically anchored in its own text first; the patch
                        # is still re-grounded against this exact allowlist.
                        p_sups = [
                            sid
                            for sid in _reground_support_ids(
                                p.text, context, minimum_shared_stems=3
                            )
                            if support_text(sid)
                        ][:_MAX_EDITOR_SUPPORTS]
                    if not p_sups:
                        sec_sups: list[str] = []
                        for other_p in sec.paragraphs:
                            sec_sups.extend(other_p.cited_support_ids)
                            sec_sups.extend(
                                cl_sid for cl in other_p.claims for cl_sid in cl.cited_support_ids
                            )
                        sec_sups.extend(sec.heading_support_ids)
                        sec_sups.extend(
                            cl_sid for cl in sec.heading_claims for cl_sid in cl.cited_support_ids
                        )
                        sec_visible_sups = [
                            sid for sid in dict.fromkeys(sec_sups) if support_text(sid)
                        ]
                        p_sups = (
                            sec_visible_sups[:_MAX_EDITOR_SUPPORTS]
                            if sec_visible_sups
                            else current_body_repair_supports()
                        )
                    p_packets = support_packets(p_sups)
                    valid_packet_ids = {pkt["support_id"] for pkt in p_packets}
                    p_sups = [sid for sid in p_sups if sid in valid_packet_ids]
                    reader_context = {
                        "section_heading": sanitize_writer_source_text(sec.heading),
                        "previous_paragraph": (
                            sanitize_writer_source_text(sec.paragraphs[paragraph_index - 1].text)
                            if paragraph_index > 0
                            else ""
                        ),
                        "next_paragraph": (
                            sanitize_writer_source_text(sec.paragraphs[paragraph_index + 1].text)
                            if paragraph_index + 1 < len(sec.paragraphs)
                            else ""
                        ),
                    }
                    unit_data.append(
                        {
                            "unit_id": p_id,
                            "unit_type": "paragraph",
                            "text": p.text,
                            "support_ids": p_sups,
                            "support_packets": p_packets,
                            "issues": issues_by_unit[p_id],
                            "reader_context": reader_context,
                        }
                    )
                p_idx += 1

        return unit_data

    @staticmethod
    def _repair_priority(unit: Mapping[str, Any]) -> int:
        """Order factual/safety repairs, composition repairs, then cosmetics."""
        issues = unit.get("issues", ())
        if any(
            not isinstance(issue, ArticleReaderQualityFinding)
            or issue.code == "CHAT_KITCHEN_LEAK"
            or issue.severity == "blocking"
            for issue in issues
        ):
            return 0
        cosmetic_classes = {
            "name_typography",
            "sentence_completeness",
            "quote_count_heuristic",
            "paragraph_rhythm",
        }
        if any(
            article_quality_policy(issue.code).finding_class not in cosmetic_classes
            for issue in issues
            if isinstance(issue, ArticleReaderQualityFinding)
        ):
            return 1
        return 2

    @staticmethod
    def _bound_prompt_supports(
        unit_contexts: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Keep a unit only when every mandatory evidence packet fits whole.

        Character limits apply independently to each text unit. Nothing is
        truncated or silently dropped from its citable support set.
        """
        bounded_units: list[dict[str, Any]] = []
        omitted_units: list[dict[str, Any]] = []
        for unit in unit_contexts:
            required_ids = tuple(dict.fromkeys(unit.get("support_ids", ())))
            packets_by_id = {
                packet["support_id"]: packet
                for packet in unit.get("support_packets", ())
                if packet.get("support_id") and packet.get("text")
            }
            missing_ids = tuple(sid for sid in required_ids if sid not in packets_by_id)
            if missing_ids or (
                not required_ids
                and not packets_by_id
                and any(getattr(iss, "blocking", False) for iss in unit.get("issues", ()))
            ):
                omitted_units.append(
                    {
                        **unit,
                        "selection_status": "missing_required_support",
                        "selection_reason": "required_support_missing",
                        "required_support_count": len(required_ids),
                        "shown_support_count": 0,
                    }
                )
                continue

            ordered_ids = tuple(sid for sid in required_ids if sid in packets_by_id) or tuple(
                packets_by_id
            )
            if len(ordered_ids) > _MAX_EDITOR_SUPPORTS:
                ordered_ids = ordered_ids[:_MAX_EDITOR_SUPPORTS]
            selected = [packets_by_id[sid] for sid in ordered_ids if sid in packets_by_id]
            rendered = [f"[{packet['support_id']}] {packet['text']}" for packet in selected]
            if any(len(text) > _MAX_EDITOR_SUPPORT_PACKET_CHARS for text in rendered):
                omitted_units.append(
                    {
                        **unit,
                        "selection_status": "deferred_budget",
                        "selection_reason": "support_packet_exceeded",
                        "required_support_count": len(required_ids),
                        "shown_support_count": 0,
                    }
                )
                continue
            if sum(map(len, rendered)) > _MAX_EDITOR_SUPPORT_CONTEXT_CHARS:
                while (
                    selected
                    and sum(len(f"[{p['support_id']}] {p['text']}") for p in selected)
                    > _MAX_EDITOR_SUPPORT_CONTEXT_CHARS
                ):
                    selected.pop()
                rendered = [f"[{packet['support_id']}] {packet['text']}" for packet in selected]
                if not selected:
                    omitted_units.append(
                        {
                            **unit,
                            "selection_status": "deferred_budget",
                            "selection_reason": "support_budget_exceeded",
                            "required_support_count": len(required_ids),
                            "shown_support_count": 0,
                        }
                    )
                    continue

            shown_ids = tuple(packet["support_id"] for packet in selected)
            copied = dict(unit)
            copied["prompt_support_ids"] = shown_ids
            copied["prompt_supports"] = rendered
            copied["supports"] = rendered
            copied["support_packets_omitted"] = 0
            copied["finding_supports_omitted"] = 0
            copied["required_support_count"] = len(required_ids)
            copied["shown_support_count"] = len(shown_ids)
            reader_context = dict(copied.get("reader_context") or {})
            if reader_context.get("section_paragraphs"):
                reader_context["section_paragraphs"] = tuple(
                    (
                        paragraph_index,
                        paragraph_text,
                        tuple(sid for sid in support_ids if sid in shown_ids),
                    )
                    for paragraph_index, paragraph_text, support_ids in reader_context[
                        "section_paragraphs"
                    ]
                )
                copied["reader_context"] = reader_context
            bounded_units.append(copied)
        return bounded_units, omitted_units

    @staticmethod
    def _patch_signature(
        text: str,
        *,
        original_text: str = "",
        support_ids: tuple[str, ...] | list[str] = (),
    ) -> str:
        normalized_text = " ".join(_normalize_homoglyphs(text or "").casefold().split())
        normalized_original = " ".join(
            _normalize_homoglyphs(original_text or "").casefold().split()
        )
        return json.dumps(
            [normalized_text, normalized_original, tuple(support_ids)],
            ensure_ascii=False,
        )

    @staticmethod
    def _compact_unit_outcomes(
        outcomes: Mapping[str, Mapping[str, str]],
    ) -> dict[str, dict[str, str]]:
        """Keep raw attempted prose out of persistent attempt metadata."""
        return {
            unit_id: {
                "status": outcome.get("status", "unknown"),
                "reason": outcome.get("reason", ""),
            }
            for unit_id, outcome in outcomes.items()
        }

    @staticmethod
    def _save_attempt_debug_artifact(
        callback: Callable[[str, Any], None] | None,
        prefix: str,
        attempt: int,
        prompt: str,
        response: str | None,
        draft: StructuredArticleDraft,
        unit_outcomes: Mapping[str, Mapping[str, str]],
        validation_result: ArticleValidationResult,
        quality_report: ArticleReaderQualityReport,
    ) -> None:
        if callback is None:
            return
        payload = {
            "attempt": attempt,
            "prompt": prompt,
            "response": response,
            "patched_draft": draft.render_markdown(),
            "unit_outcomes": {unit_id: dict(value) for unit_id, value in unit_outcomes.items()},
            "validation_findings": [
                {
                    "code": issue.code,
                    "unit_id": issue.unit_id,
                    "message": issue.message,
                    "blocking": issue.blocking,
                }
                for issue in validation_result.issues
            ],
            "quality_findings": [
                {
                    "code": finding.code,
                    "unit_id": finding.unit_id,
                    "message": finding.message,
                    "severity": finding.severity,
                }
                for finding in quality_report.repair_findings
            ],
        }
        try:
            callback(f"{prefix}_pass_{attempt}", payload)
        except Exception as exc:
            # Diagnostics are opt-in and must not change publication behavior.
            logger.warning("ArticleEditor diagnostic artifact save failed: %s", type(exc).__name__)

    @staticmethod
    def _localize_article_quality_finding(
        draft: StructuredArticleDraft,
        finding: ArticleReaderQualityFinding,
    ) -> tuple[ArticleReaderQualityFinding, ...]:
        """Map selected article-wide findings to the text units named by evidence.

        These findings are calculated over section topology and therefore carry
        ``unit_id=ARTICLE``.  Their support IDs still identify the paragraphs
        (or sections) that need a bounded copy edit.  Localizing them here lets
        the normal patch, re-grounding, and Evidence Boundary checks handle the
        edit without accepting unsupported prose or deleting the underlying
        Story.
        """
        policy = article_quality_policy(finding.code)
        if finding.unit_id not in ("ARTICLE", "DRAFT", ""):
            return (finding,)
        if policy.repair_scope != "support_units" or not finding.support_ids:
            return ()

        targeted_support_ids = set(finding.support_ids)
        localized: list[ArticleReaderQualityFinding] = []
        paragraph_index = 1
        for section_index, section in enumerate(draft.sections, start=1):
            section_matches: list[str] = []
            for paragraph in section.paragraphs:
                unit_support_ids = tuple(
                    dict.fromkeys(
                        (
                            *paragraph.cited_support_ids,
                            *(
                                support_id
                                for claim in paragraph.claims
                                for support_id in claim.cited_support_ids
                            ),
                        )
                    )
                )
                matching = tuple(
                    support_id
                    for support_id in unit_support_ids
                    if support_id in targeted_support_ids
                )
                if matching:
                    if finding.code in {
                        "DIRECTORY_TIMETABLE_SECTION",
                        "MULTI_SENTENCE_ADDRESS_STATUS_ROSTER",
                        "REPEATED_CENTRAL_THESIS",
                        "THEME_MISMATCHED_SECTION",
                        "UNCLASSIFIED_STORY_IN_CONNECTIVITY_SECTION",
                    }:
                        localized.append(
                            ArticleReaderQualityFinding(
                                code=finding.code,
                                unit_id=f"P{paragraph_index:03d}",
                                message=finding.message,
                                support_ids=matching,
                                severity=finding.severity,
                            )
                        )
                    else:
                        section_matches.extend(matching)
                paragraph_index += 1

            if section_matches:
                # Other support-scoped findings can target the heading. The affected
                # source supports are supplied to the ordinary heading editor,
                # whose result must still re-ground against those exact sources.
                localized.append(
                    ArticleReaderQualityFinding(
                        code=finding.code,
                        unit_id=f"H{section_index:03d}",
                        message=finding.message,
                        support_ids=tuple(dict.fromkeys(section_matches)),
                        severity=finding.severity,
                    )
                )

        return tuple(localized)

    def _build_system_prompt(self) -> str:
        return (
            "Вы — главный выпускающий редактор (Senior Fact-Checking Copy Editor) новостной редакции.\n"
            "Ваша задача — ТОЧЕЧНО отредактировать несколько фрагментов статьи, к которым у службы проверки фактов возникли строгие замечания.\n\n"
            "ПРАВИЛА РЕДАКТИРОВАНИЯ:\n"
            "1. ПРЯМАЯ РЕЧЬ И КАВЫЧКИ (UNSUPPORTED_DIRECT_QUOTE):\n"
            "   - Кавычки вокруг реплики или целой фразы допустимы только для дословной цитаты из источника. Кавычки вокруг подтверждённого названия компании, провайдера или бренда — типографское оформление имени, а не прямая речь; сохраняйте их.\n"
            "   - Никогда не меняйте, не исправляйте и не сокращайте слова внутри уже существующей прямой цитаты. Чтобы сжать или объединить реплики, уберите кавычки и передайте смысл косвенной речью; каждую оставленную цитату сверяйте пословно с источником.\n"
            "   - Если не подтверждена именно прямая речь, передайте её естественной косвенной речью через «что» со строчной буквы (например: «житель сообщил, что...»). Не снимайте типографские кавычки с подтверждённого названия внутри этой фразы.\n"
            "   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО оставлять двоеточие перед текстом без кавычек (например: «житель признался: Звук генераторов...» — это грубая грамматическая ошибка).\n\n"
            "2. ИМЕНА СОБСТВЕННЫЕ И ТОПОНИМЫ (UNSUPPORTED_PROPER_NAME / UNSUPPORTED_LOCATION):\n"
            "   - Если имя, аббревиатура, название стороннего города или организации выдуманы и отсутствуют в подтверждениях ниже — удалите их.\n"
            "   - СОХРАНЕНИЕ ПОДТВЕРЖДЕННЫХ ТОПОНИМОВ И ОРИЕНТИРОВ (AGENTS.md 0.4): Если название района, улицы, ориентира присутствует в источниках ниже — ОБЯЗАТЕЛЬНО СОХРАНЯЙТЕ его! КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО заменять подтвержденные топонимы абстрактными клише вроде «в одном из районов города» или «в неназванном месте».\n"
            "   - Если слово с заглавной буквы не в начале предложения отмечено как неподтвержденное, но сам объект есть в источниках, переведите его в строчные буквы (например, «военный городок»).\n\n"
            "3. КОНКРЕТНЫЕ ФАКТЫ И ЧИСЛА (UNSUPPORTED_CONCRETE_CLAIM):\n"
            "   - Если цифра, цена, процент или дата выдуманы и отсутствуют в источниках — удалите неподтвержденную цифру.\n"
            "   - Если же конкретная деталь (цена, скидка, время, напряжение вольт, этаж, возраст детей) ЕСТЬ в источниках ниже — ОБЯЗАТЕЛЬНО СОХРАНЯЙТЕ её! Запрещено выхолащивать подтвержденные факты в общие фразы.\n\n"
            "4. ПРИЧИННО-СЛЕДСТВЕННЫЕ СВЯЗИ (UNSUPPORTED_CAUSAL_RELATION):\n"
            "   - Запрещено утверждать причинно-следственные связи («из-за аварии», «вследствие чего», «по причине», «связано с тем, что»), если механизм прямо не подтвержден. Замените на нейтральное связывание фактов («в этот же период...», «наряду с этим...», «также в городе...»).\n\n"
            "5. КРИТИЧЕСКИЕ ТЕМЫ И ДОМЕНЫ (UNSUPPORTED_CRITICAL_TERM):\n"
            "   - Если валидатор указывает неподтвержденные критические концепции (например, «топливо», «горючее», «заправки», «бензин»), ВЫ ДОЛЖНЫ ПОЛНОСТЬЮ УДАЛИТЬ эти понятия и предложения из текста фрагмента, переписав его строго по подтвержденным фактам.\n\n"
            "6. НЕДОСТАТОЧНАЯ ПОДДЕРЖКА (UNSUPPORTED_CLAIM_ATOM):\n"
            "   - Замечание указывает, что в конкретном предложении есть утверждение или лексика, недостаточно подтвержденная источниками.\n"
            "   - ВАЖНО: ВЫПОЛНИТЕ ТОЧЕЧНУЮ КОРРЕКТИРОВКУ! Исправьте или перефразируйте именно проблемное утверждение, удалив неподтвержденные слова и домыслы.\n"
            "   - НЕ дублируйте факты, уже изложенные в соседних абзацах. Не добавляйте лишних обобщающих предложений. Опирайтесь только на факты, относящиеся к этому конкретному фрагменту.\n"
            "   - Категорически запрещено удалять весь абзац ([DELETE]), если в нем есть подтвержденная информация.\n\n"
            "7. ОТСУТСТВИЕ ИСТОЧНИКОВ (MISSING_SUPPORT):\n"
            '   - Только если к фрагменту вообще нет никаких подтверждающих фактов в источниках — верните пустую строку "" или "[DELETE]", чтобы удалить этот неподтвержденный фрагмент.\n\n'
            "8. МЕХАНИКА ИСТОЧНИКОВ И АВТОРСКАЯ РАЗГОВОРНАЯ РЕЧЬ:\n"
            "   - Для замечания CHAT_KITCHEN_LEAK уберите только прямое раскрытие технического источника («в чате», «в канале», «в комментариях») и сохраните сообщение с естественной подтверждённой атрибуцией.\n"
            "   - Для COLLOQUIAL_AUTHOR_PROSE перепишите только разговорные слова автора как спокойную косвенную речь, сохранив реальный факт и атрибуцию.\n"
            "   - Не редактируйте точные прямые цитаты: разговорные слова в цитате остаются неизменными. Не делайте из обычного разговорного слова утечку источника и не добавляйте статус, причину или географию.\n\n"
            "9. ОБЪЕМ, СТИЛЬ И СОХРАННОСТЬ:\n"
            "   - Сохраняйте естественный журналистский стиль и связность с остальным текстом статьи.\n"
            "   - Не добавляйте никаких новых фактов или деталей, которых нет в предоставленных подтверждениях.\n"
            "   - Проверяйте русскую грамматику, управление, согласование и пунктуацию в каждом редактируемом фрагменте. Исправляйте неестественные формулировки, сохраняя все подтверждённые факты, временные различия, географию и атрибуцию.\n"
            "   - Обычно исправляйте только проблемную фразу. ИСКЛЮЧЕНИЕ: для QUOTE_ROLL_PARAGRAPH, OVERLOADED_ROSTER_PARAGRAPH и MULTI_SENTENCE_ADDRESS_STATUS_ROSTER следуйте специальной инструкции замечания и при необходимости перепишите весь целевой абзац; общее правило малой правки на эти три типа замечаний не распространяется.\n"
            "   - Отредактируйте ТОЛЬКО запрошенные фрагменты.\n\n"
            "10. ПОВТОРЫ И ЗАЦИКЛИВАНИЕ (REPEATED_CONTENT_LOOP):\n"
            "   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО повторять одно и то же или почти идентичное предложение несколько раз подряд. Если абзац зациклился — оставьте мысль ровно один раз в грамотной формулировке и удалите повторы.\n\n"
            "11. СООТВЕТСТВИЕ ПОДТЕМ В ЗАГОЛОВКАХ РАЗДЕЛОВ (PHANTOM_HEADING_TOPIC):\n"
            "   - Если заголовок раздела содержит конкретное перечисление подтем после двоеточия (например: «Тема: подтема А, подтема Б и подтема В»), сначала сверьте ВСЕ перечисленные подтемы с показанными ниже абзацами всего этого раздела и их подтверждениями. Если абзацы раскрывают тему другими словами, сохраните её. Если тема действительно отсутствует, удалите только её из заголовка или замените заголовок более точным. Не удаляйте тему только из-за иной формулировки в тексте и не добавляйте в абзацы новые факты. Если контекст раздела явно помечен как сокращённый, не считайте невидимую часть доказательством отсутствия темы.\n\n"
            "Структурные operations допустимы только при явно предоставленном структурном режиме. "
            "Читайте полный текст только для ориентации и не расширяйте список разрешённых единиц.\n"
            "ФОРМАТ ОТВЕТА (строго валидный JSON; operations и base_fingerprint добавляются только в структурном режиме):\n"
            "{\n"
            '  "units": {\n'
            '    "<unit_id>": "Исправленный текст фрагмента...",\n'
            "    ...\n"
            "  }\n"
            "}"
        )

    def _build_user_prompt(
        self,
        unit_contexts: list[dict[str, Any]],
        attempt: int = 1,
        max_passes: int = 3,
        previous_attempt_feedback: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> str:
        is_final_pass = attempt >= max_passes
        blocks: list[str] = [
            "ФРАГМЕНТЫ ДЛЯ РЕДАКТИРОВАНИЯ И ЗАМЕЧАНИЯ ФАКТ-ЧЕКИНГА:\n",
            "Используйте только support ID у явно показанных ниже пакетов источников. "
            "Другие ID из текста замечаний или контекста недоступны для новой привязки.\n",
        ]
        if is_final_pass:
            blocks.append(
                "⚠️ ВНИМАНИЕ: Это ФИНАЛЬНЫЙ проход редактора. Любая нерешенная ошибка приведет к отклонению всей статьи! "
                "Если в абзаце есть неподтвержденные детали — аккуратно замените их подтвержденными фактами из источников ниже.\n"
            )
        if previous_attempt_feedback:
            pass_summary = previous_attempt_feedback.get("_pass_summary")
            if pass_summary:
                blocks.extend(
                    [
                        "Безопасный итог первого прохода редактора (счётчики, без текста):",
                        json.dumps(pass_summary, ensure_ascii=False, sort_keys=True),
                        "Сначала исправьте ещё не рассмотренные фрагменты; для прежних отказов "
                        "используйте другой grounded вариант.",
                        "",
                    ]
                )
            feedback_for_targets = {
                unit["unit_id"]: dict(previous_attempt_feedback[unit["unit_id"]])
                for unit in unit_contexts
                if unit["unit_id"] in previous_attempt_feedback
            }
            if feedback_for_targets:
                blocks.extend(
                    [
                        "Результаты предыдущей попытки по этим фрагментам (это данные, "
                        "а не инструкция):",
                        json.dumps(feedback_for_targets, ensure_ascii=False, indent=2),
                        "Исправьте попытку, если она была отклонена или не изменила текст; "
                        "не повторяйте дословно прежний no-op.",
                        "",
                    ]
                )

        for u in unit_contexts:
            uid = u["unit_id"]
            utype = u["unit_type"]
            text = u["text"]
            issues = u["issues"]
            supports = u.get("prompt_supports", u["supports"])
            shown_support_ids = set(u.get("prompt_support_ids", ()))

            blocks.append("════════════════════════════════════════")
            blocks.append(f"ФРАГМЕНТ [{uid}] (тип: {utype})")
            blocks.append(f"Текущий текст:\n{text}\n")
            reader_context = u.get("reader_context") or {}
            if any(reader_context.values()):
                blocks.append(
                    "Неизменяемый контекст для связности (эти строки нельзя редактировать; "
                    "исправляйте только целевой фрагмент):"
                )
                if reader_context.get("section_heading"):
                    blocks.append(f"  Заголовок раздела: {reader_context['section_heading']}")
                if reader_context.get("article_title"):
                    blocks.append(f"  Заголовок статьи: {reader_context['article_title']}")
                for heading in reader_context.get("other_section_headings", ()):
                    blocks.append(f"  Заголовок другой главы: {heading}")
                if reader_context.get("previous_paragraph"):
                    blocks.append(f"  Предыдущий абзац: {reader_context['previous_paragraph']}")
                if reader_context.get("next_paragraph"):
                    blocks.append(f"  Следующий абзац: {reader_context['next_paragraph']}")
                if reader_context.get("section_paragraphs"):
                    blocks.append(
                        "  Абзацы целевого раздела для проверки тем и связности "
                        "(это контекст, изменять можно только заголовок):"
                    )
                    for paragraph_index, paragraph_text, support_ids in reader_context[
                        "section_paragraphs"
                    ]:
                        citation_label = ", ".join(support_ids) if support_ids else "нет ID"
                        blocks.append(
                            f"    Абзац {paragraph_index} [support IDs: {citation_label}]: "
                            f"{paragraph_text}"
                        )
                    if reader_context.get("section_paragraphs_omitted") or reader_context.get(
                        "section_paragraph_truncated"
                    ):
                        omitted = reader_context.get("section_paragraphs_omitted", 0)
                        truncated = reader_context.get("section_paragraph_truncated", False)
                        blocks.append(
                            "    Контекст раздела сокращён: "
                            f"текст полностью не показан в {omitted} абзацах; "
                            f"текст сокращён в {truncated} абзацах; "
                            f"лимиты — {_MAX_HEADING_SECTION_PARAGRAPH_CHARS} символов на абзац "
                            f"и {_MAX_HEADING_SECTION_CONTEXT_CHARS} символов на раздел. "
                            "Не считайте тему отсутствующей только потому, что она могла попасть "
                            "в не показанную часть."
                        )
            blocks.append("Замечания валидатора:")
            if utype == "title":
                if any(getattr(issue, "code", "") == "EMPTY_TITLE" for issue in issues):
                    blocks.append(
                        "  ⚠️ ВНИМАНИЕ ДЛЯ ЗАГОЛОВКА (TITLE): заголовок отсутствует. "
                        "Сформулируйте только короткий заголовок по показанным пакетам PUBLISH. "
                        "Не добавляйте город, масштаб, тему, дату или деталь, которых нет в этих "
                        "пакетах. Если безопасный заголовок по ним невозможен, не придумывайте "
                        "факты."
                    )
                elif any(
                    getattr(issue, "code", "") == "HISTORICAL_CONTEXT_UNFRAMED" for issue in issues
                ):
                    blocks.append(
                        "  ⚠️ ВНИМАНИЕ ДЛЯ ЗАГОЛОВКА (TITLE): Заголовок ОБЯЗАН быть в статье "
                        "(ЗАПРЕЩЕНО возвращать [DELETE]!). Перепишите его только по сегодняшним "
                        "подтверждениям ниже; не используйте общую формулировку, если она не "
                        "подкреплена этими материалами. Не добавляйте неподтверждённые даты, "
                        "цифры, названия или городской масштаб."
                    )
                else:
                    blocks.append(
                        "  ⚠️ ВНИМАНИЕ ДЛЯ ЗАГОЛОВКА (TITLE): Заголовок ОБЯЗАН быть в статье (ЗАПРЕЩЕНО возвращать [DELETE]!). "
                        "Удалите любые конкретные цифры, даты, проценты и неподтвержденные названия. "
                        "Напишите общий заголовок о ситуации в городе (например: «Ситуация со светом и городские будни Бердянска»)."
                    )
            elif utype == "lead":
                if any(getattr(issue, "code", "") == "EMPTY_LEAD" for issue in issues):
                    blocks.append(
                        "  ⚠️ ВНИМАНИЕ ДЛЯ ЛИДА (LEAD): лид отсутствует. Напишите короткий "
                        "вводный абзац только по показанным пакетам PUBLISH и не добавляйте "
                        "события, причин, времени, географии, городского масштаба или связей, "
                        "которых в них нет. Каждый фактический фрагмент нового текста должен "
                        "подтверждаться этими пакетами; не заполняйте лид общими фразами."
                    )
                else:
                    blocks.append(
                        "  ⚠️ ВНИМАНИЕ ДЛЯ ЛИДА (LEAD): Вводный абзац ОБЯЗАН быть в статье (ЗАПРЕЩЕНО возвращать [DELETE] или пустую строку!). "
                        "Напишите емкий вводный абзац (2-3 предложения), обобщающий общую картину дня строго по предоставленным источникам ниже."
                    )
            for iss in issues:
                is_quality = isinstance(iss, ArticleReaderQualityFinding)
                msg = f"  • [{'READER_QUALITY' if is_quality else 'FACTUAL'}:{iss.code}] {iss.message}"
                if is_quality:
                    if iss.code == "MISSING_DETAIL_SUPPORT":
                        detail_message = iss.message.replace(
                            "сохраните её, если она помогает читателю понять повседневные последствия.",
                            "обязательно включите её в целевой абзац.",
                        )
                        msg = "  • [READER_QUALITY:MISSING_DETAIL_SUPPORT] " + detail_message
                    msg += f" (severity={iss.severity})"
                    visible_issue_support_ids = [
                        support_id
                        for support_id in iss.support_ids
                        if support_id in shown_support_ids
                    ]
                    if visible_issue_support_ids:
                        msg += (
                            " -> Сохраните подтверждённые детали и опирайтесь именно на support IDs: "
                            + ", ".join(visible_issue_support_ids)
                        )
                    msg += self._quality_repair_instruction(iss.code)
                    blocks.append(msg)
                    continue
                if iss.code == "UNSUPPORTED_CLAIM_ATOM":
                    if utype in ("title", "lead"):
                        msg += " -> ВАЖНО: перепишите предложение строго по фактам из источников ниже, сохраняя связность!"
                    else:
                        msg += " -> ВАЖНО: перепишите текст фрагмента строго по фактам из источников ниже, удалив любые неподтвержденные домыслы или детали. Сохраняйте абзац, НЕ удаляйте его через [DELETE]!"
                elif iss.code == "MISSING_CLAIM_SUPPORT":
                    msg += (
                        " -> Для этого отдельного утверждения нет собственного подтверждения. "
                        "Удалите его либо перепишите только по источникам ниже; не переносите "
                        "на него подтверждение соседнего факта и не выводите цель или назначение "
                        "техники по одному звуку или световому следу."
                    )
                elif iss.code.startswith("MISSING_SUPPORT"):
                    if utype in ("title", "lead"):
                        msg += " -> ВАЖНО: перепишите текст строго по фактам из источников ниже, не удаляя фрагмент!"
                    elif supports:
                        msg += " -> ВАЖНО: перепишите текст фрагмента строго по фактам из источников ниже, не удаляя абзац!"
                    else:
                        msg += ' -> КРИТИЧЕСКИ ВАЖНО: У этого фрагмента НЕТ подтверждающих фактов в источниках. Верните "" или "[DELETE]", чтобы полностью удалить его!'
                elif iss.code == "UNSUPPORTED_CONCRETE_CLAIM":
                    msg += " -> ВАЖНО: полностью удалите указанную неподтвержденную цифру/деталь/срок из текста, либо удалите предложение с ней!"
                elif iss.code in ("UNSUPPORTED_PROPER_NAME", "UNSUPPORTED_CRITICAL_TERM"):
                    msg += (
                        " -> ВАЖНО: полностью удалите указанное слово/термин из текста фрагмента!"
                    )
                elif iss.code == "CHAT_KITCHEN_LEAK":
                    msg += " -> ВАЖНО: полностью удалите слова чатовой кухни («перекличка», «в чате» и т.п.) либо разговорный сленг и перепишите фразу через нормальный литературный язык и городскую атрибуцию («по сообщениям жителей», «ситуация обратная» и т.п.)!"
                elif iss.code == "REPEATED_CONTENT_LOOP":
                    msg += " -> ВАЖНО: удалите дублирующиеся одинаковые предложения, оставив мысль ровно один раз!"
                elif iss.code == "INVALID_SUPPORT_POLICY":
                    msg += " -> ВАЖНО: заголовок раздела или лид обязан опираться на подтвержденные факты с публикацией (PUBLISH) текущего дня. Сформулируйте заголовок раздела строго по фактам из абзацев этого раздела!"
                elif iss.code == "QUESTION_CONTEXT_OVERCLAIM":
                    msg += " -> ВАЖНО: запрещено утверждать вопрос жителей из чата как установленный факт. Используйте вопросительную или исследовательскую формулировку («Что известно о...», «Вопросы жителей о...») либо перепишите по реальным подтвержденным фактам!"
                elif iss.code == "HISTORICAL_CONTEXT_UNFRAMED":
                    if utype == "title":
                        msg += (
                            " -> ВАЖНО: замените заголовок формулировкой только о сегодняшнем "
                            "материале. Для этой правки ниже приведены текущие подтверждения; "
                            "исторические основания намеренно не разрешены для нового заголовка. "
                            "Не переносите в него состояние из прошлых дней и не обобщайте его "
                            "на весь город. Используйте только темы и детали, которые прямо "
                            "подтверждены источниками ниже."
                        )
                    else:
                        msg += " -> ВАЖНО: если событие длится уже несколько дней или произошло ранее, обязательно добавьте маркер продолжения («по-прежнему», «продолжаются», «сохраняются») либо сфокусируйте формулировку строго на событиях сегодняшнего дня!"
                elif iss.code == "PHANTOM_HEADING_TOPIC":
                    msg += " -> ВАЖНО: скорректируйте заголовок раздела, удалив из перечисления после двоеточия темы, которые фактически не освещены в тексте абзацев!"
                elif iss.code == "LEAKED_META_OMISSION":
                    msg += " -> ВАЖНО: полностью удалите любые мета-комментарии об опущенных контактах/телефонах!"
                blocks.append(msg)

            if supports:
                blocks.append("\nПодтверждающие факты (источники):")
                for s_text in supports:
                    blocks.append(f"  - {s_text}")
                omitted_supports = u.get("support_packets_omitted", 0) + u.get(
                    "supports_omitted", 0
                )
                if omitted_supports:
                    blocks.append(
                        "  Часть допустимых пакетов источников не показана из-за бюджета: "
                        f"{omitted_supports}; лимиты — {_MAX_EDITOR_SUPPORTS} пакетов, "
                        f"{_MAX_EDITOR_SUPPORT_PACKET_CHARS} символов на пакет и "
                        f"{_MAX_EDITOR_SUPPORT_CONTEXT_CHARS} символов суммарно. "
                        "Используйте только перечисленные выше ID."
                    )
                if u.get("finding_supports_omitted"):
                    blocks.append(
                        "  ВАЖНО: часть ID из целевого замечания не имеет показанного пакета "
                        f"источника ({u['finding_supports_omitted']}); не ссылайтесь на них и "
                        "не восстанавливайте по ним детали."
                    )

                # Check topical overlap using stemming
                from src.publication.article_claims import _stem

                tok_re = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
                text_stems = {_stem(w.lower()) for w in tok_re.findall(text) if len(w) >= 3}
                support_stems = set()
                for s_text in supports:
                    support_stems |= {
                        _stem(w.lower()) for w in tok_re.findall(s_text) if len(w) >= 3
                    }
                if text_stems and support_stems and not (text_stems & support_stems):
                    if utype == "heading":
                        blocks.append(
                            "\n⚠️ Заголовок слабо совпадает по словам с подтверждениями раздела. "
                            "При редактуре заголовка ориентируйтесь на реальные подтемы в абзацах "
                            "выше; не переписывайте и не удаляйте абзацы."
                        )
                    else:
                        blocks.append(
                            "\n⚠️ ВНИМАНИЕ: Текущий текст недостаточно согласован с источниками. "
                            "Перепишите этот абзац заново (2–3 предложения), опираясь строго на факты из источников ниже. "
                            "НЕ удаляйте абзац!"
                        )
            else:
                blocks.append(
                    '\n(Подтверждающих фактов в источниках нет — верните "" или "[DELETE]", чтобы удалить фрагмент)'
                )
                if u.get("finding_supports_omitted"):
                    blocks.append(
                        "  ВАЖНО: часть ID из целевого замечания не имеет показанного пакета "
                        f"источника ({u['finding_supports_omitted']}); не ссылайтесь на них и "
                        "не восстанавливайте по ним детали."
                    )
            blocks.append("")

        blocks.append(
            "Верните валидный JSON вида:\n"
            "{\n"
            '  "units": {\n'
            '    "P007": "исправленный текст абзаца...",\n'
            '    "LEAD": "исправленный текст лида..."\n'
            "  }\n"
            "}"
        )
        return "\n".join(blocks)

    @staticmethod
    def _quality_repair_instruction(code: str) -> str:
        policy = article_quality_policy(code)
        instructions = {
            "OVERLOADED_ROSTER_PARAGRAPH": (
                " -> Перепишите весь целевой абзац, опираясь на все относящиеся к нему подтверждения "
                "ниже. Не перечисляйте в одном предложении четыре или более разных места с "
                "состояниями услуги, если источники прямо не подтверждают для них общий статус и "
                "период, содержательный контраст между состояниями, последовательность изменений "
                "или общее условие. Если "
                "такой связи в источниках нет, разнесите сообщения по отдельным фактическим "
                "предложениям с конкретным местом и подтверждённым состоянием; не заменяйте их "
                "расплывчатым общим перечнем. Сохраните все подтверждённые места, состояния, сроки, "
                "исключения и атрибуцию. Не придумывайте район, близость мест, общий контраст, "
                "хронологию, причину или общее условие и не переносите состояние между адресами."
            ),
            "MULTI_SENTENCE_ADDRESS_STATUS_ROSTER": (
                " -> Проверьте целевой абзац: если однотипные предложения действительно можно "
                "связать подтверждённым локальным контрастом или последовательностью, соберите их "
                "в естественный текст. Сохраните каждое место и состояние; не выводите близость, "
                "общую причину, хронологию или связь без подтверждения. Если это лишь полезный "
                "перечень независимых наблюдений и связи нет, оставьте его без изменений."
            ),
            "CROSS_SECTION_REPETITION": (
                " -> Оставьте повторяющееся утверждение в части, где оно лучше всего подтверждено; "
                "в этом целевом фрагменте удалите повтор или сохраните только новое поддержанное "
                "состояние, время либо последствие. Соседние части статьи не редактируйте."
            ),
            "REPEATED_CENTRAL_THESIS": (
                " -> В этом целевом абзаце уберите только повтор центральной мысли, если он не "
                "добавляет подтверждённое состояние, период или последствие. Сохраните новые "
                "поддержанные подробности и не меняйте остальные части статьи."
            ),
            "DUPLICATE_ARTICLE_HEADING": (
                " -> Локально уточните только этот заголовок по подтверждённому содержанию раздела, "
                "чтобы он отличался от заголовка статьи и других глав."
            ),
            "UNDEVELOPED_LEAD_PROMISE": (
                " -> Локально исправьте лид: уберите обещание, которое основной текст не раскрывает, "
                "или сформулируйте его только в пределах подтверждённого материала. Не добавляйте "
                "новые утверждения и не переписывайте тело статьи."
            ),
            "ARTICLE_INVENTORY_RHYTHM": (
                " -> Свяжите соседние короткие сюжеты естественным переходом и сохраните их "
                "конкретные детали; не превращайте абзац в перечень и не добавляйте факты."
            ),
            "QUOTE_ROLL_PARAGRAPH": (
                " -> Перепишите весь целевой абзац: после правки в нём должно остаться не более двух "
                "дословных прямых цитат, каждая точно подтверждена текстом источников ниже. "
                "Не меняйте, не исправляйте и не сокращайте слова внутри сохранённой прямой цитаты; "
                "если реплику нужно сжать, передайте её косвенной речью без кавычек. Если "
                "исходных цитат больше двух, оставьте не более двух и передайте остальные сообщения "
                "естественной косвенной речью с подтверждённой атрибуцией. Кавычки вокруг "
                "подтверждённых названий компаний и провайдеров — типографское оформление имён, "
                "а не цитаты; сохраняйте такие названия в кавычках, и они не входят в лимит двух "
                "цитат. Сохраните все подтверждённые факты и детали, не меняя их смысл или степень "
                "определённости."
            ),
            "CONSECUTIVE_DIRECT_SPEECH_ROLL": (
                " -> Уберите перечисление прямых реплик: синтезируйте сообщения плавной косвенной "
                "речью с географией, временными различиями и естественной атрибуцией. Сохраните "
                "не более одной подходящей прямой цитаты, только если её слова дословно подтверждены "
                "источником. Не меняйте ни одного слова внутри сохранённой цитаты; остальные реплики "
                "перескажите без кавычек. Кавычки вокруг подтверждённых названий организаций, "
                "провайдеров и мест — оформление имён, а не прямая речь; сохраняйте такие названия. "
                "Не добавляйте факты, причины, географию или связь между сообщениями, которой нет "
                "в подтверждениях."
            ),
            "ARTICLE_PLACE_AREA_MISMATCH": (
                " -> Перепишите этот абзац так, чтобы каждый ориентир и каждая улица относились "
                "только к своей подтверждённой географии. Если подтверждения не показывают, что "
                "место и названный район относятся к одной географической области, изложите их как "
                "разные наблюдения в отдельных полных предложениях; не распространяйте описание "
                "района на другой ориентир или улицу. Независимое сообщение при необходимости можно "
                "начать словами «В отдельном сообщении говорилось...». Не добавляйте название района, "
                "близость мест, общий статус, причинную или временную связь, если этого нет в "
                "подтверждениях. Сохраните все подтверждённые факты и конкретные детали каждого "
                "сообщения."
            ),
            "PRIVATE_SECTOR_AREA_UNSPECIFIED": (
                " -> Сохраните сообщение и его срок, но прямо скажите, что источник не уточнил район. "
                "Не присваивайте сообщению район из соседнего сюжета и не удаляйте сам факт."
            ),
            "ARTICLE_AREA_BEFORE_STREET_ORDER": (
                " -> Начните с общего подтверждённого района, затем изложите наблюдения по его "
                "улицам. Если состояния менялись в течение дня, покажите их хронологически; не "
                "создавайте впечатление, что улица находится в другом районе."
            ),
            "UNQUOTED_COMMERCIAL_PROVIDER_NAME": (
                " -> Заключите каждое подтверждённое название провайдера в русские типографские "
                "кавычки, например «Миранда», «Юпитер», «+7Телеком». Это оформление названия, а не "
                "прямая цитата. Сверьте точную форму с источниками ниже; не расширяйте короткое "
                "обозначение до полного бренда, если источники этого не подтверждают. Одновременно "
                "проверьте русскую грамматику и пунктуацию абзаца."
            ),
            "INCOMPLETE_QUANTITY_PHRASE": (
                " -> Исправьте незавершённый количественный оборот. Используйте существительное "
                "(например, «заявок» или «домов») только если понятно из источников ниже, что "
                "именно считают. Если единица счёта не подтверждается, удалите только оборот с "
                "неполным числом, сохранив остальные поддержанные сведения."
            ),
            "CONTRADICTORY_SERVICE_STATE": (
                " -> Передайте подтверждённое локальное различие для одной услуги, места и времени "
                "как явный контраст. Не обобщайте состояние на весь город, не выводите причину и "
                "не переносите состояние между адресами."
            ),
            "DIRECTORY_TIMETABLE_SECTION": (
                " -> Сократите каталог адресов и обычных часов работы в этом абзаце. Сохраните "
                "подтверждённое изменение, сбой или конкретную практическую деталь, если они есть; "
                "не удаляйте полезный сюжет, не превращайте расписание в новость без основания и "
                "не добавляйте отсутствующие в источниках факты."
            ),
            "THEME_MISMATCHED_SECTION": (
                " -> Исправьте тематическое размещение указанного абзаца. При доступном "
                "структурном режиме перенесите его без изменений либо разделите смешанные темы "
                "по разрешённой операции, сохраняя факты, детали и атрибуцию. Не создавайте "
                "связь между независимыми сообщениями. Если структура недоступна, измените только "
                "целевой текст по его подтверждениям и сохраните естественное место сюжета."
            ),
            "UNCLASSIFIED_STORY_IN_CONNECTIVITY_SECTION": (
                " -> Исправьте тематическое размещение целевого абзаца только в разрешённых "
                "границах структурного режима; неизвестная тема не разрешает домыслы. Сохраните сюжет "
                "и не приписывайте ему тему, связь или причинную связь, которой нет в источниках."
            ),
            "MISSING_DETAIL_SUPPORT": (
                " -> ОБЯЗАТЕЛЬНО включите в этот целевой абзац конкретную деталь, указанную "
                "замечанием и подтверждённую источником ниже. Формулировка «если деталь помогает» "
                "не означает, что её можно опустить: добавьте её компактно, естественно и по теме, "
                "перестроив при необходимости предложение. Не добавляйте иных деталей, не меняйте "
                "смысл или степень уверенности источника и сохраните остальной подтверждённый текст."
            ),
            "MISSING_DEVELOP_STORY": (
                " -> Только если этот существующий абзац находится в запланированной для сюжета "
                "главе, естественно добавьте недостающую ключевую линию по указанным support IDs. "
                "Не превращайте абзац в перечень, не переписывайте несвязанный сюжет, не добавляйте "
                "факты и не создавайте filler. Если сюжет нельзя органично встроить в этот абзац, "
                "сохраните его текущую формулировку. Любые прямые цитаты оставляйте дословно; "
                "сжатие выполняйте косвенной речью."
            ),
            "COLLOQUIAL_AUTHOR_PROSE": (
                " -> Перепишите только авторскую разговорную формулировку в спокойную косвенную "
                "речь, сохранив конкретное сообщение, его смысл и атрибуцию. Не заменяйте факт "
                "общим клише, не добавляйте причину, район, время или статус услуги и не меняйте "
                "ни одного слова внутри точной прямой цитаты."
            ),
        }
        instruction = instructions.get(code)
        if instruction is None and policy.repair_scope in {"unit", "support_units", "story_unit"}:
            raise ArticleQualityPolicyError(
                f"Reader-quality repair policy has no editor instruction: {code!r}"
            )
        return instruction or ""

    @staticmethod
    def _operation_metadata(
        operation: ArticleStructuralOperation, attempt: int, status: str, reason: str
    ) -> dict[str, Any]:
        return {
            "attempt": attempt,
            "operation_id": operation.operation_id,
            "kind": operation.kind,
            "source_unit_ids": list(operation.source_unit_ids),
            "destination_section_id": operation.destination_section_id
            or (f"new:{operation.operation_id}" if operation.kind == "create_section" else None),
            "destination_before_unit_id": operation.destination_before_unit_id,
            "status": status,
            "reason": reason,
        }

    @staticmethod
    def _response_object(response: str) -> dict[str, Any]:
        cleaned = (response or "").strip()
        match = _JSON_BLOCK_RE.search(cleaned)
        if match:
            cleaned = match.group(1)
        try:
            value = json.loads(cleaned)
        except (ValueError, TypeError):
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start < 0 or end <= start:
                return {}
            try:
                value = json.loads(cleaned[start : end + 1])
            except (ValueError, TypeError):
                return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _parse_structural_operations(cls, response: str) -> tuple[ArticleStructuralOperation, ...]:
        raw = cls._response_object(response).get("operations", [])
        if not isinstance(raw, list):
            raise ValueError("invalid_operation_payload")
        operations: list[ArticleStructuralOperation] = []
        fields = set(ArticleStructuralOperation.__dataclass_fields__)
        for item in raw:
            if not isinstance(item, dict) or set(item) - fields:
                raise ValueError("invalid_operation_payload")
            operation_id = item.get("operation_id")
            kind = item.get("kind")
            sources = item.get("source_unit_ids")
            texts = item.get("paragraph_texts", [])
            if (
                not isinstance(operation_id, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", operation_id) is None
                or not isinstance(kind, str)
                or kind not in {"move", "recompose", "create_section"}
                or not isinstance(sources, list)
                or not sources
                or not all(
                    isinstance(value, str) and re.fullmatch(r"P[0-9]{3,}", value)
                    for value in sources
                )
                or not isinstance(texts, list)
                or not all(isinstance(value, str) and value.strip() for value in texts)
                or any(
                    item.get(key) is not None and not isinstance(item[key], str)
                    for key in ("destination_section_id", "destination_before_unit_id", "heading")
                )
            ):
                raise ValueError("invalid_operation_payload")
            if (
                item.get("destination_section_id") is not None
                and re.fullmatch(r"H[0-9]{3,}", item["destination_section_id"]) is None
            ) or (
                item.get("destination_before_unit_id") is not None
                and re.fullmatch(r"P[0-9]{3,}", item["destination_before_unit_id"]) is None
            ):
                raise ValueError("invalid_operation_payload")
            operations.append(
                ArticleStructuralOperation(
                    operation_id=operation_id,
                    kind=cast(Literal["move", "recompose", "create_section"], kind),
                    source_unit_ids=tuple(sources),
                    destination_section_id=item.get("destination_section_id"),
                    destination_before_unit_id=item.get("destination_before_unit_id"),
                    heading=item.get("heading"),
                    paragraph_texts=tuple(texts),
                )
            )
        return tuple(operations)

    @staticmethod
    def _build_pass_registry(
        draft: StructuredArticleDraft,
        quality: ArticleReaderQualityReport,
        context: ArticleEditorialContext,
        material_projection: ArticleMaterialProjection | None,
    ) -> _ArticlePassRegistry:
        from src.publication.article_quality import (
            _ARTICLE_SECTION_THEME_PATTERNS,
            _article_themes,
        )

        sections: dict[str, ArticleSection] = {}
        paragraphs: dict[str, ArticleParagraph] = {}
        paragraph_sections: dict[str, str] = {}
        index = 1
        for section_index, section in enumerate(draft.sections, 1):
            section_id = f"H{section_index:03d}"
            sections[section_id] = section
            for paragraph in section.paragraphs:
                unit_id = f"P{index:03d}"
                paragraphs[unit_id] = paragraph
                paragraph_sections[unit_id] = section_id
                index += 1
        targeted: set[str] = set()
        for finding in quality.repair_findings:
            if finding.code not in _STRUCTURAL_FINDING_CODES:
                continue
            for unit_id, paragraph in paragraphs.items():
                support_match = set(_paragraph_support_ids(paragraph)).intersection(
                    finding.support_ids
                )
                if finding.unit_id == unit_id or (
                    support_match
                    and finding.unit_id in {"ARTICLE", "DRAFT", "", paragraph_sections[unit_id]}
                ):
                    targeted.add(unit_id)
        source_supports: dict[str, tuple[str, ...]] = {}
        destinations: dict[str, frozenset[str]] = {}
        new_section_sources: set[str] = set()
        heading_themes = {
            sid: _article_themes(section.heading, _ARTICLE_SECTION_THEME_PATTERNS)
            for sid, section in sections.items()
        }
        for unit_id in targeted:
            # Quality findings are built from citable evidence. Keep this
            # structural allowlist on the same evidence boundary so one stale,
            # contextual, or projection-suppressed citation cannot disable
            # structural edits for every otherwise repairable paragraph.
            support_ids = _citable_support_ids(
                _paragraph_support_ids(paragraphs[unit_id]),
                context,
                material_projection,
            )
            support_ids = tuple(
                support_id
                for support_id in support_ids
                if (support := context.support_by_id.get(support_id)) is not None
                and (
                    sanitize_writer_source_text(support.text)
                    or sanitize_writer_source_text(support.source_text)
                )
            )
            if not support_ids:
                continue
            source_supports[unit_id] = support_ids
            themes: set[str] = set()
            for support_id in support_ids:
                themes.update(
                    _projected_support_themes(
                        support_id,
                        context,
                        material_projection,
                    )
                )
            compatible = {sid for sid, heading in heading_themes.items() if themes & heading}
            # Keeping the source section permits a first-pass split; a fresh
            # second-pass registry can then move the isolated misplaced unit.
            destinations[unit_id] = frozenset(compatible | {paragraph_sections[unit_id]})
            represented = set().union(*heading_themes.values()) if heading_themes else set()
            if themes - represented:
                new_section_sources.add(unit_id)
        return _ArticlePassRegistry(
            _draft_fingerprint(draft),
            MappingProxyType(sections),
            MappingProxyType(paragraphs),
            MappingProxyType(paragraph_sections),
            MappingProxyType(source_supports),
            MappingProxyType(destinations),
            frozenset(new_section_sources),
        )

    def _structural_prompt_context(
        self,
        draft: StructuredArticleDraft,
        registry: _ArticlePassRegistry,
        context: ArticleEditorialContext,
        user_prompt: str,
        system_prompt: str,
    ) -> tuple[str | None, str]:
        from src.publication.article_context import article_support_theme_hints
        from src.publication.article_writer_context import ARTICLE_WRITER_CONTEXT_MAX_CHARS

        if not registry.source_support_ids:
            return None, "no_authorized_structural_sources"
        packets: list[dict[str, Any]] = []
        support_chars = 0
        required = tuple(
            dict.fromkeys(sid for ids in registry.source_support_ids.values() for sid in ids)
        )
        if len(required) > _MAX_EDITOR_SUPPORTS:
            return None, "required_support_budget_exceeded"
        for support_id in required:
            support = context.support_by_id.get(support_id)
            if support is None or support.publication_use != "PUBLISH":
                return None, "required_support_unavailable"
            text = sanitize_writer_source_text(support.text)
            source = sanitize_writer_source_text(support.source_text)
            if not (text or source):
                return None, "required_support_unavailable"
            packet = {
                "support_id": support_id,
                "story_id": support.story_id,
                "evidence_kind": support.evidence_kind,
                "source_roles": list(support.source_roles),
                "framing": _support_framing(support),
                "temporal_role": support.temporal_role,
                "observed_at": support.observed_at.isoformat() if support.observed_at else None,
                "effective_from": support.effective_from.isoformat()
                if support.effective_from
                else None,
                "effective_until": support.effective_until.isoformat()
                if support.effective_until
                else None,
                "theme_hints": article_support_theme_hints(support),
                "fact": text,
                "primary_source": source if source and source != text else None,
                "reply_parent_context": sanitize_writer_source_text(
                    support.reply_parent_context_text
                )
                or None,
                "provenance": {
                    "source_refs": list(support.source_refs),
                    "fragment_ids": list(support.fragment_ids),
                    "source_item_ids": list(support.source_item_ids),
                    "reply_parent_item_id": support.reply_parent_item_id,
                },
            }
            size = len(json.dumps(packet, ensure_ascii=False))
            if size > _MAX_EDITOR_SUPPORT_PACKET_CHARS:
                return None, "required_support_packet_exceeded"
            support_chars += size
            if support_chars > _MAX_EDITOR_SUPPORT_CONTEXT_CHARS:
                return None, "required_support_budget_exceeded"
            packets.append(packet)
        article = {
            "TITLE": draft.title,
            "LEAD": draft.lead,
            "sections": [
                {
                    "section_id": sid,
                    "heading": section.heading,
                    "paragraphs": [
                        {"unit_id": pid, "text": paragraph.text}
                        for pid, paragraph in registry.paragraphs.items()
                        if registry.paragraph_sections[pid] == sid
                    ],
                }
                for sid, section in registry.sections.items()
            ],
        }
        payload = {
            "base_fingerprint": registry.base_fingerprint,
            "complete_read_only_article": article,
            "authorized_sources": {
                pid: {
                    "support_ids": ids,
                    "destination_section_ids": sorted(registry.destinations[pid]),
                    "create_section_allowed": pid in registry.new_section_sources,
                    "reason": "targeted_thematic_finding",
                }
                for pid, ids in registry.source_support_ids.items()
            },
            "required_support_packets": packets,
        }
        block = (
            "\nСТРУКТУРНЫЙ РЕЖИМ: полный текст ниже дан только для ориентации. "
            "Менять разрешено только authorized_sources. Верните base_fingerprint точно и "
            "operations: список объектов с operation_id, kind (move/recompose/create_section), "
            "source_unit_ids, destination_section_id, destination_before_unit_id, heading, "
            "paragraph_texts. MOVE: один исходный абзац, без нового текста/заголовка. "
            "RECOMPOSE: один или несколько разрешённых абзацев, один существующий раздел, "
            "один или несколько новых абзацев. Для разделения смешанного абзаца можно сначала "
            "пересобрать его в исходной секции; следующий проход отдельно перенесёт часть. "
            "CREATE_SECTION: только явно разрешённый источник и только когда существующей "
            "подходящей главы нет; destination_section_id=null, подтверждённый heading и абзацы. "
            "Новый раздел встанет сразу после последней исходной секции; его ID new:<operation_id>. "
            "Вставляйте перед неизменяемым базовым destination_before_unit_id или в конец (null). "
            "При общей позиции вставки операции применяются в порядке списка operations. "
            "Не ссылайтесь на новые/перемещаемые абзацы как anchors. Не редактируйте один source "
            "дважды и не совмещайте его с units text patch. Не назначайте claims/support IDs. "
            "Сохраняйте слова оставленных прямых цитат дословно, атрибуцию, время, географию "
            "и подтверждённые детали. Тематические hints не доказывают связи и не являются фактами. "
            "Если структура не требует исправления, верните operations=[].\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        # Same configured model as the writer; there is no provider token-window
        # API. This combined character cap is a conservative proxy, including
        # instructions/envelope and four characters per reserved output token.
        if (
            len(system_prompt) + len(user_prompt) + len(block) + 4 * self.max_output_tokens
            > ARTICLE_WRITER_CONTEXT_MAX_CHARS
        ):
            return None, "complete_article_context_budget_exceeded"
        return block, "complete_context_available"

    @staticmethod
    def _identity_map(draft: StructuredArticleDraft) -> dict[str, str]:
        result = {"TITLE": "TITLE", "LEAD": "LEAD"}
        index = 1
        for section_index, section in enumerate(draft.sections, 1):
            result[f"H{section_index:03d}"] = f"H{section_index:03d}"
            for _ in section.paragraphs:
                result[f"P{index:03d}"] = f"P{index:03d}"
                index += 1
        return result

    @staticmethod
    def _mapped_blocking_keys(
        validation: ArticleValidationResult,
        quality: ArticleReaderQualityReport,
        origins: Mapping[str, str],
    ) -> set[tuple[Any, ...]]:
        # Message wording can contain positional IDs; compare factual content
        # and stable origins, rather than a heading/paragraph's shifted number.
        def replace_origin(match: re.Match[str]) -> str:
            return origins.get(match.group(0), match.group(0))

        return {
            (
                "evidence",
                issue.code,
                origins.get(issue.unit_id, issue.unit_id),
                tuple(sorted(issue.support_ids)),
                issue.claim_text,
                repr(issue.unsupported_claims),
            )
            for issue in validation.issues
            if issue.blocking
        } | {
            (
                "quality",
                finding.code,
                origins.get(finding.unit_id, finding.unit_id),
                tuple(sorted(finding.support_ids)),
                re.sub(
                    r"\b[PH][0-9]{3,}\b",
                    replace_origin,
                    finding.message,
                ),
            )
            for finding in quality.blocking_findings
        }

    def apply_structural_operations(
        self,
        draft: StructuredArticleDraft,
        operations: tuple[ArticleStructuralOperation, ...],
        registry: _ArticlePassRegistry,
        context: ArticleEditorialContext,
        *,
        text_patched_unit_ids: frozenset[str] = frozenset(),
    ) -> tuple[StructuredArticleDraft, dict[str, str]]:
        """Resolve the complete batch against the immutable base before assembly.

        This method creates a candidate only; edit_draft assesses it completely
        and owns the atomic acceptance/rollback checkpoint.
        """
        used_sources: set[str] = set()
        operation_ids: set[str] = set()
        insertions: dict[tuple[str, str | None], list[tuple[str, ArticleParagraph]]] = {}
        new_sections: dict[str, ArticleSection] = {}
        new_section_after: dict[str, str] = {}
        # Text patches cannot change topology, so the pass IDs still identify
        # the accepted pre-structure checkpoint, even when its text changed.
        current_paragraphs: dict[str, ArticleParagraph] = {}
        current_sections = {f"H{i:03d}": section for i, section in enumerate(draft.sections, 1)}
        index = 1
        for section in draft.sections:
            for paragraph in section.paragraphs:
                current_paragraphs[f"P{index:03d}"] = paragraph
                index += 1
        if not text_patched_unit_ids and _draft_fingerprint(draft) != registry.base_fingerprint:
            raise ValueError("stale_base_fingerprint")
        if any(
            paragraph != registry.paragraphs.get(pid)
            for pid, paragraph in current_paragraphs.items()
            if pid not in text_patched_unit_ids
        ):
            raise ValueError("stale_base_fingerprint")
        if set(current_paragraphs) != set(registry.paragraphs) or set(current_sections) != set(
            registry.sections
        ):
            raise ValueError("base_topology_changed")
        for operation in operations:
            if operation.operation_id in operation_ids:
                raise ValueError("duplicate_operation_id")
            operation_ids.add(operation.operation_id)
            sources = set(operation.source_unit_ids)
            if len(sources) != len(operation.source_unit_ids) or sources & used_sources:
                raise ValueError("conflicting_source_operations")
            if not sources <= registry.source_support_ids.keys():
                raise ValueError("unauthorized_or_unknown_source")
            if sources & text_patched_unit_ids:
                raise ValueError("conflicting_text_and_structural_edits")
            source_sections = {registry.paragraph_sections[pid] for pid in sources}
            destination = operation.destination_section_id
            if operation.kind == "move":
                if len(sources) != 1 or operation.paragraph_texts or operation.heading is not None:
                    raise ValueError("invalid_move")
            elif not operation.paragraph_texts:
                raise ValueError("missing_recomposed_paragraphs")
            if operation.kind == "create_section":
                if (
                    destination is not None
                    or operation.destination_before_unit_id is not None
                    or not operation.heading
                ):
                    raise ValueError("invalid_new_section")
                if not sources <= registry.new_section_sources:
                    raise ValueError("new_section_not_authorized")
                destination = f"new:{operation.operation_id}"
                source_order = list(registry.sections)
                new_section_after[destination] = max(source_sections, key=source_order.index)
            elif (
                destination not in registry.sections
                or any(destination not in registry.destinations[pid] for pid in sources)
                or operation.heading is not None
            ):
                raise ValueError("unauthorized_or_unknown_destination")
            if (
                source_sections.intersection(text_patched_unit_ids)
                or destination in text_patched_unit_ids
            ):
                raise ValueError("conflicting_heading_and_structural_edits")
            anchor = operation.destination_before_unit_id
            if anchor is not None and (
                anchor not in registry.paragraphs
                or registry.paragraph_sections[anchor] != destination
            ):
                raise ValueError("invalid_insertion_anchor")
            used_sources.update(sources)
        # Anchors must be immutable base paragraphs; references to another
        # operation's source would introduce unresolved/cyclic insertion order.
        if any(
            op.destination_before_unit_id in used_sources | text_patched_unit_ids
            for op in operations
        ):
            raise ValueError("conflicting_or_unresolved_insertion_anchor")
        for operation in operations:
            destination = operation.destination_section_id or f"new:{operation.operation_id}"
            allowed = tuple(
                dict.fromkeys(
                    sid
                    for pid in operation.source_unit_ids
                    for sid in registry.source_support_ids[pid]
                )
            )
            if any(
                sid not in context.support_by_id
                or context.support_by_id[sid].publication_use != "PUBLISH"
                for sid in allowed
            ):
                raise ValueError("required_support_unavailable")
            if operation.kind == "move":
                replacements = [
                    (operation.source_unit_ids[0], current_paragraphs[operation.source_unit_ids[0]])
                ]
            else:
                base_prose = "\n".join(
                    registry.paragraphs[pid].text for pid in operation.source_unit_ids
                )
                output_prose = "\n".join((*operation.paragraph_texts, operation.heading or ""))
                if not _preserves_existing_direct_quotes(base_prose, output_prose):
                    raise ValueError("direct_quote_words_changed")
                replacements = []
                for ordinal, raw_text in enumerate(operation.paragraph_texts, 1):
                    text = _strip_internal_handles(raw_text.strip())
                    supports = _reground_support_ids(text, context, allowed)
                    if not supports:
                        raise ValueError("replacement_not_grounded_in_source_supports")
                    claims = tuple(
                        ArticleClaimAtom(
                            sentence, _reground_support_ids(sentence, context, supports)
                        )
                        for sentence in (_split_sentences_safe(text) or [text])
                    )
                    replacements.append(
                        (
                            f"op:{operation.operation_id}:P{ordinal}",
                            ArticleParagraph(
                                text=text,
                                cited_support_ids=supports,
                                claims=claims,
                                generation_origin="AI",
                            ),
                        )
                    )
                if operation.kind == "create_section":
                    heading = _strip_internal_handles((operation.heading or "").strip())
                    supports = _reground_support_ids(
                        heading, context, allowed, minimum_shared_stems=1
                    )
                    if not supports:
                        raise ValueError("new_heading_not_grounded_in_source_supports")
                    from src.publication.article_quality import (
                        _ARTICLE_SECTION_THEME_PATTERNS,
                        _article_themes,
                        _projected_support_themes,
                    )

                    heading_themes = _article_themes(heading, _ARTICLE_SECTION_THEME_PATTERNS)
                    source_themes = set().union(
                        *(_projected_support_themes(sid, context, None) for sid in allowed)
                    )
                    if not heading_themes.intersection(source_themes) or any(
                        heading_themes.issubset(
                            _article_themes(section.heading, _ARTICLE_SECTION_THEME_PATTERNS)
                        )
                        for section in registry.sections.values()
                    ):
                        raise ValueError("new_section_theme_has_existing_destination")
                    new_sections[destination] = ArticleSection(
                        heading=heading,
                        heading_support_ids=supports,
                        heading_claims=(ArticleClaimAtom(heading, supports),),
                    )
            insertions.setdefault((destination, operation.destination_before_unit_id), []).extend(
                replacements
            )
        ordered: list[tuple[str, ArticleSection]] = []
        for sid, section in current_sections.items():
            ordered.append((sid, section))
            ordered.extend(
                (new_id, new_sections[new_id])
                for new_id, after in new_section_after.items()
                if after == sid
            )
        output_sections: list[ArticleSection] = []
        origins = {"TITLE": "TITLE", "LEAD": "LEAD"}
        emitted: list[str] = []
        index = 1
        for sid, section in ordered:
            units: list[tuple[str, ArticleParagraph]] = []
            for pid, paragraph in current_paragraphs.items():
                if registry.paragraph_sections[pid] != sid:
                    continue
                units.extend(insertions.get((sid, pid), ()))
                if pid not in used_sources:
                    units.append((pid, paragraph))
            units.extend(insertions.get((sid, None), ()))
            if not units:
                # A factual heading is itself supported reader content. Decline
                # the batch when an empty section could lose its unique fact;
                # ordinary thematic labels may be removed deterministically.
                from src.domain.service_taxonomy import detect_service_families
                from src.publication.article_claims import extract_concrete_claims
                from src.publication.article_quality import _ARTICLE_SECTION_THEME_PATTERNS

                surviving_supports = {
                    support_id
                    for pid, paragraph in current_paragraphs.items()
                    if pid not in used_sources
                    for support_id in _paragraph_support_ids(paragraph)
                } | {
                    support_id
                    for units_at_anchor in insertions.values()
                    for _, paragraph in units_at_anchor
                    for support_id in _paragraph_support_ids(paragraph)
                }
                heading_unique_supports = set(section.heading_support_ids) - surviving_supports
                heading_has_fact = bool(extract_concrete_claims(section.heading))
                heading_has_state = bool(detect_service_families(section.heading)) and bool(
                    re.search(
                        r"\b(?:нет|есть|работ\w*|восстанов\w*|отключ\w*|авари\w*|закры\w*|откры\w*|возобнов\w*|прекрат\w*)",
                        section.heading,
                        re.IGNORECASE,
                    )
                )
                heading_remainder = section.heading
                for _, pattern in _ARTICLE_SECTION_THEME_PATTERNS:
                    heading_remainder = pattern.sub(" ", heading_remainder)
                # Delete only a recognized thematic label. Any remaining
                # content word may carry its own assertion or named detail.
                heading_remainder = re.sub(
                    r"\b(?:и|в|на|о|об|с|для|город\w*|местн\w*|коммунальн\w*|"
                    r"повседневн\w*|жизн\w*|ситуаци\w*)\b",
                    " ",
                    heading_remainder,
                    flags=re.IGNORECASE,
                )
                heading_has_other_content = bool(
                    re.search(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]", heading_remainder)
                )
                if (
                    heading_unique_supports
                    or heading_has_fact
                    or heading_has_state
                    or heading_has_other_content
                ):
                    raise ValueError("empty_section_heading_fact_requires_preservation")
                elif sid in registry.sections and any(
                    registry.paragraph_sections[pid] == sid for pid in used_sources
                ):
                    continue
            evidence_ids = tuple(
                dict.fromkeys(
                    (
                        *section.heading_support_ids,
                        *(
                            sid
                            for _, paragraph in units
                            for sid in _paragraph_support_ids(paragraph)
                        ),
                    )
                )
            )
            unchanged_section = (
                sid in registry.sections
                and tuple(paragraph for _, paragraph in units) == section.paragraphs
                and not any(destination == sid for destination, _ in insertions)
                and not any(registry.paragraph_sections[pid] == sid for pid in used_sources)
            )
            output_sections.append(
                section
                if unchanged_section
                else replace(
                    section,
                    paragraphs=tuple(paragraph for _, paragraph in units),
                    cited_evidence_ids=evidence_ids,
                )
            )
            origins[f"H{len(output_sections):03d}"] = sid
            for identity, _ in units:
                origins[f"P{index:03d}"] = identity
                emitted.append(identity)
                index += 1
        expected_base = set(registry.paragraphs) - used_sources
        expected_base.update(op.source_unit_ids[0] for op in operations if op.kind == "move")
        if {pid for pid in emitted if pid in registry.paragraphs} != expected_base or len(
            emitted
        ) != len(set(emitted)):
            raise ValueError("paragraph_identity_integrity_failed")
        candidate = replace(
            draft,
            sections=tuple(output_sections),
            word_count=(
                len(draft.title.split())
                + len(draft.lead.split())
                + sum(
                    len(paragraph.text.split())
                    for section in output_sections
                    for paragraph in section.paragraphs
                )
            ),
            cited_evidence_ids=tuple(
                dict.fromkeys(
                    (
                        *draft.cited_evidence_ids,
                        *draft.title_support_ids,
                        *draft.lead_support_ids,
                        *(sid for section in output_sections for sid in section.cited_evidence_ids),
                    )
                )
            ),
        )
        return candidate, origins

    def _parse_editor_response(self, response: str) -> dict[str, str]:
        """Extract unit_id -> edited_text mapping from model response."""
        cleaned = (response or "").strip()
        m = _JSON_BLOCK_RE.search(cleaned)
        if m:
            cleaned = m.group(1)
        elif cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        try:
            data = json.loads(cleaned)
        except Exception:
            s_idx = cleaned.find("{")
            e_idx = cleaned.rfind("}")
            if s_idx != -1 and e_idx != -1 and e_idx > s_idx:
                try:
                    data = json.loads(cleaned[s_idx : e_idx + 1])
                except Exception:
                    return {}
            else:
                return {}

        raw_units = data.get("units") if isinstance(data, dict) else None
        if not isinstance(raw_units, dict):
            raw_units = data if isinstance(data, dict) else {}

        patches: dict[str, str] = {}
        for k, v in raw_units.items():
            if k in {"operations", "base_fingerprint"}:
                continue
            if isinstance(k, str) and isinstance(v, str):
                patches[k.strip()] = _normalize_homoglyphs(v.strip())
            elif isinstance(k, str) and isinstance(v, dict) and "text" in v:
                patches[k.strip()] = _normalize_homoglyphs(str(v["text"]).strip())
            elif isinstance(k, str) and v is None:
                patches[k.strip()] = ""

        return patches

    @staticmethod
    def _parse_editor_response_details(
        response: str | None,
        requested_unit_ids: set[str],
    ) -> tuple[dict[str, str], str, int]:
        """Parse tolerant editor envelopes and classify safe shape outcomes."""
        cleaned = (response or "").strip()
        if not cleaned:
            return {}, "unparseable_response", 0
        match = _JSON_BLOCK_RE.search(cleaned)
        if match:
            cleaned = match.group(1)
        elif cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        try:
            data = json.loads(cleaned)
        except Exception:
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start < 0 or end <= start:
                return {}, "unparseable_response", 0
            try:
                data = json.loads(cleaned[start : end + 1])
            except Exception:
                return {}, "unparseable_response", 0

        if not isinstance(data, dict):
            return {}, "invalid_response_shape", 0
        if "units" in data:
            raw_units = data.get("units")
            if not isinstance(raw_units, dict):
                return {}, "invalid_response_shape", 0
        else:
            raw_units = {
                key: value
                for key, value in data.items()
                if key not in {"operations", "base_fingerprint"}
            }
            if not raw_units and not isinstance(data.get("operations"), list):
                return {}, "invalid_response_shape", 0

        patches: dict[str, str] = {}
        unknown_count = 0
        malformed_values = 0
        for key, value in raw_units.items():
            if not isinstance(key, str):
                malformed_values += 1
                continue
            unit_id = key.strip()
            if unit_id not in requested_unit_ids:
                unknown_count += 1
                continue
            if isinstance(value, str):
                patches[unit_id] = _normalize_homoglyphs(value.strip())
            elif isinstance(value, dict) and isinstance(value.get("text"), str):
                patches[unit_id] = _normalize_homoglyphs(value["text"].strip())
            elif value is None:
                patches[unit_id] = ""
            else:
                malformed_values += 1

        if unknown_count:
            return patches, "unknown_unit_ids", unknown_count
        if malformed_values:
            return patches, "invalid_response_shape", 0
        return patches, "completed", 0

    def apply_patches(
        self,
        draft: StructuredArticleDraft,
        patches: Mapping[str, str],
        context: ArticleEditorialContext | None = None,
        *,
        preserve_unmatched_supports: bool = False,
        allowed_support_ids_by_unit: Mapping[str, tuple[str, ...]] | None = None,
        patch_outcomes: dict[str, dict[str, str]] | None = None,
    ) -> StructuredArticleDraft:
        """Apply targeted text patches while preserving structure and valid provenance.

        Projected validation contexts intentionally omit suppressed material.  In
        every mode, replacement prose must be strictly re-grounded before the
        patch is accepted. A failed or unavailable re-grounding rejects the
        patch, leaving the original text and its original provenance for final
        validation. The legacy ``preserve_unmatched_supports`` argument remains
        accepted for call compatibility, but cannot authorize citations for new
        prose.
        """
        if not patches:
            return draft

        def record_outcome(
            unit_id: str,
            status: str,
            reason: str,
            attempted_text: str,
            result_text: str = "",
        ) -> None:
            if patch_outcomes is not None:
                patch_outcomes[unit_id] = {
                    "status": status,
                    "reason": reason,
                    "attempted_text": attempted_text,
                    "result_text": result_text,
                }

        def allowed_ids(unit_id: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
            if allowed_support_ids_by_unit is not None:
                return tuple(allowed_support_ids_by_unit.get(unit_id, ()))
            return tuple(fallback)

        title_sups = draft.title_support_ids
        if not title_sups and allowed_support_ids_by_unit is None:
            title_sups = draft.lead_support_ids or (
                draft.sections[0].heading_support_ids if draft.sections else ()
            )

        title = draft.title
        title_claims = draft.title_claims
        if "TITLE" in patches:
            raw_t = patches["TITLE"].strip()
            if raw_t.upper() not in ("", "[DELETE]", "DELETE", "NONE", "NULL", "[УДАЛИТЬ]"):
                candidate_title = _normalize_homoglyphs(_strip_internal_handles(raw_t))
                quotes_preserved = _preserves_existing_direct_quotes(draft.title, candidate_title)
                if not quotes_preserved:
                    record_outcome("TITLE", "rejected", "direct_quote_words_changed", raw_t)
                    candidate_title = ""
                regrounded_title_sups = (
                    _reground_support_ids(
                        candidate_title, context, allowed_ids("TITLE", tuple(title_sups))
                    )
                    if candidate_title and context is not None
                    else ()
                )
                if regrounded_title_sups:
                    title = candidate_title
                    title_sups = regrounded_title_sups
                    title_claims = (ArticleClaimAtom(text=title, cited_support_ids=title_sups),)
                    changed = title != draft.title or title_sups != draft.title_support_ids
                    record_outcome(
                        "TITLE",
                        "applied" if changed else "no_op",
                        "text_or_supports_changed" if changed else "text_unchanged",
                        raw_t,
                        title,
                    )
                elif quotes_preserved:
                    record_outcome(
                        "TITLE", "rejected", "replacement_not_grounded_in_visible_supports", raw_t
                    )
            else:
                record_outcome("TITLE", "rejected", "title_deletion_not_allowed", raw_t)

        lead = draft.lead
        lead_claims = draft.lead_claims
        lead_sups = draft.lead_support_ids
        if "LEAD" in patches:
            raw_l = patches["LEAD"]
            candidate_lead = _normalize_homoglyphs(_strip_internal_handles(raw_l))
            quotes_preserved = _preserves_existing_direct_quotes(draft.lead, candidate_lead)
            if not quotes_preserved:
                record_outcome("LEAD", "rejected", "direct_quote_words_changed", raw_l)
                candidate_lead = ""
            regrounded_lead_sups = (
                _reground_support_ids(
                    candidate_lead,
                    context,
                    allowed_ids("LEAD", tuple(draft.lead_support_ids)),
                )
                if candidate_lead and context is not None
                else ()
            )
            if regrounded_lead_sups:
                lead = candidate_lead
                lead_sups = regrounded_lead_sups
                lead_sentences = _split_sentences_safe(lead)
                lead_claims = tuple(
                    ArticleClaimAtom(text=s, cited_support_ids=lead_sups)
                    for s in (lead_sentences or [lead])
                )
                changed = lead != draft.lead or lead_sups != draft.lead_support_ids
                record_outcome(
                    "LEAD",
                    "applied" if changed else "no_op",
                    "text_or_supports_changed" if changed else "text_unchanged",
                    raw_l,
                    lead,
                )
            elif quotes_preserved:
                record_outcome(
                    "LEAD", "rejected", "replacement_not_grounded_in_visible_supports", raw_l
                )

        p_idx = 1
        new_sections: list[ArticleSection] = []
        for s_idx, sec in enumerate(draft.sections, start=1):
            h_id = f"H{s_idx:03d}"
            heading = sec.heading
            heading_claims = sec.heading_claims
            h_sups = sec.heading_support_ids
            if h_id in patches:
                candidate_heading = _normalize_homoglyphs(_strip_internal_handles(patches[h_id]))
                quotes_preserved = _preserves_existing_direct_quotes(sec.heading, candidate_heading)
                if not quotes_preserved:
                    record_outcome(
                        h_id,
                        "rejected",
                        "direct_quote_words_changed",
                        patches[h_id],
                    )
                    candidate_heading = ""
                regrounded_heading_sups = (
                    _reground_support_ids(
                        candidate_heading,
                        context,
                        allowed_ids(h_id, tuple(sec.heading_support_ids)),
                        minimum_shared_stems=1,
                    )
                    if candidate_heading and context is not None
                    else ()
                )
                if regrounded_heading_sups:
                    heading = candidate_heading
                    h_sups = regrounded_heading_sups
                    heading_claims = (ArticleClaimAtom(text=heading, cited_support_ids=h_sups),)
                    changed = heading != sec.heading or h_sups != sec.heading_support_ids
                    record_outcome(
                        h_id,
                        "applied" if changed else "no_op",
                        "text_or_supports_changed" if changed else "text_unchanged",
                        patches[h_id],
                        heading,
                    )
                elif quotes_preserved:
                    record_outcome(
                        h_id,
                        "rejected",
                        "replacement_not_grounded_in_visible_supports",
                        patches[h_id],
                    )

            new_paragraphs: list[ArticleParagraph] = []
            for para in sec.paragraphs:
                p_id = f"P{p_idx:03d}"
                if p_id not in patches:
                    # Keep unpatched claim/citation tuples byte-for-byte intact.
                    # Rebuilding paragraph citations from their claims loses
                    # intentional distinctions between unit and claim support.
                    new_paragraphs.append(para)
                    p_idx += 1
                    continue

                text = para.text
                claims = para.claims
                existing_supports = tuple(
                    dict.fromkeys(
                        (
                            *para.cited_support_ids,
                            *(sid for claim in para.claims for sid in claim.cited_support_ids),
                        )
                    )
                )
                raw_patch = patches[p_id].strip()
                if raw_patch.upper() in (
                    "",
                    "[DELETE]",
                    "DELETE",
                    "УДАЛИТЬ",
                    "[УДАЛИТЬ]",
                    "NONE",
                    "NULL",
                ):
                    record_outcome(
                        p_id, "rejected", "whole_paragraph_deletion_not_allowed", raw_patch
                    )
                    new_paragraphs.append(para)
                    p_idx += 1
                    continue

                candidate_text = _normalize_homoglyphs(_strip_internal_handles(raw_patch))
                if not _preserves_existing_direct_quotes(para.text, candidate_text):
                    record_outcome(
                        p_id,
                        "rejected",
                        "direct_quote_words_changed",
                        raw_patch,
                    )
                    new_paragraphs.append(para)
                    p_idx += 1
                    continue
                p_sups = (
                    _reground_support_ids(
                        candidate_text,
                        context,
                        allowed_ids(p_id, existing_supports),
                    )
                    if context is not None
                    else ()
                )
                if not p_sups:
                    # Reject unsupported replacement prose. The original unit
                    # remains intact so final validation still sees its claims.
                    record_outcome(
                        p_id,
                        "rejected",
                        "replacement_not_grounded_in_visible_supports",
                        raw_patch,
                    )
                    new_paragraphs.append(para)
                    p_idx += 1
                    continue

                text = candidate_text
                sentences = _split_sentences_safe(text)
                from src.publication.article_models import _normalize_for_dedup

                seen_sn: set[str] = set()
                deduped_s: list[str] = []
                for sentence in sentences:
                    sn = _normalize_for_dedup(sentence)
                    if sn in seen_sn:
                        continue
                    seen_sn.add(sn)
                    deduped_s.append(sentence)
                if deduped_s and len(deduped_s) < len(sentences):
                    text = " ".join(deduped_s)
                    sentences = deduped_s
                changed = text != para.text or tuple(p_sups) != existing_supports
                record_outcome(
                    p_id,
                    "applied" if changed else "no_op",
                    "text_or_supports_changed" if changed else "text_unchanged",
                    raw_patch,
                    text,
                )
                claims = tuple(
                    ArticleClaimAtom(text=sentence, cited_support_ids=p_sups)
                    for sentence in (sentences or [text])
                )

                new_paragraphs.append(
                    ArticleParagraph(
                        text=text,
                        cited_support_ids=p_sups,
                        claims=claims,
                        generation_origin=para.generation_origin,
                    )
                )
                p_idx += 1

            if not new_paragraphs:
                continue

            sec_sups = sec.heading_support_ids or tuple(
                dict.fromkeys(sid for p in new_paragraphs for sid in p.cited_support_ids)
            )
            new_sections.append(
                ArticleSection(
                    heading=heading,
                    heading_support_ids=sec_sups,
                    heading_claims=heading_claims,
                    paragraphs=tuple(new_paragraphs),
                    cited_evidence_ids=sec.cited_evidence_ids,
                    heading_generation_origin=sec.heading_generation_origin,
                )
            )

        if patch_outcomes is not None and not any(
            outcome.get("status") == "applied" for outcome in patch_outcomes.values()
        ):
            return draft

        calc_words = (
            len(title.split())
            + len(lead.split())
            + sum(len(p.text.split()) for s in new_sections for p in s.paragraphs)
        )
        return StructuredArticleDraft(
            title=title,
            title_support_ids=title_sups,
            lead=lead,
            lead_support_ids=lead_sups,
            sections=tuple(new_sections),
            title_claims=title_claims,
            lead_claims=lead_claims,
            cited_evidence_ids=draft.cited_evidence_ids,
            word_count=calc_words,
            title_generation_origin=draft.title_generation_origin,
            lead_generation_origin=draft.lead_generation_origin,
        )
