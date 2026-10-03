"""Publication generation service producing immutable publications and generation attempt history (Plan 4 Task 5)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections.abc import Sequence
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
                from src.publication.digest_narrative import DIGEST_COMPOSITION_MEMBERSHIP_VERSION
                from src.publication.digest_quality_diagnostics import DIGEST_DIAGNOSTICS_VERSION
                from src.publication.narrative_contract import DIGEST_NARRATIVE_PROMPT_VERSION
                from src.publication.policies import (
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
                    "narrative_contract_prompt_version": writer_config.get(
                        "narrative_contract_prompt_version", DIGEST_NARRATIVE_PROMPT_VERSION
                    ),
                    "composition_policy_version": COMPOSITION_POLICY_VERSION,
                    "composition_membership_version": DIGEST_COMPOSITION_MEMBERSHIP_VERSION,
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
                        validate_digest_narrative,
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
                    writer = DigestNarrativeWriter(provider=writer_provider)
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

                        from src.publication.digest_coverage import build_digest_coverage_trace
                        from src.publication.digest_narrative import sanitize_digest_narrative_draft
                        from src.publication.digest_quality_diagnostics import (
                            DigestCheckStatus,
                            audit_rendered_digest,
                        )
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

                        def _evaluate_candidate(candidate: Any) -> tuple[Any, Any, Any, Any]:
                            candidate = sanitize_digest_narrative_draft(candidate)
                            validation = validate_digest_narrative(
                                candidate,
                                plan,
                                support_text_by_id=support_text_index,
                                situation_plan=presentation_plan.city_situation,
                                allowed_context_terms=allowed_digest_terms,
                                all_known_draft_supports=all_draft_support_texts,
                            )
                            coverage = build_digest_coverage_trace(
                                presentation_plan,
                                candidate,
                                plan,
                            )
                            artifact = renderer.render_grouped_digest_artifact(
                                frozen,
                                snapshot_at=run.snapshot_at,
                                timezone_name=getattr(self.config.settings, "timezone", "UTC"),
                                narrative_draft=candidate,
                                presentation_plan=presentation_plan,
                            )
                            audit = audit_rendered_digest(
                                artifact,
                                candidate,
                                evidence_dict,
                                presentation_plan,
                                coverage,
                                narrative_validation=validation,
                            )
                            return validation, coverage, artifact, audit

                        draft_cand = sanitize_digest_narrative_draft(draft_cand)
                        val_res, coverage_trace, rendered_artifact, rendered_audit = (
                            _evaluate_candidate(draft_cand)
                        )
                        if val_res.not_evaluated:
                            logger.info(
                                "digest narrative has semantic bindings explicitly marked NOT_EVALUATED: %s",
                                list(val_res.not_evaluated)[:10],
                            )

                        repair_findings = list(val_res.violations)
                        for check in rendered_audit.checks:
                            if check.status == DigestCheckStatus.FAIL:
                                repair_findings.append(
                                    f"DIGEST_QUALITY:{check.code}: {check.message} {check.text}".strip()
                                )
                        for warning in rendered_audit.prose_audit.warnings:
                            repair_findings.append(
                                f"STYLE_OBSERVATION:{warning.code}: {warning.message}"
                            )
                        repair_findings = list(dict.fromkeys(repair_findings))
                        repair_used = False
                        digest_repair_max_calls = 1
                        recompose_block_ids: tuple[str, ...] = ()
                        if repair_findings:
                            repair_findings.append(
                                "EDITORIAL_CONSTRAINT: Preserve every useful PUBLISH community_report as a faithfully attributed local report. Do not remove it or weaken its wording merely because it has one source or lacks official confirmation."
                            )
                            affected_item_ids = tuple(
                                dict.fromkeys(
                                    item.item_id
                                    for warning in rendered_audit.prose_audit.warnings
                                    for block in draft_cand.blocks
                                    if block.block_id == warning.block_id
                                    for index, item in enumerate(block.items)
                                    if index == warning.item_index and item.item_id
                                )
                            )
                            if not affected_item_ids:
                                affected_item_ids = tuple(
                                    item.item_id
                                    for block in draft_cand.blocks
                                    for item in block.items
                                    if item.item_id
                                )
                            recompose_block_ids = tuple(
                                dict.fromkeys(
                                    w.block_id
                                    for w in rendered_audit.prose_audit.warnings
                                    if w.code
                                    in {"OVERLONG_SYNTHESIS", "FRAGMENTED_SERVICE_REPORTS"}
                                )
                            )
                            if recompose_block_ids:
                                affected_item_ids = tuple(
                                    dict.fromkeys(
                                        item.item_id
                                        for warning in rendered_audit.prose_audit.warnings
                                        if warning.code
                                        in {"OVERLONG_SYNTHESIS", "FRAGMENTED_SERVICE_REPORTS"}
                                        and warning.block_id in recompose_block_ids
                                        and warning.item_index is not None
                                        for block in draft_cand.blocks
                                        if block.block_id == warning.block_id
                                        and warning.item_index < len(block.items)
                                        for item in (block.items[warning.item_index],)
                                        if item.item_id
                                    )
                                )
                            repair_checkpoint = (
                                draft_cand,
                                val_res,
                                coverage_trace,
                                rendered_artifact,
                                rendered_audit,
                            )
                            from src.publication.digest_editor import (
                                DigestEditor,
                                DigestRecompositionError,
                            )

                            editor = DigestEditor(provider=writer_provider)
                            structural_warning_codes = {
                                "OVERLONG_SYNTHESIS",
                                "FRAGMENTED_SERVICE_REPORTS",
                            }
                            repair_call_limit = 2 if recompose_block_ids else 1
                            digest_repair_max_calls = repair_call_limit
                            recomposition_feedback = ""
                            for repair_call in range(repair_call_limit):
                                attempt_findings = list(repair_findings)
                                if repair_call:
                                    retry_feedback = (
                                        "The previous response was rejected: "
                                        + recomposition_feedback
                                        if recomposition_feedback
                                        else "The previous recomposition did not resolve the "
                                        "flagged structural finding."
                                    )
                                    attempt_findings.append(
                                        "EDITORIAL_CONSTRAINT: "
                                        + retry_feedback
                                        + " Regroup the targeted facts into fewer reader items "
                                        "and preserve every fact and summary unit exactly once."
                                    )
                                edit_att_id = await observer.attempt_started(
                                    "repair",
                                    metadata={
                                        "digest_implementation_versions": digest_implementation_versions,
                                        "subkind": "digest_editor_combined_repair",
                                        "repair_call": repair_call + 1,
                                        "finding_count": len(attempt_findings),
                                        "finding_codes": [
                                            str(value).split(":", 2)[1]
                                            for value in attempt_findings
                                            if ":" in str(value)
                                        ][:20],
                                    },
                                )
                                try:
                                    async with asyncio.timeout(narrative_timeout):
                                        repaired_draft = await editor.polish_and_compress(
                                            repair_checkpoint[0],
                                            plan=plan,
                                            evidence=evidence_dict,
                                            max_chars=3600,
                                            model=getattr(
                                                self.config.settings, "openai_model", None
                                            )
                                            or getattr(self.config.settings, "ai_model", None),
                                            violations=attempt_findings,
                                            target_item_ids=affected_item_ids,
                                            recompose_block_ids=recompose_block_ids,
                                        )
                                    candidate = sanitize_digest_narrative_draft(repaired_draft)
                                    (
                                        candidate_validation,
                                        candidate_coverage,
                                        candidate_artifact,
                                        candidate_audit,
                                    ) = _evaluate_candidate(candidate)
                                    unresolved_recomposition = tuple(
                                        dict.fromkeys(
                                            warning.code
                                            for warning in candidate_audit.prose_audit.warnings
                                            if warning.code in structural_warning_codes
                                            and warning.block_id in recompose_block_ids
                                        )
                                    )
                                    repair_accepted = (
                                        candidate_validation.is_valid
                                        and candidate_audit.is_publishable
                                        and candidate_coverage.story_coverage >= 1.0
                                        and candidate_coverage.material_fact_coverage >= 1.0
                                        and (
                                            not recompose_block_ids
                                            or (
                                                candidate != repair_checkpoint[0]
                                                and not unresolved_recomposition
                                            )
                                        )
                                    )
                                    await observer.attempt_finished(
                                        edit_att_id,
                                        "succeeded" if repair_accepted else "failed",
                                        error_kind=(
                                            None
                                            if repair_accepted
                                            else "digest_editor_combined_repair_unresolved"
                                        ),
                                        metadata={
                                            "repair_used": repair_accepted,
                                            "unresolved_recomposition_codes": list(
                                                unresolved_recomposition
                                            ),
                                            "validation": {
                                                "is_valid": candidate_validation.is_valid,
                                                "scope": "implemented_hard_checks_only",
                                                "not_evaluated": list(
                                                    candidate_validation.not_evaluated
                                                ),
                                            },
                                            "quality_audit": candidate_audit.as_metadata(),
                                        },
                                    )
                                    if repair_accepted:
                                        draft_cand = candidate
                                        val_res = candidate_validation
                                        coverage_trace = candidate_coverage
                                        rendered_artifact = candidate_artifact
                                        rendered_audit = candidate_audit
                                        repair_used = True
                                        break
                                except Exception as edit_exc:
                                    if isinstance(edit_exc, DigestRecompositionError):
                                        recomposition_feedback = str(edit_exc)
                                    await observer.attempt_finished(
                                        edit_att_id,
                                        "failed",
                                        error_kind="digest_editor_combined_repair_exception",
                                        metadata={"error_message": str(edit_exc)},
                                    )
                                    logger.warning(
                                        "DigestEditor combined repair failed: %s", edit_exc
                                    )

                            if recompose_block_ids and not repair_used:
                                (
                                    draft_cand,
                                    val_res,
                                    coverage_trace,
                                    rendered_artifact,
                                    rendered_audit,
                                ) = repair_checkpoint

                        blocking_findings = [
                            check.code for check in rendered_audit.blocking_failures
                        ]
                        if not val_res.is_valid:
                            blocking_findings.extend(val_res.violations)
                        if any(
                            warning.code in {"OVERLONG_SYNTHESIS", "FRAGMENTED_SERVICE_REPORTS"}
                            and warning.block_id in recompose_block_ids
                            for warning in rendered_audit.prose_audit.warnings
                        ):
                            blocking_findings.append("DIGEST_RECOMPOSITION_UNRESOLVED")
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
                                "max_calls": digest_repair_max_calls,
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
