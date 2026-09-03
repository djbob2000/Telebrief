"""Publication generation service producing immutable publications and generation attempt history (Plan 4 Task 5)."""

from __future__ import annotations

import datetime as dt
import logging
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
from src.publication.editorializer import DigestEditorializer
from src.publication.errors import ArticlePublicationRejected
from src.publication.models import Publication
from src.publication.policies import (
    SUPPORTED_ARTICLE_COVERAGE_PLAN_VERSIONS,
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
        editorializer: DigestEditorializer | None = None,
        rubric_classifier: DigestRubricClassifier | None = None,
    ) -> None:
        from src.config_loader import load_config

        self.uow = uow
        self.config = config or load_config()
        self.repo = repo or PublicationRepository()
        self.adapter = adapter or KnowledgeEditorialAdapter(uow=uow, repo=self.repo)
        self.generator = generator or ArticleGenerator(config=self.config, logger=logger)
        self.editorializer = editorializer or DigestEditorializer(config=self.config)

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

            # Validate frozen writer policy semantics for article publications
            if run.publication_type in ("daily_article", "article"):
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

        try:
            if run.publication_type in DIGEST_PUBLICATION_TYPES:
                if frozen.analysis.cards and not has_event_first:
                    try:
                        editorialized_cards = await self.editorializer.editorialize(
                            cards=frozen.analysis.cards,
                            bundle=frozen.writer_bundle,
                            attempt_observer=observer,
                        )
                        frozen = replace(
                            frozen,
                            analysis=replace(frozen.analysis, cards=editorialized_cards),
                        )
                    except Exception as exc:
                        logger.warning(
                            "digest editorializer failed (%s: %s); falling back to canonical cards",
                            type(exc).__name__,
                            exc,
                        )

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
                narrative_mode = (
                    getattr(pub_edit, "digest_narrative_mode", "deterministic")
                    if pub_edit
                    else "deterministic"
                )

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
                )

                if (
                    narrative_mode in ("single_call", "journalistic")
                    and run.publication_type != "digest_channel"
                    and frozen.analysis.cards
                ):
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
                    plan = plan_digest_narrative_blocks(
                        cards=detail_cards,
                        evidence=evidence_dict,
                        rubrics=renderer.rubrics,
                        max_cards_per_block=max_cards,
                        presentation_plan=presentation_plan,
                    )

                    writer_provider = getattr(self.generator, "provider", None)
                    writer = DigestNarrativeWriter(provider=writer_provider)
                    att_id = await observer.attempt_started(
                        "writer",
                        metadata={
                            "subkind": f"digest_narrative_{narrative_mode}",
                            "block_count": len(plan.blocks),
                            "card_count": len(detail_cards),
                            "situation_group_count": len(presentation_plan.city_situation.groups),
                        },
                    )
                    try:
                        if narrative_mode == "journalistic":
                            city_name = (
                                getattr(self.config.settings, "edition_name", None)
                                or getattr(frozen, "edition_name", None)
                                or "Бердянск"
                            )
                            date_str = (
                                run.snapshot_at or dt.datetime.now(dt.timezone.utc)
                            ).strftime("%d.%m.%Y")
                            (
                                journalistic_text,
                                draft_cand,
                            ) = await writer.generate_journalistic_digest(
                                city=city_name,
                                date_str=date_str,
                                cards=detail_cards or frozen.analysis.cards,
                                evidence=evidence_dict,
                                model=getattr(self.config.settings, "openai_model", None)
                                or getattr(self.config.settings, "ai_model", None),
                                max_chars=3900,
                                target_chars=3500,
                            )
                            title = f"Дайджест: {city_name} · {date_str}"
                            lead = ""
                            body = journalistic_text
                            narrative_draft = draft_cand
                            final_digest_draft = draft_cand
                            from src.publication.digest_coverage import (
                                build_digest_coverage_trace,
                            )

                            coverage_trace = build_digest_coverage_trace(
                                presentation_plan,
                                final_digest_draft,
                                plan,
                            )
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
                                "deterministic_digest_fallback_used": False,
                                "digest_presentation_plan": presentation_plan.to_audit_dict(),
                                "digest_coverage_trace": coverage_trace.to_dict(),
                            }
                            await observer.attempt_finished(
                                att_id,
                                "succeeded",
                                metadata={
                                    "mode": "journalistic",
                                    "final_length": len(body),
                                    "single_message_safe": len(body) <= 4096,
                                    **coverage_meta,
                                },
                            )
                        else:
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
                            )
                            support_text_index = build_digest_support_text_index(
                                evidence=evidence_dict,
                                cards=frozen.analysis.cards,
                                frozen_input=frozen,
                            )
                            all_draft_support_texts = list(support_text_index.values())
                            import datetime as _dt
                            import re as _re

                            edition_tokens: set[str] = {
                                tok.lower()
                                for st in all_draft_support_texts
                                for tok in _re.findall(r"[\w-]+", st)
                                if len(tok) >= 2
                            }
                            window_terms: set[str] = set()
                            if getattr(run, "snapshot_at", None):
                                s_at = run.snapshot_at
                                months_ru = [
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
                                ]
                                for offset in range(3):
                                    cur_d = (s_at - _dt.timedelta(days=offset)).date()
                                    window_terms.add(cur_d.strftime("%d.%m"))
                                    window_terms.add(str(cur_d.day))
                                    m_name = months_ru[cur_d.month - 1]
                                    window_terms.add(m_name)
                                    window_terms.add(f"{cur_d.day} {m_name}")
                            allowed_digest_terms = tuple(edition_tokens | window_terms)

                            val_res = validate_digest_narrative(
                                draft_cand,
                                plan,
                                support_text_by_id=support_text_index,
                                situation_plan=presentation_plan.city_situation,
                                allowed_context_terms=allowed_digest_terms,
                                all_known_draft_supports=all_draft_support_texts,
                            )
                            if val_res.is_valid:
                                narrative_draft = draft_cand
                                final_digest_draft = draft_cand

                                title, lead, body = renderer.render_grouped_digest(
                                    frozen,
                                    snapshot_at=run.snapshot_at,
                                    narrative_draft=narrative_draft,
                                    presentation_plan=presentation_plan,
                                )
                                from src.publication.digest_coverage import (
                                    build_digest_coverage_trace,
                                )
                                from src.publication.digest_quality_diagnostics import (
                                    audit_digest_prose_quality,
                                )

                                quality_audit = audit_digest_prose_quality(
                                    draft_cand,
                                    evidence=evidence_dict,
                                    presentation_plan=presentation_plan,
                                )
                                coverage_trace = build_digest_coverage_trace(
                                    presentation_plan,
                                    final_digest_draft,
                                    plan,
                                )
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
                                    "deterministic_digest_fallback_used": False,
                                    "digest_presentation_plan": presentation_plan.to_audit_dict(),
                                    "digest_coverage_trace": coverage_trace.to_dict(),
                                }
                                await observer.attempt_finished(
                                    att_id,
                                    "succeeded",
                                    metadata={
                                        "validation": {"is_valid": True},
                                        "block_count": len(draft_cand.blocks),
                                        "situation_item_count": len(draft_cand.situation_items),
                                        "prose_quality_audit": quality_audit.as_metadata(),
                                        **coverage_meta,
                                    },
                                )

                            else:
                                await observer.attempt_finished(
                                    att_id,
                                    "failed",
                                    error_kind="digest_narrative_validation_failed",
                                    metadata={
                                        "error_message": "; ".join(val_res.violations[:5]),
                                        "violations": list(val_res.violations),
                                    },
                                )

                    except Exception as exc:
                        logger.warning(
                            "digest narrative synthesis failed (%s: %s); falling back to deterministic",
                            type(exc).__name__,
                            exc,
                        )

                        await observer.attempt_finished(
                            att_id,
                            "failed",
                            error_kind="digest_narrative_synthesis_failed",
                            metadata={"error_message": str(exc)},
                        )

                if narrative_draft is None:
                    att_id = await observer.attempt_started(
                        "story_renderer_fallback", metadata={"renderer": run.publication_type}
                    )
                    if run.publication_type == "digest_channel":
                        title, lead, body = renderer.render_channel_digest(
                            frozen, snapshot_at=run.snapshot_at
                        )
                        await observer.attempt_finished(att_id, "succeeded")
                    else:
                        from src.publication.digest_coverage import (
                            build_digest_coverage_trace,
                        )
                        from src.publication.digest_narrative import (
                            build_deterministic_digest_draft,
                            build_digest_support_text_index,
                            plan_digest_narrative_blocks,
                            validate_digest_narrative,
                        )
                        from src.publication.errors import DigestCoverageInvariantError

                        support_text_index = build_digest_support_text_index(
                            evidence=evidence_dict,
                            cards=frozen.analysis.cards,
                            frozen_input=frozen,
                        )
                        all_draft_support_texts = list(support_text_index.values())
                        import datetime as _dt
                        import re as _re

                        det_edition_tokens: set[str] = {
                            tok.lower()
                            for st in all_draft_support_texts
                            for tok in _re.findall(r"[\w-]+", st)
                            if len(tok) >= 2
                        }
                        det_window_terms: set[str] = set()
                        if getattr(run, "snapshot_at", None):
                            s_at = run.snapshot_at
                            months_ru = [
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
                            ]
                            for offset in range(3):
                                cur_d = (s_at - _dt.timedelta(days=offset)).date()
                                det_window_terms.add(cur_d.strftime("%d.%m"))
                                det_window_terms.add(str(cur_d.day))
                                m_name = months_ru[cur_d.month - 1]
                                det_window_terms.add(m_name)
                                det_window_terms.add(f"{cur_d.day} {m_name}")
                        allowed_digest_terms = tuple(det_edition_tokens | det_window_terms)

                        final_digest_draft = build_deterministic_digest_draft(
                            cards=frozen.analysis.cards,
                            evidence=evidence_dict,
                            rubrics=renderer.rubrics,
                            presentation_plan=presentation_plan,
                            allowed_context_terms=allowed_digest_terms,
                            all_known_draft_supports=all_draft_support_texts,
                        )

                        detail_cards = [
                            c
                            for c in frozen.analysis.cards
                            if c.id in presentation_plan.detail_story_ids
                        ]
                        max_cards = getattr(pub_edit, "digest_narrative_max_cards_per_block", 6)
                        det_plan = plan_digest_narrative_blocks(
                            cards=detail_cards,
                            evidence=evidence_dict,
                            rubrics=renderer.rubrics,
                            max_cards_per_block=max_cards,
                            presentation_plan=presentation_plan,
                        )
                        det_val = validate_digest_narrative(
                            final_digest_draft,
                            det_plan,
                            support_text_by_id=support_text_index,
                            situation_plan=None,
                            allowed_context_terms=allowed_digest_terms,
                            all_known_draft_supports=all_draft_support_texts,
                        )
                        if not det_val.is_valid:
                            raise DigestCoverageInvariantError(
                                f"deterministic digest draft failed validation: {det_val.violations}"
                            )

                        coverage_trace = build_digest_coverage_trace(
                            presentation_plan,
                            final_digest_draft,
                            det_plan,
                        )
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
                            "deterministic_digest_fallback_used": True,
                            "digest_presentation_plan": presentation_plan.to_audit_dict(),
                            "digest_coverage_trace": coverage_trace.to_dict(),
                        }

                        title, lead, body = renderer.render_grouped_digest(
                            frozen,
                            snapshot_at=run.snapshot_at,
                            narrative_draft=final_digest_draft,
                            presentation_plan=presentation_plan,
                        )
                        await observer.attempt_finished(att_id, "succeeded", metadata=coverage_meta)

            else:
                title, lead, body = await self.generator.generate_from_frozen_input(
                    frozen, attempt_observer=observer
                )
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
