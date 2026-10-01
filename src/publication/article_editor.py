"""Targeted editorial copy-editor and fact-checker for structured article drafts."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from time import perf_counter
from typing import Any, Callable, Mapping

from src.ai_providers import AIProvider, capture_provider_attempts
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_coverage import ArticleCoveragePlan
from src.publication.article_coverage_diagnostics import diagnose_article_coverage
from src.publication.article_finalization import (
    ArticleAssessmentCheckpoint,
    ArticleCheckpointObserver,
    article_assessment_input_fingerprint,
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
    _direct_speech_spans,
    diagnose_article_quality,
)
from src.publication.article_quality_policy import (
    ArticleQualityPolicyError,
    article_quality_policy,
)
from src.publication.article_validator import ArticleValidationResult, validate_article_draft
from src.publication.article_writer_context import sanitize_writer_source_text

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_MAX_HEADING_SECTION_PARAGRAPH_CHARS = 1_500
_MAX_HEADING_SECTION_CONTEXT_CHARS = 18_000
_MAX_HEADING_CONTEXT_SUPPORTS = 64
_MAX_EDITOR_SUPPORTS = 64
_MAX_EDITOR_SUPPORT_CONTEXT_CHARS = 32_000
_MAX_EDITOR_SUPPORT_PACKET_CHARS = 4_000


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
    ) -> None:
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = min(max_output_tokens, 32768)
        self.last_attempt_count = 0
        self.last_provider_attempts: list[dict[str, Any]] = []
        self.last_assessment: ArticleAssessmentCheckpoint | None = None
        self.last_patched_unit_ids: tuple[str, ...] = ()
        self.last_quality_report = ArticleReaderQualityReport()

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
        no_op_patch_signatures: dict[str, set[str]] = {}
        previous_attempt_feedback: dict[str, dict[str, Any]] = {}
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
        )
        reusable_assessment = assessment is not None and assessment.matches(
            draft, input_fingerprint
        )
        if reusable_assessment and assessment is not None:
            current_val, current_quality = assessment.validation, assessment.quality
        elif max_attempts > 0:
            current_val = await asyncio.to_thread(
                validate_article_draft,
                draft,
                context,
                config=config,
                length_profile=length_profile,
                material_projection=material_projection,
            )
            if coverage_plan is not None:
                current_quality = await asyncio.to_thread(
                    diagnose_article_quality,
                    draft,
                    coverage_plan,
                    context,
                    material_projection=material_projection,
                    place_resolver=place_resolver,
                )
        self.last_assessment = (
            ArticleAssessmentCheckpoint(
                current_draft,
                current_val,
                current_quality,
                input_fingerprint,
            )
            if reusable_assessment or max_attempts > 0
            else None
        )
        if reusable_assessment:
            self.last_assessment = assessment
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

        for attempt in range(1, max_attempts + 1):
            attempt_started = perf_counter()
            blocking_issues = [
                iss
                for iss in current_val.issues
                if iss.blocking and iss.unit_id not in ("DRAFT", "")
            ]
            quality_issues = [
                localized
                for finding in current_quality.repair_findings
                for localized in self._localize_article_quality_finding(current_draft, finding)
            ]
            if not blocking_issues and not quality_issues:
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

            prompt_data = self._build_unit_contexts(
                current_draft,
                issues_by_unit,
                validation_context,
                material_projection=material_projection,
            )
            prompt_data = self._bound_prompt_supports(prompt_data)
            if not prompt_data:
                logger.warning("ArticleEditor could not build unit context for issues; stopping")
                break

            system_prompt = self._build_system_prompt()
            user_prompt = self._build_user_prompt(
                prompt_data,
                attempt=attempt,
                max_passes=max_attempts,
                previous_attempt_feedback=previous_attempt_feedback,
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
            response: str | None = None
            unit_outcomes: dict[str, dict[str, str]] = {}
            requested_units = {unit["unit_id"] for unit in prompt_data}
            try:
                self.last_attempt_count += 1
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
                            reasoning_effort="none",
                            response_format={"type": "json_object"},
                        )
                    finally:
                        self.last_provider_attempts.append(counts.to_metadata())
                patches = self._parse_editor_response(response)
                patches = {
                    unit_id: value
                    for unit_id, value in patches.items()
                    if unit_id in requested_units
                }
                if not patches:
                    logger.warning("ArticleEditor returned no valid unit patches")
                    unit_outcomes = {
                        unit_id: {
                            "status": "no_op",
                            "reason": "no_valid_patch_returned",
                            "attempted_text": "",
                        }
                        for unit_id in requested_units
                    }
                    previous_attempt_feedback = unit_outcomes
                    if attempt_observer is not None:
                        await attempt_observer.attempt_finished(
                            obs_att_id,
                            "failed",
                            error_kind="empty_patches",
                            metadata={
                                "unit_outcomes": self._compact_unit_outcomes(unit_outcomes),
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

                prompt_units_by_id = {unit["unit_id"]: unit for unit in prompt_data}
                for unit_id, patch_text in tuple(patches.items()):
                    prompt_unit = prompt_units_by_id[unit_id]
                    signature = self._patch_signature(
                        patch_text,
                        original_text=prompt_unit["text"],
                        support_ids=prompt_unit["prompt_support_ids"],
                    )
                    if signature in no_op_patch_signatures.get(unit_id, set()):
                        unit_outcomes[unit_id] = {
                            "status": "no_op",
                            "reason": "repeated_identical_no_op_patch",
                            "attempted_text": patch_text,
                        }
                        del patches[unit_id]

                if not patches:
                    logger.warning(
                        "ArticleEditor pass %d repeated only previously rejected/no-op patches; stopping",
                        attempt,
                    )
                    previous_attempt_feedback = unit_outcomes
                    if attempt_observer is not None:
                        await attempt_observer.attempt_finished(
                            obs_att_id,
                            "failed",
                            error_kind="repeated_no_op_patches",
                            metadata={
                                "unit_outcomes": self._compact_unit_outcomes(unit_outcomes),
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

                def evaluate_editor_draft(
                    draft: StructuredArticleDraft = current_draft,
                    previous_quality: ArticleReaderQualityReport = current_quality,
                ) -> tuple[
                    ArticleValidationResult,
                    ArticleReaderQualityReport,
                    float,
                    float,
                ]:
                    validation_started = perf_counter()
                    validation = validate_article_draft(
                        draft,
                        context,
                        config=config,
                        length_profile=length_profile,
                        material_projection=material_projection,
                    )
                    validation_elapsed = perf_counter() - validation_started
                    quality_elapsed = 0.0
                    quality = previous_quality
                    if coverage_plan is not None:
                        quality_started = perf_counter()
                        quality = diagnose_article_quality(
                            draft,
                            coverage_plan,
                            context,
                            material_projection=material_projection,
                            place_resolver=place_resolver,
                        )
                        quality_elapsed = perf_counter() - quality_started
                    return validation, quality, validation_elapsed, quality_elapsed

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
                    ) = await asyncio.to_thread(evaluate_editor_draft)
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
                        ) = await asyncio.to_thread(
                            evaluate_editor_draft, current_draft, previous_quality
                        )
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

                coverage = (
                    await asyncio.to_thread(
                        diagnose_article_coverage,
                        current_draft,
                        coverage_plan,
                        context=context,
                        excluded_story_ids=(
                            material_projection.suppressed_story_ids
                            if material_projection is not None
                            else ()
                        ),
                    )
                    if coverage_plan is not None
                    else None
                )
                self.last_assessment = ArticleAssessmentCheckpoint(
                    current_draft,
                    current_val,
                    current_quality,
                    input_fingerprint,
                    coverage,
                )
                self.last_quality_report = current_quality
                if checkpoint_observer is not None:
                    checkpoint_observer("editor", current_draft, self.last_assessment)
                made_progress = bool(
                    {key for key in previous_actionable if key[1] in requested_units}
                    - actionable_keys(current_val, current_quality)
                )
                previous_attempt_feedback = {
                    unit_id: dict(outcome) for unit_id, outcome in unit_outcomes.items()
                }
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

                if not actually_changed or not made_progress:
                    logger.info("ArticleEditor stopped after no measurable targeted progress")
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
                raise
            except Exception as exc:
                # Keep text, Evidence Boundary result, and quality report as
                # one transaction. A failed validation/diagnostic must not
                # return a patched draft paired with stale assessment data.
                current_draft = previous_draft
                current_val = previous_val
                current_quality = previous_quality
                self.last_quality_report = current_quality
                self.last_assessment = previous_assessment
                patched_unit_ids = previous_patched_unit_ids
                self.last_patched_unit_ids = tuple(dict.fromkeys(patched_unit_ids))
                for outcome in unit_outcomes.values():
                    if outcome.get("status") == "applied":
                        outcome["status"] = "rejected"
                        outcome["reason"] = "validation_error_rolled_back"
                logger.warning("ArticleEditor pass %d encountered error: %s", attempt, exc)
                if not unit_outcomes:
                    unit_outcomes = {
                        unit_id: {
                            "status": "rejected",
                            "reason": f"editor_pass_error:{type(exc).__name__}",
                            "attempted_text": "",
                        }
                        for unit_id in requested_units
                    }
                previous_attempt_feedback = {
                    unit_id: dict(outcome) for unit_id, outcome in unit_outcomes.items()
                }
                if attempt_observer is not None:
                    await attempt_observer.attempt_finished(
                        obs_att_id,
                        "failed",
                        error_kind=type(exc).__name__,
                        metadata={
                            "unit_outcomes": self._compact_unit_outcomes(unit_outcomes),
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
                return projected
            source = sanitize_writer_source_text((support.source_text or "").strip())
            if source and source != fact:
                return f"{fact}\nПервичный источник: {source}" if fact else source
            return fact or source

        def support_packets(support_ids: list[str] | tuple[str, ...]) -> list[dict[str, str]]:
            packets: list[dict[str, str]] = []
            for support_id in dict.fromkeys(support_ids):
                rendered = support_text(support_id)
                if rendered:
                    packets.append({"support_id": support_id, "text": rendered})
            return packets

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
            ][:_MAX_HEADING_CONTEXT_SUPPORTS]

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
            title_has_unframed_history = any(
                getattr(issue, "code", "") == "HISTORICAL_CONTEXT_UNFRAMED"
                for issue in issues_by_unit["TITLE"]
            )
            if title_has_unframed_history:
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
                    "issues": issues_by_unit["TITLE"],
                }
            )

        # 2. Lead
        if "LEAD" in issues_by_unit:
            lead_sups = unit_supports(
                list(draft.lead_support_ids),
                [sid for claim in draft.lead_claims for sid in claim.cited_support_ids],
                issues_by_unit["LEAD"],
            )
            unit_data.append(
                {
                    "unit_id": "LEAD",
                    "unit_type": "lead",
                    "text": draft.lead,
                    "support_ids": lead_sups,
                    "support_packets": support_packets(lead_sups),
                    "issues": issues_by_unit["LEAD"],
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
                    p_sups = unit_supports(
                        list(p.cited_support_ids),
                        [sid for claim in p.claims for sid in claim.cited_support_ids],
                        issues_by_unit[p_id],
                    )
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
                            "support_packets": support_packets(p_sups),
                            "issues": issues_by_unit[p_id],
                            "reader_context": reader_context,
                        }
                    )
                p_idx += 1

        return unit_data

    @staticmethod
    def _bound_prompt_supports(unit_contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Choose bounded, identifiable evidence packets for each requested unit.

        Finding-specific supports are first, followed by the other support
        packets allowed for the unit. The exact IDs and text returned here are
        the only ones shown to the editor and later allowed for re-grounding.
        """
        bounded_units: list[dict[str, Any]] = []
        for unit in unit_contexts:
            packets_by_id = {
                packet["support_id"]: packet
                for packet in unit.get("support_packets", ())
                if packet.get("support_id") and packet.get("text")
            }
            finding_support_ids = [
                support_id
                for issue in unit.get("issues", ())
                for support_id in (getattr(issue, "support_ids", ()) or ())
            ]
            ordered_ids = list(
                dict.fromkeys(
                    (*finding_support_ids, *unit.get("support_ids", ()), *packets_by_id.keys())
                )
            )
            selected: list[dict[str, str]] = []
            shown_chars = 0
            for support_id in ordered_ids:
                packet = packets_by_id.get(support_id)
                if packet is None or len(selected) >= _MAX_EDITOR_SUPPORTS:
                    continue
                remaining_chars = _MAX_EDITOR_SUPPORT_CONTEXT_CHARS - shown_chars
                packet_chars = len(f"[{support_id}] {packet['text']}")
                if len(packet["text"]) > _MAX_EDITOR_SUPPORT_PACKET_CHARS:
                    continue
                if remaining_chars < packet_chars:
                    break
                text = packet["text"]
                selected.append({"support_id": support_id, "text": text})
                shown_chars += packet_chars

            shown_ids = tuple(packet["support_id"] for packet in selected)
            omitted_finding_supports = sum(
                support_id not in shown_ids for support_id in dict.fromkeys(finding_support_ids)
            )
            omitted_count = max(0, len(packets_by_id) - len(selected))
            copied = dict(unit)
            copied["prompt_support_ids"] = shown_ids
            copied["prompt_supports"] = [
                f"[{packet['support_id']}] {packet['text']}" for packet in selected
            ]
            copied["supports"] = copied["prompt_supports"]
            copied["support_packets_omitted"] = omitted_count
            copied["finding_supports_omitted"] = omitted_finding_supports
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
        return bounded_units

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
                # Theme placement can be repaired without deleting or moving a
                # Story by retitling its containing section.  The affected
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
            "   - СОХРАНЕНИЕ ПОДТВЕРЖДЕННЫХ ТОПОНИМОВ И ОРИЕНТИРОВ (AGENTS.md 0.4): Если название района, улицы, ориентира (например, Лиски, район Химиков, супермаркет «Зеркальный») присутствует в источниках ниже — ОБЯЗАТЕЛЬНО СОХРАНЯЙТЕ его! КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО заменять подтвержденные топонимы абстрактными клише вроде «в одном из районов города» или «в неназванном месте».\n"
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
            "8. ЧАТОВАЯ КУХНЯ И ЖАРГОН (CHAT_KITCHEN_LEAK):\n"
            "   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать слова чатовой кухни («перекличка», «в перекличках», «в чате», «в каналах», «в пабликах», «в группах», «в комментариях» и т.п.).\n"
            "   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать разговорный и сетевой сленг («фигня», «хрень», «херня», «хреново», «нафиг», «пофиг» и т.п.). Даже если в источниках жители выражаются неформально, в тексте статьи переводите их в литературный язык («сохраняются перебои», «трудности», «проблемы»).\n"
            "   - Замените их нейтральным описанием ситуации от сути события или стандартной городской журналистской атрибуцией («по сообщениям жителей», «горожане отмечают», «картина обратная»).\n\n"
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
            "ФОРМАТ ОТВЕТА (строго валидный JSON):\n"
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
                if any(
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
                " -> Исправьте только заголовок главы: назовите её шире или точнее, чтобы он "
                "естественно охватывал приведённые ниже подтверждённые сюжеты. Сохраните все сюжеты "
                "и детали в статье; не маскируйте их удалением или переносом фактов и не объявляйте "
                "их связанными, если источники этого не подтверждают. Формулируйте заголовок по "
                "поддержанным ниже темам."
            ),
            "UNCLASSIFIED_STORY_IN_CONNECTIVITY_SECTION": (
                " -> Исправьте только заголовок главы, убрав неподтверждённую привязку к связи и "
                "назвав подтверждённое ниже содержание нейтрально и конкретно. Не исключайте сюжет "
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
        }
        instruction = instructions.get(code)
        if instruction is None and policy.repair_scope in {"unit", "support_units", "story_unit"}:
            raise ArticleQualityPolicyError(
                f"Reader-quality repair policy has no editor instruction: {code!r}"
            )
        return instruction or ""

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
            if isinstance(k, str) and isinstance(v, str):
                patches[k.strip()] = _normalize_homoglyphs(v.strip())
            elif isinstance(k, str) and isinstance(v, dict) and "text" in v:
                patches[k.strip()] = _normalize_homoglyphs(str(v["text"]).strip())
            elif isinstance(k, str) and v is None:
                patches[k.strip()] = ""

        return patches

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
