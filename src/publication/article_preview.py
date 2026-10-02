"""Read-only replay of an existing frozen article publication run."""

from __future__ import annotations

import datetime as dt
import logging
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from src.article_generator import ArticleGenerator
from src.config_loader import Config
from src.publication.article_finalization import (
    ArticleAssessmentCheckpoint,
    ArticleCheckpointObserver,
    _compact_quality_value,
)
from src.publication.article_models import StructuredArticleDraft
from src.publication.errors import ArticlePublicationRejected
from src.publication.event_editorial_adapter import EventEditorialAdapter
from src.publication.policies import ARTICLE_PUBLICATION_TYPES
from src.publication.repository import PublicationRepository
from src.runtime import get_runtime
from src.timezones import get_timezone, normalize_timezone_name

logger = logging.getLogger(__name__)


ArticlePreviewStatus = Literal["accepted", "rejected", "failed"]


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
        parts = [f"# {self.title}" if self.title else "# Вечерняя статья"]
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

    def __init__(self) -> None:
        self.checkpoints: list[_CapturedCheckpoint] = []
        self.assessment_pair_mismatch_count = 0

    def __call__(
        self,
        stage: str,
        draft: StructuredArticleDraft,
        assessment: ArticleAssessmentCheckpoint | None,
    ) -> None:
        captured_assessment = assessment
        captured_draft = draft
        if assessment is not None:
            if assessment.matches(draft, assessment.input_fingerprint):
                captured_draft = assessment.draft
            else:
                captured_assessment = None
                self.assessment_pair_mismatch_count += 1
        self.checkpoints.append(
            _CapturedCheckpoint(
                stage=stage,
                draft=captured_draft,
                assessment=captured_assessment,
            )
        )

    def diagnostics(self) -> list[dict[str, object]]:
        return [
            {
                "stage": checkpoint.stage,
                "assessment_available": checkpoint.assessment is not None,
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

    def __init__(self) -> None:
        self._next_id = 0
        self._attempts: dict[int, _CapturedAttempt] = {}

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
            writer_metadata=_safe_writer_metadata(metadata),
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
        attempt.final_metadata = _safe_final_metadata(metadata)

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


def _safe_structural_operations(value: Any) -> list[dict[str, Any]]:
    """Allow only operation codes and syntactically valid pass-local IDs."""
    from src.publication.article_editor import ARTICLE_STRUCTURAL_OUTCOME_REASONS

    if not isinstance(value, list):
        return []
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
            isinstance(source, str) and re.fullmatch(r"P[0-9]{3,}", source) for source in sources
        ):
            compact["source_unit_ids"] = sources
        for key, pattern in (
            ("destination_section_id", r"(?:H[0-9]{3,}|new:[A-Za-z0-9_-]{1,64})"),
            ("destination_before_unit_id", r"P[0-9]{3,}"),
        ):
            destination = item.get(key)
            if destination is None or (
                isinstance(destination, str) and re.fullmatch(pattern, destination)
            ):
                compact[key] = destination
        result.append(compact)
    return result


def _safe_writer_metadata(value: Any) -> dict[str, Any]:
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
        "article_writer_context_version": {"event-article-context-v2-evidence-inventory"},
        "article_narrative_prompt_version": {"event-article-narrative-v13"},
        "article_writer_prompt_version": {"v17"},
    }
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

    operations = _safe_structural_operations(value.get("structural_operations"))
    if operations:
        result["structural_operations"] = operations
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


def _safe_final_metadata(value: Any) -> dict[str, Any]:
    """Keep versioned quality and composition diagnostics, never article/source text."""
    if not isinstance(value, dict):
        return {}
    from src.publication.article_finalization import (
        _compact_composition_value,
        _compact_quality_value,
    )

    result: dict[str, Any] = {
        key: value[key]
        for key in (
            "status",
            "winning_kind",
            "writer_status",
            "recovery_mode",
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
        if key in value and isinstance(value[key], (str, int, float, bool, type(None)))
    }
    result.update(_safe_writer_metadata(value))
    for key in (
        "reader_quality",
        "quality_before_edit",
        "quality_after_edit",
        "quality_after_finalization",
    ):
        quality = _compact_quality_value(value.get(key))
        if quality is not None:
            result[key] = quality

    composition = _compact_composition_value(value.get("composition"))
    if composition is not None:
        result["composition"] = composition

    writer = _safe_writer_metadata(value.get("writer_attempt"))
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
    attempts = _MemoryGenerationAttemptObserver()
    checkpoints = _MemoryCheckpointObserver()
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

        # Frozen replay is a dry-run: isolate config from the caller and disable
        # production prompt/draft artifact writes on this generator instance.
        preview_config = deepcopy(config)
        preview_config.settings.article.save_debug_artifacts = False
        generator = ArticleGenerator(config=preview_config, logger=logger)
        checkpoint_observer: ArticleCheckpointObserver = checkpoints
        title, lead, body = await generator.generate_from_frozen_input(
            frozen,
            attempt_observer=attempts,
            checkpoint_observer=checkpoint_observer,
        )

        captured = checkpoints.checkpoints[-1] if checkpoints.checkpoints else None
        candidate = (
            _candidate_from_checkpoint(run_details, captured)
            if captured is not None
            else _candidate_from_returned_text(run_details, title, lead, body)
        )
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
        return ArticleRunPreviewOutcome(
            status="rejected",
            candidate=_candidate_from_checkpoint(
                run_details,
                checkpoints.checkpoints[-1] if checkpoints.checkpoints else None,
            ),
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
        body=draft.render_markdown(),
        checkpoint_stage=checkpoint.stage,
        draft=draft,
        assessment=checkpoint.assessment,
    )


def _candidate_from_returned_text(
    run_details: dict[str, object],
    title: str,
    lead: str,
    body: str,
) -> ArticlePreviewCandidate:
    return ArticlePreviewCandidate(
        run_id=_preview_run_id(run_details),
        publication_type=(
            str(run_details["publication_type"]) if "publication_type" in run_details else None
        ),
        edition_slug=str(run_details["edition_slug"]) if "edition_slug" in run_details else None,
        snapshot_at=_snapshot_from_run_details(run_details),
        title=title,
        lead=lead,
        body=body,
        checkpoint_stage="returned_without_checkpoint",
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
        "validation": {
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
        },
    }
    quality = _compact_quality_value(assessment.quality.to_metadata())
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
        "schema_version": "article-run-preview-v2",
        "status": status,
        **run_details,
        "generation": {
            "attempts": attempts.to_metadata(),
            "checkpoints": checkpoints.diagnostics(),
            "assessment_pair_mismatch_count": checkpoints.assessment_pair_mismatch_count,
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
        if isinstance(error, ArticlePublicationRejected):
            failure["reason"] = error.reason
            failure["error_kind"] = error.error_kind
            safe_metadata = _safe_final_metadata(error.metadata)
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
