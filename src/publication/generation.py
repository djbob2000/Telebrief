"""Publication generation service producing immutable publications and generation attempt history (Plan 4 Task 5)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

import psycopg

from src.article_generator import ArticleGenerator
from src.config_loader import Config
from src.db.uow import DatabaseUnitOfWork
from src.embedding_providers import create_embedding_provider
from src.publication.digest_contracts import DIGEST_PUBLICATION_TYPES
from src.publication.editorial_adapter import (
    DatabaseGenerationAttemptObserver,
    KnowledgeEditorialAdapter,
)
from src.publication.errors import ArticlePublicationRejected
from src.publication.models import Publication
from src.publication.policies import (
    ARTICLE_PUBLICATION_TYPES,
    SUPPORTED_ARTICLE_COVERAGE_PLAN_VERSIONS,
    SUPPORTED_ARTICLE_EDITORIAL_PLAN_VERSIONS,
    SUPPORTED_ARTICLE_RECOVERY_VERSIONS,
    SUPPORTED_ARTICLE_WRITER_VERSIONS,
    UnsupportedFrozenSemanticVersion,
)
from src.publication.repository import PublicationRepository
from src.publication.rubrics import (
    RUBRIC_CLASSIFIER_VERSION,
    DigestRubricClassifier,
)

logger = logging.getLogger(__name__)

_MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def effective_digest_narrative_mode(mode: str | None) -> str:
    """Return the production pipeline for a configured digest mode.

    ``journalistic`` was the original free-form text path. It bypassed the
    immutable narrative plan and its coverage/evidence validation, allowing a
    digest to become one item per Story. Keep the value accepted for config
    compatibility, but route it through the structured single-call pipeline.
    """
    if mode == "journalistic":
        return "single_call"
    return mode or "deterministic"


def compute_digest_allowed_terms(
    all_draft_support_texts: Sequence[str],
    snapshot_at: dt.datetime | None = None,
) -> tuple[str, ...]:
    """Compute allowed context terms (edition tokens + date window terms) for digest validation."""
    edition_tokens: set[str] = {
        tok.lower()
        for st in all_draft_support_texts
        for tok in re.findall(r"[\w-]+", st)
        if len(tok) >= 2
    }
    window_terms: set[str] = set()
    if snapshot_at:
        for offset in range(3):
            cur_d = (snapshot_at - dt.timedelta(days=offset)).date()
            window_terms.add(cur_d.strftime("%d.%m"))
            window_terms.add(str(cur_d.day))
            m_name = _MONTHS_RU[cur_d.month - 1]
            window_terms.add(m_name)
            window_terms.add(f"{cur_d.day} {m_name}")
    return tuple(edition_tokens | window_terms)


def _digest_repair_request(
    checkpoint: tuple[Any, ...],
) -> tuple[list[str], tuple[str, ...], tuple[str, ...]]:
    draft, validation, _, _, audit = checkpoint
    findings = list(validation.violations)
    findings.extend(
        f"DIGEST_QUALITY:{check.code}: {check.message} {check.text}".strip()
        for check in audit.checks
        if str(check.status) == "FAIL"
    )
    findings.extend(
        f"STYLE_OBSERVATION:{warning.code}: block={warning.block_id} "
        f"item_id={_digest_warning_item_id(draft, warning)} "
        f"item_index={warning.item_index}: {warning.message}"
        for warning in audit.prose_audit.warnings
    )
    findings = list(dict.fromkeys(findings))
    if not findings:
        return [], (), ()
    findings.append(
        "EDITORIAL_CONSTRAINT: Preserve every useful PUBLISH community_report as a faithfully "
        "attributed local report. One source is sufficient; do not require official confirmation."
    )
    structural_codes = {"OVERLONG_SYNTHESIS", "FRAGMENTED_SERVICE_REPORTS"}
    recompose_ids = tuple(
        dict.fromkeys(
            warning.block_id
            for warning in audit.prose_audit.warnings
            if warning.code in structural_codes and warning.block_id
        )
    )
    # A hard blocker names its fact or item; that item must stay editable even
    # when style warnings elsewhere narrow the request.
    targets = tuple(
        dict.fromkeys(
            (
                *_digest_violation_item_ids(draft, validation.violations),
                *(
                    item.item_id
                    for warning in audit.prose_audit.warnings
                    if not recompose_ids or warning.code in structural_codes
                    for block in draft.blocks
                    if block.block_id == warning.block_id
                    for index, item in enumerate(block.items)
                    if index == warning.item_index and item.item_id
                ),
            )
        )
    )
    if not targets:
        targets = tuple(
            item.item_id for block in draft.blocks for item in block.items if item.item_id
        )
    return findings, targets, recompose_ids


def _digest_violation_item_ids(draft: Any, violations: Sequence[str]) -> tuple[str, ...]:
    """Return items whose ID or covered fact ID is named by a hard violation."""

    def named(identifier: str) -> bool:
        pattern = rf"(?<![\w-]){re.escape(identifier)}(?![\w-])"
        return any(re.search(pattern, str(violation)) for violation in violations)

    return tuple(
        dict.fromkeys(
            item.item_id
            for block in draft.blocks
            for item in block.items
            if item.item_id
            and (named(item.item_id) or any(named(fact) for fact in item.covered_fact_ids))
        )
    )


def _digest_warning_item_id(draft: Any, warning: Any) -> str:
    if warning.item_index is None:
        return "unknown"
    for block in draft.blocks:
        if block.block_id != warning.block_id:
            continue
        if 0 <= warning.item_index < len(block.items):
            return block.items[warning.item_index].item_id or "unknown"
    return "unknown"


def _digest_warning_item_ids(
    draft: Any,
    warnings: Any,
    *,
    codes: set[str],
    allowed_ids: set[str],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.item_id
            for warning in warnings
            if warning.code in codes and warning.block_id
            for block in draft.blocks
            if block.block_id == warning.block_id
            for index, item in enumerate(block.items)
            if index == warning.item_index and item.item_id and item.item_id in allowed_ids
        )
    )


def _digest_local_issue_keys(checkpoint: tuple[Any, ...]) -> set[tuple[str, str]]:
    draft, _, _, _, audit = checkpoint
    return {
        (warning.code, item.item_id)
        for warning in audit.prose_audit.warnings
        for block in draft.blocks
        if block.block_id == warning.block_id
        for index, item in enumerate(block.items)
        if index == warning.item_index and item.item_id
    }


def _digest_has_only_size_failure(checkpoint: tuple[Any, ...]) -> bool:
    """Return whether a fully covered candidate has only a size-related failure."""
    _, validation, coverage, _, audit = checkpoint
    blocking_failures = audit.blocking_failures
    length_violation_prefixes = ("BODY_TOO_LONG:", "HEADLINE_TOO_LONG:")
    length_violations = tuple(
        violation
        for violation in validation.violations
        if violation.startswith(length_violation_prefixes)
    )
    other_violations = tuple(
        violation
        for violation in validation.violations
        if not violation.startswith(length_violation_prefixes)
    )
    post_limit_failed = any(
        check.code == "TELEGRAM_SINGLE_POST_LIMIT" for check in blocking_failures
    )
    return (
        not other_violations
        and not validation.unsupported_claims
        and coverage.story_coverage >= 1.0
        and coverage.material_fact_coverage >= 1.0
        and (bool(length_violations) or post_limit_failed)
        and all(check.code == "TELEGRAM_SINGLE_POST_LIMIT" for check in blocking_failures)
    )


async def _repair_digest_candidate(
    *,
    checkpoint: tuple[Any, ...],
    plan: Any,
    evidence: Any,
    editor: Any,
    observer: Any,
    evaluate_candidate: Callable[[Any], tuple[Any, ...]],
    model: str | None,
    timeout_seconds: float,
    implementation_versions: Any,
    editor_scope: str = "targeted_items",
    review_without_findings: bool = False,
    deadline_at: float | None = None,
    max_context_chars: int | None = None,
    support_text_by_id: Mapping[str, str] | None = None,
) -> tuple[tuple[Any, ...], bool, int]:
    """Use up to three bounded editor calls, retaining each exact safe assessment.

    Style diagnostics request an edit; they do not establish factual unsafety
    or unreadability. The final call is reserved for remaining style findings or
    a complete, fully covered candidate that fails only a size check.
    A rejected later edit cannot erase a safe earlier result.
    """
    from src.publication.digest_edit_scope import build_digest_block_edit_scope
    from src.publication.digest_editor import (
        DigestEditorContextBudgetError,
        DigestEditorContextMissingError,
        DigestRecompositionError,
    )
    from src.publication.digest_narrative import sanitize_digest_narrative_draft

    if editor_scope not in ("targeted_items", "thematic_blocks"):
        raise ValueError("digest_editor_scope is unsupported")
    thematic = editor_scope == "thematic_blocks"
    if not _digest_repair_request(checkpoint)[0] and not (thematic and review_without_findings):
        return checkpoint, False, 0
    deadline = deadline_at if deadline_at is not None else time.monotonic() + timeout_seconds
    actual_calls = 0
    used = False
    feedback = ""
    feedback_targets: tuple[str, ...] = ()
    safe_checkpoint = checkpoint
    editor_checkpoint = checkpoint
    max_calls = 3
    for call in range(max_calls):
        checkpoint = editor_checkpoint
        size_only_failure = _digest_has_only_size_failure(checkpoint)
        if call == 2 and not size_only_failure:
            checkpoint_safe = (
                checkpoint[1].is_valid
                and checkpoint[4].is_publishable
                and checkpoint[2].story_coverage >= 1.0
                and checkpoint[2].material_fact_coverage >= 1.0
            )
            if not checkpoint_safe or not checkpoint[4].prose_audit.warnings:
                break
        findings, targets, recompose_ids = _digest_repair_request(checkpoint)
        checkpoint_requires_recomposition = not (
            checkpoint[1].is_valid
            and checkpoint[4].is_publishable
            and checkpoint[2].story_coverage >= 1.0
            and checkpoint[2].material_fact_coverage >= 1.0
        )
        structural_retry_blocks = tuple(
            dict.fromkeys(
                warning.block_id
                for warning in checkpoint[4].prose_audit.warnings
                if warning.code in {"OVERLONG_SYNTHESIS", "FRAGMENTED_SERVICE_REPORTS"}
                and warning.block_id
            )
        )
        if (
            not findings
            and not feedback
            and (call > 0 or not thematic or not review_without_findings)
        ):
            break
        edit_scope = None
        should_recompose = size_only_failure or (
            thematic and (call == 0 or checkpoint_requires_recomposition or structural_retry_blocks)
        )
        if should_recompose:
            block_ids = (
                tuple(block.block_id for block in checkpoint[0].blocks)
                if size_only_failure or call == 0 or checkpoint_requires_recomposition
                else structural_retry_blocks
            )
            edit_scope = build_digest_block_edit_scope(
                checkpoint[0], plan=plan, block_ids=block_ids
            )
            targets = edit_scope.item_ids
            recompose_ids = edit_scope.block_ids
            findings.append(
                "EDITORIAL_CONSTRAINT: Recompose the authorized full themes for natural hierarchy, cohesion, precise detail and honest attribution. Reconcile the complete required fact inventory and assign every fact ID exactly once. IDs are a coverage checklist, not a sentence quota: state a repeated supported condition once while assigning all IDs that restate it to the same item. Keep unchanged themes when already clear; do not invent context or remove selected facts."
            )
        if call == 2:
            if size_only_failure:
                findings.append(
                    "EDITORIAL_CONSTRAINT: FINAL_COMPRESSION. The previous candidate represents 100% of selected stories and required facts; its remaining failures concern text size. This is the final repair call. Its current rendered length is "
                    f"{getattr(checkpoint[3], 'visible_character_count', 'unknown')} visible characters and "
                    f"{getattr(checkpoint[3], 'utf16_character_count', 'unknown')} UTF-16 units; target at most 3600 characters. "
                    "Compress this exact candidate by removing duplicate wording, repeated labels and unnecessary phrasing. Keep each item body within the 1200-character limit. Preserve every selected story and fact exactly once, with its place, time, number, attribution, uncertainty and service scope attached. Do not solve length by dropping a fact, story, item, rubric or required detail. Recompose the full authorized themes and check the final rendered length."
                )
            else:
                findings.append(
                    "EDITORIAL_CONSTRAINT: FINAL_EDITORIAL_PASS. This is the final bounded style pass. Resolve the remaining listed prose warnings with the smallest clear edits. Assign every selected fact ID exactly once; do not repeat the same supported condition merely to echo multiple IDs. Preserve its evidence, attribution, uncertainty, place, time and scope."
                )
        if call > 0 and thematic:
            findings.append(
                "EDITORIAL_CONSTRAINT: Recheck the final wording against its own supporting facts "
                "and source text. Keep each street, district, date, duration, clock time and service "
                "state attached to the fact that supplies it. Preserve city-wide scope only when "
                "that fact explicitly says city-wide; do not transfer a time, status or location "
                "between facts. When support is unclear, keep the narrow attributed wording rather "
                "than broadening the claim."
            )
            if checkpoint_requires_recomposition:
                findings.append(
                    "EDITORIAL_CONSTRAINT: This final editor call must use full thematic recomposition because the current checkpoint is still unsafe. Return recomposed_items for every authorized block; a text-only patch cannot restore missing facts or repair fact membership."
                )
            elif structural_retry_blocks:
                findings.append(
                    "EDITORIAL_CONSTRAINT: This final editor call must recompose each authorized theme that still has a structural readability finding. Group related service reports into fewer coherent passages, remove repeated facts and message-by-message narration, and retain every exact fact once. Return recomposed_items, not text-only patches."
                )
            else:
                recompose_ids = ()
                # A safe checkpoint needs only local text repair. If the existing
                # candidate still lacks complete coverage or fails a hard check,
                # keep the repair structural so it can restore fact membership.
                local_targets = tuple(
                    dict.fromkeys(
                        item.item_id
                        for warning in checkpoint[4].prose_audit.warnings
                        for block in checkpoint[0].blocks
                        if block.block_id == warning.block_id
                        for index, item in enumerate(block.items)
                        if index == warning.item_index and item.item_id
                    )
                )
                targets = local_targets or targets or feedback_targets
                findings.append(
                    "EDITORIAL_CONSTRAINT: This is the final local text repair. Change only "
                    "authorized items; preserve other items exactly. Do not merge or recompose blocks. "
                    "Replace source-process narration with natural attributed reporting, preserving uncertainty."
                )
            if not checkpoint_requires_recomposition:
                warning_targets = _digest_warning_item_ids(
                    checkpoint[0],
                    checkpoint[4].prose_audit.warnings,
                    codes={
                        "REPETITIVE_BODY_ATTRIBUTION",
                        "SOURCE_META_NARRATION",
                        "SOURCE_PROCESS_DESCRIPTION",
                        "OVERLONG_SYNTHESIS",
                    },
                    allowed_ids=set(targets),
                )
                if warning_targets:
                    findings.append(
                        "EDITORIAL_CONSTRAINT: Apply the specific repair to these diagnosed items: "
                        + ", ".join(warning_targets)
                        + ". For repeated attribution, use one frame only across reports with the "
                        "same source and certainty; retain attribution when either changes and keep "
                        "qualifiers such as 'возможно', but do not repeat the same attribution as "
                        "'по их словам' in the same passage. Replace message-logistics phrases with the "
                        "supported event itself, without implying an unknown place is a different "
                        "district. For an overlong item, regroup the facts into two or three clear "
                        "service or locality passages when evidence supports that split. Preserve "
                        "each exact fact ID once, state any repeated supported condition only once, and keep every distinct time, place, uncertainty, and source scope."
                    )
        if feedback:
            findings.append(
                "EDITORIAL_CONSTRAINT: The previous edit was rejected: "
                + feedback
                + " Preserve every targeted fact and summary unit exactly once."
            )
        feedback_targets = targets
        attempt_id = await observer.attempt_started(
            "repair",
            metadata={
                "digest_implementation_versions": implementation_versions,
                "subkind": "digest_editor_combined_repair",
                "repair_call": call + 1,
                "finding_count": len(findings),
                "finding_codes": [
                    value.split(":", 2)[1]
                    if value.startswith(("STYLE_OBSERVATION:", "DIGEST_QUALITY:"))
                    else value.split(":", 1)[0]
                    for value in findings
                ][:20],
            },
        )
        actual_calls += 1
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("DIGEST_GENERATION_DEADLINE")
            extra_kwargs: dict[str, Any] = {
                "max_context_chars": max_context_chars,
                "support_text_by_id": support_text_by_id,
            }
            if edit_scope is not None:
                extra_kwargs.update(
                    edit_scope=edit_scope,
                    max_context_chars=max_context_chars,
                    support_text_by_id=support_text_by_id,
                )
            async with asyncio.timeout(remaining):
                edited = await editor.polish_and_compress(
                    checkpoint[0],
                    plan=plan,
                    evidence=evidence,
                    max_chars=3600,
                    model=model,
                    violations=findings,
                    target_item_ids=targets,
                    recompose_block_ids=recompose_ids,
                    **extra_kwargs,
                )
            candidate = sanitize_digest_narrative_draft(edited)
            validation, coverage, artifact, audit = evaluate_candidate(candidate)
            safe = (
                validation.is_valid
                and audit.is_publishable
                and coverage.story_coverage >= 1.0
                and coverage.material_fact_coverage >= 1.0
            )
            checkpoint_safe = (
                checkpoint[1].is_valid
                and checkpoint[4].is_publishable
                and checkpoint[2].story_coverage >= 1.0
                and checkpoint[2].material_fact_coverage >= 1.0
            )
            # Preserve an already clear price-to-leg relation. This quarantines
            # an editor regression; the advisory itself never vetoes publication
            # or prevents recovery from an initially unsafe writer response.
            previous_fare_ambiguities = {
                warning.block_id
                for warning in checkpoint[4].prose_audit.warnings
                if warning.code == "AMBIGUOUS_PASSING_BUS_FARE"
            }
            new_fare_ambiguities = {
                warning.block_id
                for warning in audit.prose_audit.warnings
                if warning.code == "AMBIGUOUS_PASSING_BUS_FARE"
            } - previous_fare_ambiguities
            previous_repetition = sum(
                w.code == "REPEATED_SUPPORTED_MEASUREMENT"
                for w in checkpoint[4].prose_audit.warnings
            )
            new_repetition = (
                sum(w.code == "REPEATED_SUPPORTED_MEASUREMENT" for w in audit.prose_audit.warnings)
                > previous_repetition
            )
            # A safe rewrite that leaves the reader with more diagnosed problems
            # (e.g. one overlong roster split into near-duplicate items) is not
            # an improvement; retain the exact previous safe checkpoint. Thematic
            # recomposition has its own staged no-progress policy.
            more_reader_findings = not thematic and len(audit.prose_audit.warnings) > len(
                checkpoint[4].prose_audit.warnings
            )
            editorial_regression = (
                safe
                and checkpoint_safe
                and (bool(new_fare_ambiguities) or new_repetition or more_reader_findings)
            )
            local_issues = {
                issue for issue in _digest_local_issue_keys(checkpoint) if issue[1] in targets
            }
            remaining_issues = _digest_local_issue_keys(
                (candidate, validation, coverage, artifact, audit)
            )
            no_local_progress = (
                thematic
                and call > 0
                and safe
                and checkpoint_safe
                and candidate != checkpoint[0]
                and bool(local_issues)
                and not (local_issues - remaining_issues)
            )
            accepted = (
                safe
                and not editorial_regression
                and not no_local_progress
                and candidate != checkpoint[0]
            )
            outcome = (
                "rejected_editorial_regression"
                if editorial_regression
                else "rejected_editorial_no_progress"
                if no_local_progress
                else "accepted_change"
                if accepted
                else "unchanged_safe"
                if safe
                else "rejected_unsafe"
            )
            await observer.attempt_finished(
                attempt_id,
                "succeeded"
                if (
                    accepted
                    or safe
                    and thematic
                    and not editorial_regression
                    and not no_local_progress
                )
                else "failed",
                error_kind=None
                if (
                    accepted
                    or safe
                    and thematic
                    and not editorial_regression
                    and not no_local_progress
                )
                else "digest_editor_combined_repair_unresolved",
                metadata={
                    "repair_used": accepted,
                    "editor_outcome": outcome,
                    "validation": {
                        "is_valid": validation.is_valid,
                        "scope": "implemented_hard_checks_only",
                        "not_evaluated": list(validation.not_evaluated),
                    },
                    "quality_audit": {
                        "is_publishable": audit.is_publishable,
                        "finding_codes": [c.code for c in audit.checks],
                        "style_codes": [w.code for w in audit.prose_audit.warnings],
                    },
                },
            )
            if accepted:
                checkpoint = (candidate, validation, coverage, artifact, audit)
                safe_checkpoint = checkpoint
                editor_checkpoint = checkpoint
                used = True
                feedback = ""
            elif no_local_progress:
                feedback = "the local edit did not resolve any diagnosed target issue"
            elif editorial_regression:
                feedback = (
                    "the edit introduced a repeated supported measurement or an ambiguous passing-bus "
                    "fare into a safe checkpoint; retain each measurement once with its owner "
                    "and preserve the explicit paid-leg meaning"
                )
            elif not safe:
                candidate_checkpoint = (candidate, validation, coverage, artifact, audit)
                rejected_findings, _, _ = _digest_repair_request(candidate_checkpoint)
                if _digest_has_only_size_failure(candidate_checkpoint):
                    editor_checkpoint = candidate_checkpoint
                    feedback = (
                        "the candidate preserves all selected stories and required facts and "
                        "passes the other blocking checks, but its rendered text exceeds the "
                        "single-post limit; continue from this exact candidate and compress it "
                        "without dropping facts"
                    )
                else:
                    feedback = (
                        "the candidate failed factual, coverage, or rendered-post safety checks:\n"
                        + "\n".join(rejected_findings)
                    )
            else:
                if thematic:
                    break
                feedback = "the targeted wording did not change"
        except (TimeoutError, asyncio.CancelledError):
            raise
        except DigestEditorContextMissingError:
            await observer.attempt_finished(
                attempt_id, "succeeded", metadata={"editor_outcome": "skipped_missing_context"}
            )
            break
        except DigestEditorContextBudgetError:
            await observer.attempt_finished(
                attempt_id, "succeeded", metadata={"editor_outcome": "skipped_context_budget"}
            )
            break
        except Exception as exc:
            feedback = str(exc) if isinstance(exc, DigestRecompositionError) else type(exc).__name__
            await observer.attempt_finished(
                attempt_id,
                "failed",
                error_kind="digest_editor_combined_repair_exception",
                metadata={"error_message": feedback, "editor_outcome": "invalid_response"},
            )
            logger.warning("DigestEditor repair rejected: %s", feedback)
    return safe_checkpoint, used, actual_calls


class PublicationGenerationService:
    """Orchestrates publication generation from frozen inputs with attempt audit."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        config: Config | None = None,
        repo: PublicationRepository | None = None,
        adapter: KnowledgeEditorialAdapter | None = None,
        generator: ArticleGenerator | None = None,
        editorializer: Any = None,
        rubric_classifier: DigestRubricClassifier | None = None,
    ) -> None:
        from src.config_loader import load_config

        del editorializer
        self.uow = uow
        self.config = config or load_config()
        self.repo = repo or PublicationRepository()
        self.adapter = adapter or KnowledgeEditorialAdapter(uow=uow, repo=self.repo)
        self.generator = generator or ArticleGenerator(config=self.config, logger=logger)

        self.rubric_classifier = rubric_classifier
        if self.rubric_classifier is None:
            try:
                emb_prov = create_embedding_provider(self.config, logger)
                prov_name = getattr(
                    emb_prov,
                    "provider_name",
                    getattr(self.config.embedding, "provider", ""),
                )
                model = getattr(emb_prov, "model", getattr(self.config.embedding, "model", ""))
                dim = getattr(self.config.embedding, "dimensions", 1536)
                self.rubric_classifier = DigestRubricClassifier(
                    provider=emb_prov,
                    provider_name=prov_name,
                    model=model,
                    dimensions=dim,
                )
            except Exception as exc:
                logger.warning("Could not initialize rubric embedding provider: %s", exc)
                self.rubric_classifier = DigestRubricClassifier()

    async def generate(
        self,
        run_id: int,
        *,
        defer_delivery: bool = True,
        publication_metadata: dict[str, Any] | None = None,
    ) -> Publication:
        digest_implementation_versions: dict[str, Any] = {}
        async with self.uow.transaction() as conn:
            run = await self.repo.lock_run(conn, run_id)
            if run is None:
                raise ValueError(f"publication run {run_id} not found")

            if run.status == "succeeded":
                existing = await self.repo.get_publication_by_run_id(conn, run_id)
                if existing is not None:
                    return existing

            if run.status not in ("selected_inputs_sealed", "generating"):
                raise RuntimeError(
                    f"cannot generate for publication run {run_id} in status '{run.status}'"
                )

            if run.publication_type in DIGEST_PUBLICATION_TYPES:
                from src.publication.digest_composition import COMPOSITION_POLICY_VERSION
                from src.publication.digest_contracts import DIGEST_ELIGIBILITY_VERSION
                from src.publication.digest_narrative import (
                    DIGEST_COMPOSITION_MEMBERSHIP_VERSION,
                    DIGEST_NARRATIVE_SANITIZER_VERSION,
                )
                from src.publication.digest_quality_diagnostics import DIGEST_DIAGNOSTICS_VERSION
                from src.publication.narrative_contract import DIGEST_NARRATIVE_PROMPT_VERSION
                from src.publication.policies import (
                    DIGEST_EDITOR_PROMPT_VERSION,
                    DIGEST_EDITORIALIZER_PROMPT_VERSION,
                    SELECTION_SEMANTICS_VERSION,
                )

                eligibility_policy = (
                    await self.repo.get_eligibility_policy_by_id(conn, run.eligibility_policy_id)
                    if run.eligibility_policy_id is not None
                    else None
                )
                selection_policy = (
                    await self.repo.get_selection_policy_by_id(conn, run.selection_policy_id)
                    if run.selection_policy_id is not None
                    else None
                )
                writer_policy = (
                    await self.repo.get_writer_policy_by_id(conn, run.writer_policy_id)
                    if run.writer_policy_id is not None
                    else None
                )
                eligibility_config = eligibility_policy.config if eligibility_policy else {}
                selection_config = selection_policy.config if selection_policy else {}
                writer_config = writer_policy.config if writer_policy else {}
                digest_implementation_versions = {
                    "policy_ids": {
                        "eligibility": run.eligibility_policy_id,
                        "selection": run.selection_policy_id,
                        "writer": run.writer_policy_id,
                    },
                    "digest_eligibility_version": eligibility_config.get(
                        "digest_eligibility_version", "legacy_unversioned"
                    ),
                    "eligibility_implementation_version": DIGEST_ELIGIBILITY_VERSION,
                    "selection_semantics_version": selection_config.get(
                        "selection_semantics_version", "unknown"
                    ),
                    "selection_semantics_implementation": SELECTION_SEMANTICS_VERSION,
                    "selection_prompt_version": (
                        selection_policy.prompt_version if selection_policy else "unknown"
                    ),
                    "writer_prompt_version": (
                        writer_policy.prompt_version if writer_policy else "unknown"
                    ),
                    "editorializer_prompt_version": writer_config.get(
                        "editorializer_prompt_version", DIGEST_EDITORIALIZER_PROMPT_VERSION
                    ),
                    "digest_editor_prompt_version": writer_config.get(
                        "digest_editor_prompt_version", DIGEST_EDITOR_PROMPT_VERSION
                    ),
                    "narrative_contract_prompt_version": writer_config.get(
                        "narrative_contract_prompt_version", DIGEST_NARRATIVE_PROMPT_VERSION
                    ),
                    "composition_policy_version": COMPOSITION_POLICY_VERSION,
                    "composition_membership_version": DIGEST_COMPOSITION_MEMBERSHIP_VERSION,
                    "narrative_sanitizer_version": DIGEST_NARRATIVE_SANITIZER_VERSION,
                    "diagnostics_version": DIGEST_DIAGNOSTICS_VERSION,
                }

            # Validate frozen writer policy semantics for article publications
            if run.publication_type in ARTICLE_PUBLICATION_TYPES:
                writer_policy = await self.repo.get_writer_policy_by_id(conn, run.writer_policy_id)
                if writer_policy is not None and writer_policy.config:
                    w_ver = writer_policy.config.get("article_writer_version")
                    if w_ver is not None and w_ver not in SUPPORTED_ARTICLE_WRITER_VERSIONS:
                        raise UnsupportedFrozenSemanticVersion(
                            f"Unsupported frozen article_writer_version: {w_ver} (supported: {SUPPORTED_ARTICLE_WRITER_VERSIONS})"
                        )
                    plan_ver = writer_policy.config.get("article_coverage_plan_version")
                    if (
                        plan_ver is not None
                        and plan_ver not in SUPPORTED_ARTICLE_COVERAGE_PLAN_VERSIONS
                    ):
                        raise UnsupportedFrozenSemanticVersion(
                            f"Unsupported frozen article_coverage_plan_version: {plan_ver} (supported: {SUPPORTED_ARTICLE_COVERAGE_PLAN_VERSIONS})"
                        )
                    editorial_plan_ver = writer_policy.config.get("article_editorial_plan_version")
                    if (
                        editorial_plan_ver is not None
                        and editorial_plan_ver not in SUPPORTED_ARTICLE_EDITORIAL_PLAN_VERSIONS
                    ):
                        raise UnsupportedFrozenSemanticVersion(
                            "Unsupported frozen article_editorial_plan_version: "
                            f"{editorial_plan_ver} "
                            f"(supported: {SUPPORTED_ARTICLE_EDITORIAL_PLAN_VERSIONS})"
                        )
                    rec_ver = writer_policy.config.get("article_recovery_version")
                    if rec_ver is not None and rec_ver not in SUPPORTED_ARTICLE_RECOVERY_VERSIONS:
                        raise UnsupportedFrozenSemanticVersion(
                            f"Unsupported frozen article_recovery_version: {rec_ver} (supported: {SUPPORTED_ARTICLE_RECOVERY_VERSIONS})"
                        )

            await self.repo.transition_run(conn, run_id, "generating")

        # Build deterministic frozen input from sealed knowledge
        async with self.uow.transaction() as conn:
            inputs = await self.repo.load_sealed_inputs(conn, run_id)
            has_event_first = any(bool(inp.fragment_ids) for inp in inputs)
            if not has_event_first:
                s_ids = [inp.story_id for inp in inputs]
                if s_ids:
                    s_cur = await conn.execute(
                        "SELECT COUNT(*) FROM stories WHERE id = ANY(%s) AND knowledge_source = 'event_first'",
                        (s_ids,),
                    )
                    s_row = await s_cur.fetchone()
                    has_event_first = bool(s_row and s_row[0] and int(s_row[0]) > 0)

        if has_event_first:
            from src.publication.event_editorial_adapter import EventEditorialAdapter

            event_adapter = EventEditorialAdapter(uow=self.uow, repo=self.repo)
            frozen = await event_adapter.adapt_inputs(run_id, inputs=inputs)
        else:
            frozen = await self.adapter.build(run_id)

        # Observer records each attempt into publication_generation_attempts
        observer = DatabaseGenerationAttemptObserver(uow=self.uow, run_id=run_id, repo=self.repo)
        fallback_attempt_id: int | None = None

        try:
            if run.publication_type in DIGEST_PUBLICATION_TYPES:
                # Rubric classification (semantic embedding assignment)
                if (
                    frozen.analysis.cards
                    and self.config.settings.digest_rubrics is not None
                    and self.rubric_classifier is not None
                ):
                    try:
                        att_id = await observer.attempt_started(
                            "writer",
                            metadata={
                                "subkind": "rubric_classifier",
                                "classifier_version": RUBRIC_CLASSIFIER_VERSION,
                                "card_count": len(frozen.analysis.cards),
                            },
                        )

                        classified_cards, assignments = await self.rubric_classifier.classify(
                            frozen.analysis.cards,
                            rubrics=self.config.settings.digest_rubrics,
                        )
                        frozen = replace(
                            frozen,
                            analysis=replace(frozen.analysis, cards=classified_cards),
                        )
                        await observer.attempt_finished(
                            att_id,
                            "succeeded",
                            metadata={
                                "assignments": [
                                    {
                                        "story_id": a.story_id,
                                        "rubric_id": a.rubric_id,
                                        "score": a.score,
                                        "method": a.method,
                                    }
                                    for a in assignments
                                ]
                            },
                        )
                    except Exception as exc:
                        logger.warning(
                            "digest rubric classifier failed (%s: %s); falling back",
                            type(exc).__name__,
                            exc,
                        )

                from src.publication.renderers import PublicationDigestRenderer

                renderer = PublicationDigestRenderer(
                    output_language=getattr(self.config.settings, "output_language", "Russian"),
                    use_emojis=getattr(self.config.settings, "use_emojis", True),
                    include_statistics=getattr(self.config.settings, "include_statistics", True),
                    rubrics_config=self.config.settings.digest_rubrics,
                    custom_rubrics=getattr(self.config.settings, "digest_groups", None),
                )

                from src.publication.digest_presentation import build_digest_presentation_plan

                narrative_draft = None
                pub_edit = getattr(self.config.settings, "publication_editorial", None)
                digest_implementation_versions.update(
                    writer_material_format=getattr(
                        pub_edit, "digest_writer_material_format", "legacy"
                    ),
                    editor_scope=getattr(pub_edit, "digest_editor_scope", "targeted_items"),
                )
                configured_narrative_mode = (
                    getattr(pub_edit, "digest_narrative_mode", "deterministic")
                    if pub_edit
                    else "deterministic"
                )
                narrative_mode = effective_digest_narrative_mode(configured_narrative_mode)

                max_sit_items = (
                    getattr(pub_edit, "digest_city_situation_max_items", 7) if pub_edit else 7
                )
                max_sit_details = (
                    getattr(pub_edit, "digest_city_situation_max_details_per_item", 2)
                    if pub_edit
                    else 2
                )
                max_positive_items = (
                    getattr(pub_edit, "digest_city_situation_max_positive_items", 2)
                    if pub_edit
                    else 2
                )
                evidence_dict = getattr(frozen.analysis, "evidence", {}) or {}

                presentation_plan = build_digest_presentation_plan(
                    cards=frozen.analysis.cards,
                    city_situation=frozen.analysis.city_situation,
                    evidence=evidence_dict,
                    max_city_situation_items=max_sit_items,
                    max_city_situation_details=max_sit_details,
                    max_city_situation_positive_items=max_positive_items,
                    include_all_candidates=True,
                )

                # Freeze fact-level composition and budget admission before any writer call.
                if frozen.analysis.cards:
                    from src.publication.digest_composition import build_digest_composition
                    from src.publication.errors import PublicationGenerationError

                    input_by_decision = {
                        int(inp.selection_decision_id): (f"story:{inp.story_id}", str(inp.story_id))
                        for inp in inputs
                    }
                    selection_suggestions: dict[str, Any] = {}
                    edition_slug = ""
                    async with self.uow.transaction() as conn:
                        edition_cur = await conn.execute(
                            """
                            SELECT e.slug
                            FROM publication_runs pr
                            JOIN editions e ON e.id = pr.edition_id
                            WHERE pr.id = %s
                            """,
                            (run.id,),
                        )
                        edition_row = await edition_cur.fetchone()
                        if edition_row and edition_row[0]:
                            edition_slug = str(edition_row[0])
                        if not edition_slug:
                            raise PublicationGenerationError(
                                "DIGEST_EDITION_GEOGRAPHY_UNAVAILABLE:missing frozen run edition slug"
                            )

                        if input_by_decision:
                            decision_cur = await conn.execute(
                                """
                                SELECT id, metadata
                                FROM publication_selection_decisions
                                WHERE id = ANY(%s)
                                """,
                                (list(input_by_decision),),
                            )
                            for decision_id, decision_metadata in await decision_cur.fetchall():
                                story_keys = input_by_decision.get(int(decision_id), ())
                                metadata = (
                                    decision_metadata if isinstance(decision_metadata, dict) else {}
                                )
                                suggestion = metadata.get("selector_exclusion_suggestion")
                                resolved = (
                                    suggestion
                                    if suggestion is not None
                                    else {"status": "unavailable"}
                                )
                                for story_key in story_keys:
                                    selection_suggestions[story_key] = resolved

                    rubric_config = getattr(self.config.settings, "digest_rubrics", None)
                    composition = build_digest_composition(
                        presentation_plan,
                        frozen.analysis.cards,
                        evidence_dict,
                        edition_slug=edition_slug,
                        snapshot_at=getattr(run, "snapshot_at", None),
                        max_chars=3900,
                        # Fixed title/date allowance. Rubric labels and spacing
                        # are charged once per admitted rubric by the composer.
                        reserved_chars=128,
                        include_statistics=bool(
                            getattr(self.config.settings, "include_statistics", True)
                        ),
                        selector_suggestions=selection_suggestions,
                        rubric_labels={
                            rubric.id: rubric.name for rubric in getattr(rubric_config, "items", ())
                        },
                        rubric_fallback_id=getattr(
                            getattr(rubric_config, "fallback", None), "id", "other"
                        ),
                    )
                    if not composition.feasible:
                        raise PublicationGenerationError(
                            f"DIGEST_BUDGET_FAILURE:{composition.failure_reason}"
                        )
                    presentation_plan = presentation_plan.with_composition(composition)

                if narrative_mode == "single_call" and frozen.analysis.cards:
                    from src.publication.digest_narrative import (
                        DigestNarrativeWriter,
                        build_digest_support_text_index,
                        plan_digest_narrative_blocks,
                    )

                    detail_cards = [
                        c
                        for c in frozen.analysis.cards
                        if c.id in presentation_plan.detail_story_ids
                    ]

                    max_cards = getattr(pub_edit, "digest_narrative_max_cards_per_block", 6)
                    max_tokens = getattr(pub_edit, "digest_narrative_max_output_tokens", 4096)
                    narrative_timeout = (
                        getattr(pub_edit, "digest_narrative_timeout_seconds", None)
                        or getattr(self.config.settings, "digest_narrative_timeout_seconds", None)
                        or 600
                    )
                    plan = plan_digest_narrative_blocks(
                        cards=detail_cards,
                        evidence=evidence_dict,
                        rubrics=renderer.rubrics,
                        max_cards_per_block=max_cards,
                        presentation_plan=presentation_plan,
                        edition_slug=getattr(frozen, "edition_slug", "") or edition_slug,
                    )

                    writer_provider = getattr(self.generator, "provider", None)
                    writer = DigestNarrativeWriter(
                        provider=writer_provider,
                        writer_material_format=getattr(
                            pub_edit, "digest_writer_material_format", "legacy"
                        ),
                    )
                    att_id = await observer.attempt_started(
                        "writer",
                        metadata={
                            "digest_implementation_versions": digest_implementation_versions,
                            "subkind": f"digest_narrative_{narrative_mode}",
                            "block_count": len(plan.blocks),
                            "card_count": len(detail_cards),
                            "situation_group_count": len(presentation_plan.city_situation.groups),
                        },
                    )
                    generation_deadline = time.monotonic() + narrative_timeout
                    try:
                        has_topic_bundles = any(
                            getattr(b, "topic_bundles", None) for b in plan.blocks
                        )
                        async with asyncio.timeout(narrative_timeout):
                            draft_cand = await writer.generate_narrative_draft(
                                plan=plan,
                                cards=detail_cards,
                                evidence=evidence_dict,
                                language=getattr(
                                    self.config.settings, "output_language", "Russian"
                                ),
                                max_output_tokens=max_tokens,
                                model=getattr(self.config.settings, "openai_model", None)
                                or getattr(self.config.settings, "ai_model", None),
                                situation_plan=None
                                if has_topic_bundles
                                else presentation_plan.city_situation,
                            )
                        support_text_index = build_digest_support_text_index(
                            evidence=evidence_dict,
                            cards=frozen.analysis.cards,
                            frozen_input=frozen,
                        )
                        all_draft_support_texts = list(support_text_index.values())
                        allowed_digest_terms = compute_digest_allowed_terms(
                            all_draft_support_texts, getattr(run, "snapshot_at", None)
                        )

                        from src.publication.digest_narrative import sanitize_digest_narrative_draft
                        from src.publication.errors import DigestCoverageInvariantError

                        support_text_index = build_digest_support_text_index(
                            evidence=evidence_dict,
                            cards=frozen.analysis.cards,
                            frozen_input=frozen,
                        )
                        all_draft_support_texts = list(support_text_index.values())
                        allowed_digest_terms = compute_digest_allowed_terms(
                            all_draft_support_texts, getattr(run, "snapshot_at", None)
                        )

                        from src.publication.digest_assessment import (
                            DigestAssessmentContext,
                            assess_digest_candidate,
                        )

                        assessment_context = DigestAssessmentContext(
                            frozen=frozen,
                            plan=plan,
                            presentation_plan=presentation_plan,
                            evidence=evidence_dict,
                            support_text_by_id=support_text_index,
                            allowed_context_terms=allowed_digest_terms,
                            snapshot_at=run.snapshot_at,
                            timezone_name=getattr(self.config.settings, "timezone", "UTC"),
                            renderer=renderer,
                        )

                        def _evaluate_candidate(
                            candidate: Any, *, allow_incomplete_coverage: bool = False
                        ) -> tuple[Any, Any, Any, Any]:
                            return assess_digest_candidate(
                                candidate,
                                context=assessment_context,
                                allow_incomplete_coverage=allow_incomplete_coverage,
                            ).checks()

                        draft_cand = sanitize_digest_narrative_draft(draft_cand)
                        val_res, coverage_trace, rendered_artifact, rendered_audit = (
                            _evaluate_candidate(draft_cand, allow_incomplete_coverage=True)
                        )
                        if val_res.not_evaluated:
                            logger.info(
                                "digest narrative has semantic bindings explicitly marked NOT_EVALUATED: %s",
                                list(val_res.not_evaluated)[:10],
                            )

                        from src.publication.digest_editor import DigestEditor

                        (
                            checkpoint,
                            repair_used,
                            digest_repair_max_calls,
                        ) = await _repair_digest_candidate(
                            checkpoint=(
                                draft_cand,
                                val_res,
                                coverage_trace,
                                rendered_artifact,
                                rendered_audit,
                            ),
                            plan=plan,
                            evidence=evidence_dict,
                            editor=DigestEditor(provider=writer_provider),
                            observer=observer,
                            evaluate_candidate=_evaluate_candidate,
                            model=getattr(self.config.settings, "openai_model", None)
                            or getattr(self.config.settings, "ai_model", None),
                            timeout_seconds=narrative_timeout,
                            implementation_versions=digest_implementation_versions,
                            editor_scope=getattr(pub_edit, "digest_editor_scope", "targeted_items"),
                            support_text_by_id=support_text_index,
                            deadline_at=generation_deadline
                            if getattr(pub_edit, "digest_editor_scope", "targeted_items")
                            == "thematic_blocks"
                            else None,
                        )
                        draft_cand, val_res, coverage_trace, rendered_artifact, rendered_audit = (
                            checkpoint
                        )

                        blocking_findings = [
                            check.code for check in rendered_audit.blocking_failures
                        ]
                        if not val_res.is_valid:
                            blocking_findings.extend(val_res.violations)
                        if blocking_findings:
                            raise PublicationGenerationError(
                                "DIGEST_RENDERED_AUDIT_FAILED: "
                                + "; ".join(dict.fromkeys(blocking_findings))
                            )
                        if (
                            coverage_trace.story_coverage < 1.0
                            or coverage_trace.material_fact_coverage < 1.0
                        ):
                            raise DigestCoverageInvariantError(
                                "final canonical digest artifact has incomplete story or material-fact coverage"
                            )

                        quality_audit = rendered_audit.prose_audit
                        narrative_draft = draft_cand
                        title, lead, _legacy_body = renderer.render_grouped_digest(
                            frozen,
                            snapshot_at=run.snapshot_at,
                            timezone_name=getattr(self.config.settings, "timezone", "UTC"),
                            narrative_draft=narrative_draft,
                            presentation_plan=presentation_plan,
                        )
                        body = rendered_artifact.visible_text
                        presentations = presentation_plan.story_presentations
                        coverage_meta = {
                            "planned_story_count": len(presentation_plan.story_ids),
                            "dashboard_only_count": sum(
                                p.mode == "DASHBOARD_ONLY" for p in presentations
                            ),
                            "detail_only_count": sum(
                                p.mode == "DETAIL_ONLY" for p in presentations
                            ),
                            "dashboard_and_drilldown_count": sum(
                                p.mode == "DASHBOARD_AND_DRILLDOWN" for p in presentations
                            ),
                            "final_covered_story_count": len(coverage_trace.story_ids),
                            "final_digest_story_coverage": coverage_trace.story_coverage,
                            "final_digest_material_fact_coverage": coverage_trace.material_fact_coverage,
                            "deterministic_digest_fallback_used": False,
                            "digest_presentation_plan": presentation_plan.to_metadata_dict(),
                            "digest_coverage_trace": coverage_trace.to_metadata_dict(),
                            "digest_admission_trace": coverage_trace.admission_to_dict(),
                            "rendered_digest_artifact": rendered_artifact.as_metadata(),
                            "digest_quality_audit": rendered_audit.as_metadata(),
                            "digest_repair": {
                                "used": repair_used,
                                "max_calls": 3,
                                "actual_calls": digest_repair_max_calls,
                            },
                            "digest_implementation_versions": digest_implementation_versions,
                            "upstream_hard_exclusion_count": None,
                            "upstream_hard_exclusion_count_status": "unavailable_in_frozen_publication_snapshot",
                        }
                        await observer.attempt_finished(
                            att_id,
                            "succeeded",
                            metadata={
                                "validation": {
                                    "is_valid": True,
                                    "scope": "implemented_hard_checks_only",
                                    "not_evaluated": list(val_res.not_evaluated),
                                },
                                "block_count": len(draft_cand.blocks),
                                "situation_item_count": len(draft_cand.situation_items),
                                "prose_quality_audit": quality_audit.as_metadata(),
                                **coverage_meta,
                            },
                        )

                    except Exception as exc:
                        logger.warning(
                            "digest narrative synthesis failed (%s: %s); publication will fail closed",
                            type(exc).__name__,
                            exc,
                            exc_info=True,
                        )

                        await observer.attempt_finished(
                            att_id,
                            "failed",
                            error_kind=(
                                "digest_narrative_timeout"
                                if isinstance(exc, TimeoutError)
                                else "digest_narrative_synthesis_failed"
                            ),
                            metadata={
                                "error_message": str(exc)
                                or f"digest narrative writer timed out after {narrative_timeout}s",
                                **(
                                    {"timeout_seconds": narrative_timeout}
                                    if isinstance(exc, TimeoutError)
                                    else {}
                                ),
                            },
                        )
                        allow_fallback = getattr(
                            pub_edit,
                            "digest_allow_deterministic_fallback",
                            False,
                        )
                        if not allow_fallback and narrative_mode == "single_call":
                            raise PublicationGenerationError(
                                f"Digest narrative generation failed: {exc}"
                            ) from exc

                if narrative_draft is None:
                    raise PublicationGenerationError(
                        "DIGEST_AI_NARRATIVE_REQUIRED: no verified AI-authored digest draft was produced; deterministic prose fallback is disabled"
                    )

            else:
                title, lead, body = await self.generator.generate_from_frozen_input(
                    frozen, attempt_observer=observer
                )
                is_article = run.publication_type in (
                    "daily_article",
                    "article",
                    "weekly_article",
                    "monthly_article",
                ) or run.publication_type.endswith("_article")
                if is_article and self.config is not None:
                    article_meta: dict[str, Any] = {}
                    article_cfg = getattr(self.config.settings, "article", None)
                    preview_mode = bool(
                        publication_metadata and publication_metadata.get("preview") is True
                    )

                    # Preview generation must not create external artifacts. In particular,
                    # do not create a Telegra.ph page or an image that could later be
                    # mistaken for a deliverable publication.
                    if not preview_mode and not (
                        publication_metadata and publication_metadata.get("telegraph_url")
                    ):
                        try:
                            from src.telegraph import TelegraphPublisher

                            author_name = (
                                getattr(article_cfg, "author_name", "@berdiansk_news")
                                if article_cfg
                                else "@berdiansk_news"
                            )
                            token = (
                                getattr(article_cfg, "telegraph_access_token", None)
                                if article_cfg
                                else None
                            )
                            publisher = TelegraphPublisher(access_token=token, logger=logger)
                            telegraph_url = await publisher.create_page(
                                title=title,
                                content_markdown=body,
                                author_name=author_name,
                            )
                            article_meta["telegraph_url"] = telegraph_url
                            logger.info("Published article to Telegra.ph: %s", telegraph_url)
                        except Exception as exc:
                            logger.warning("Failed to publish article to Telegra.ph: %s", exc)

                    if not preview_mode and not (
                        publication_metadata and publication_metadata.get("photo_path")
                    ):
                        try:
                            from src.image_generator import NewsImageGenerator

                            img_gen = NewsImageGenerator(self.config, logger)
                            img_prompt = await img_gen.generate_prompt(
                                title=title, lead=lead or "", article_text=body
                            )
                            photo_path = await img_gen.generate_image(img_prompt)
                            if photo_path:
                                article_meta["photo_path"] = str(photo_path)
                                article_meta["image_prompt"] = img_prompt
                                logger.info("Generated article cover photo: %s", photo_path)
                        except Exception as exc:
                            logger.warning("Failed to generate article cover photo: %s", exc)

                    if article_meta:
                        publication_metadata = {**(publication_metadata or {}), **article_meta}
        except ArticlePublicationRejected as exc:
            logger.warning(
                "article publication rejected for run %s: %s (%s)",
                run_id,
                exc.reason,
                exc,
            )
            async with self.uow.transaction() as conn:
                await self.repo.transition_run(
                    conn,
                    run_id,
                    "failed",
                    error_kind=exc.error_kind,
                    completed_at=dt.datetime.now(dt.timezone.utc),
                )
            raise
        except Exception as exc:
            logger.error("generation failed completely for run %s: %s", run_id, exc)
            if fallback_attempt_id is not None:
                try:
                    await observer.attempt_finished(
                        fallback_attempt_id,
                        "failed",
                        error_kind="digest_deterministic_fallback_failed",
                        metadata={"error_message": str(exc)},
                    )
                except Exception as observer_exc:
                    logger.error(
                        "could not close fallback attempt %s for run %s: %s",
                        fallback_attempt_id,
                        run_id,
                        observer_exc,
                    )
            async with self.uow.transaction() as conn:
                await self.repo.transition_run(
                    conn, run_id, "failed", error_kind=type(exc).__name__
                )
            raise

        winning_attempt = observer.last_successful_content_attempt
        if winning_attempt is None:
            # Fallback: find the latest succeeded attempt from DB
            async with self.uow.transaction() as conn:
                cursor = await conn.execute(
                    """
                    SELECT id, publication_run_id, attempt_no, kind, status, error_kind,
                           provider, model, prompt_hash, metadata, started_at, completed_at
                    FROM publication_generation_attempts
                    WHERE publication_run_id = %s AND status = 'succeeded'
                    ORDER BY attempt_no DESC LIMIT 1
                    """,
                    (run_id,),
                )
                row = await cursor.fetchone()
                if row is not None:
                    from src.publication.models import PublicationGenerationAttempt

                    winning_attempt = PublicationGenerationAttempt.from_row(row)

        if winning_attempt is None:
            raise RuntimeError(f"no successful generation attempt recorded for run {run_id}")

        winning_meta = dict(winning_attempt.metadata or {})
        meta: dict[str, Any] = {
            **winning_meta,
            "winning_kind": winning_meta.get("winning_kind", winning_attempt.kind),
        }
        if publication_metadata:
            meta.update(publication_metadata)

        async with self.uow.transaction() as conn:
            pub = await self.repo.create_publication(
                conn,
                run_id=run_id,
                winning_attempt_id=winning_attempt.id,
                publication_type=run.publication_type,
                title=title,
                lead=lead,
                body=body,
                metadata=meta,
            )
            await self.repo.transition_run(
                conn, run_id, "succeeded", completed_at=dt.datetime.now(dt.timezone.utc)
            )
            if defer_delivery:
                await self._defer_delivery_payloads(conn, pub.id)
            return pub

    async def _defer_delivery_payloads(
        self, conn: psycopg.AsyncConnection, publication_id: int
    ) -> None:
        try:
            from src.jobs.publication import prepare_delivery_payloads

            await prepare_delivery_payloads.configure(connection=conn).defer_async(
                publication_id=publication_id
            )
        except Exception as err:
            # Re-raise: the transaction must roll back so the run is not left
            # succeeded with no delivery job ever queued.
            logger.error(
                "could not defer prepare_delivery_payloads for publication %s: %s",
                publication_id,
                err,
            )
            raise
