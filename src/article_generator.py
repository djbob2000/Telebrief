"""Daily Story Card editorial pipeline with safe degraded publication paths."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from src.ai_providers import (
    AIProvider,
    ProviderCascadeError,
    create_provider,
    ensure_provider_cascade,
)
from src.city_context import CityContextResolver, CityProfileError, StoryContextEnricher
from src.collector import Message
from src.config_loader import Config, PublicationEditorialConfig, SourceRoleResolver
from src.editorial_analysis import (
    ContextSizeRejectedError,
    EditorialAnalysisError,
    EditorialAnalyzer,
    is_large_bundle_for_rescue,
)
from src.editorial_audit import (
    FactCheckResult,
    FactCheckUnavailableError,
    LightFactChecker,
    deterministic_preflight,
    publication_copy_preflight,
)
from src.editorial_fallback import (
    DeterministicStoryCardBuilder,
    NoSubstantiveMaterialError,
    StoryCardRenderer,
)
from src.editorial_input import EditorialInputBuilder
from src.editorial_models import EditorialAnalysis, PreparedBundle
from src.editorial_writer import ArticleDraft, EditorialWriter
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_length import (
    ArticleLengthProfile,
    derive_article_length_profile,
)
from src.publication.article_models import StructuredArticleDraft
from src.publication.article_quote_allowlist import build_article_quote_allowlist
from src.publication.article_validator import validate_article_draft
from src.publication.narrative_contract import build_article_narrative_contract


class UnsafeDraftError(RuntimeError):
    """Raised when an unresolved high-risk fragment is central to the draft."""


class NoSubstantiveEditorialError(NoSubstantiveMaterialError):
    """Valid editorial analysis found no publishable local story."""


def _load_skill_instructions(path: str) -> str:
    """Load the configured skill once, stripping optional YAML frontmatter."""
    skill_path = Path(path)
    if not skill_path.is_absolute() and not skill_path.exists():
        repo_root = Path(__file__).resolve().parent.parent
        candidate = repo_root / path
        if candidate.exists():
            skill_path = candidate
    if not skill_path.exists():
        raise FileNotFoundError(f"Article skill/prompt template not found: {path}")
    content = skill_path.read_text(encoding="utf-8").strip()
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2].strip()
    return content


RUN_DEBUG_ARTIFACTS = (
    "prepared_input.txt",
    "story_cards.json",
    "editorial_analysis_raw.txt",
    "writer_bundle.txt",
    "writer_input.txt",
    "writer_bundle.json",
    "writer_draft.json",
    "story_card_fallback.md",
    "fallback_reason.txt",
    "fallback_story_cards.json",
    "fact_check_raw.txt",
    "fact_check_initial.json",
    "fact_check_final.json",
    "fact_check.json",
    "fact_check_failure.json",
    "final_article.md",
)


def _ground_draft_in_coverage_plan(
    parsed: dict[str, Any],
    coverage_plan: Any,
    article_ctx: Any | None = None,
) -> dict[str, Any]:
    """Ensure draft sections and paragraphs inherit valid evidence provenance from coverage plan."""
    if not isinstance(parsed, dict) or coverage_plan is None:
        return parsed

    import re

    from src.publication.article_claims import _stem

    tok_re = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)

    # Precompute stems for all available supports in context
    support_stems: dict[str, set[str]] = {}
    curr_pub_sups: list[str] = []
    support_by_id = getattr(article_ctx, "support_by_id", {}) if article_ctx else {}

    if article_ctx is not None:
        for s in getattr(article_ctx, "supports", ()):
            sid = getattr(s, "support_id", None)
            if not sid:
                continue
            if (
                getattr(s, "publication_use", "") == "PUBLISH"
                and getattr(s, "temporal_role", "") == "CURRENT_WINDOW"
            ):
                curr_pub_sups.append(sid)
            full_t = f"{s.text or ''} {s.source_text or ''}"
            words = tok_re.findall(full_t)
            support_stems[sid] = {_stem(w.lower()) for w in words if len(w) >= 3}

    # 1. Title & lead support IDs (guarantee at least one PUBLISH CURRENT_WINDOW support)
    top_story_sups: list[str] = []
    stories = getattr(coverage_plan, "stories", ()) or ()
    for s in stories:
        prom = getattr(s, "prominence", "")
        if prom in ("DEVELOP", "lead_develop"):
            top_story_sups.extend(list(getattr(s, "support_ids", ()))[:5])
    if not top_story_sups and stories:
        top_story_sups = list(getattr(stories[0], "support_ids", ()))[:5]

    lead_sups = [sid for sid in top_story_sups if sid in curr_pub_sups]
    if not lead_sups:
        lead_sups = curr_pub_sups[:3] or top_story_sups[:3]

    if not parsed.get("title_support_ids") or not any(
        sid in curr_pub_sups for sid in parsed.get("title_support_ids", ())
    ):
        parsed["title_support_ids"] = lead_sups[:3]
    if support_stems and parsed.get("lead"):
        l_text = str(parsed["lead"])
        l_words = tok_re.findall(l_text)
        l_stems = {_stem(w.lower()) for w in l_words if len(w) >= 3}
        l_nums = set(re.findall(r"\b\d+\b", l_text))
        matched_lead_sups = []
        for sid in curr_pub_sups:
            s_stems = support_stems.get(sid, set())
            shared_st = l_stems & s_stems
            s_text = getattr(support_by_id.get(sid), "text", "")
            s_nums_sup = set(re.findall(r"\b\d+\b", s_text)) if s_text else set()
            shared_nums = l_nums & s_nums_sup
            if len(shared_st) >= 2 or (shared_st and shared_nums) or len(shared_nums) >= 2:
                matched_lead_sups.append(sid)
        all_l_sups = list(dict.fromkeys(lead_sups[:3] + matched_lead_sups))
        parsed["lead_support_ids"] = all_l_sups
        if not parsed.get("lead_claims"):
            l_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", l_text) if s.strip()]
            lead_claims_list = []
            for sent in l_sentences:
                s_words = tok_re.findall(sent)
                s_stems = {_stem(w.lower()) for w in s_words if len(w) >= 3}
                s_nums = set(re.findall(r"\b\d+\b", sent))
                matched_sent_sups = []
                for sid in all_l_sups:
                    sup_st = support_stems.get(sid, set())
                    shared_st = s_stems & sup_st
                    s_text = getattr(support_by_id.get(sid), "text", "")
                    s_nums_sup = set(re.findall(r"\b\d+\b", s_text)) if s_text else set()
                    shared_nums = s_nums & s_nums_sup
                    if len(shared_st) >= 2 or (shared_st and shared_nums) or len(shared_nums) >= 2:
                        matched_sent_sups.append(sid)
                lead_claims_list.append(
                    {
                        "text": sent,
                        "cited_support_ids": all_l_sups,
                    }
                )
            parsed["lead_claims"] = lead_claims_list
    elif not parsed.get("lead_support_ids") or not any(
        sid in curr_pub_sups for sid in parsed.get("lead_support_ids", ())
    ):
        parsed["lead_support_ids"] = lead_sups[:3]

    # 2. Sections & paragraphs
    raw_sections = parsed.get("sections") or []
    plan_sections = list(getattr(coverage_plan, "sections", ()))

    for idx, sec in enumerate(raw_sections):
        if not isinstance(sec, dict):
            continue
        matched_plan = plan_sections[idx] if idx < len(plan_sections) else None
        sec_sups: list[str] = []
        if matched_plan:
            sec_story_ids = {
                getattr(a, "story_id", "") for a in getattr(matched_plan, "story_assignments", ())
            }
            for s in stories:
                if getattr(s, "story_id", "") in sec_story_ids:
                    sec_sups.extend(getattr(s, "support_ids", ()))
            for a in getattr(matched_plan, "story_assignments", ()):
                sec_sups.extend(getattr(a, "primary_evidence_ids", ()))
                sec_sups.extend(getattr(a, "concrete_details", ()))
        if not sec_sups and stories:
            sec_sups = [sup for s in stories for sup in list(getattr(s, "support_ids", ()))]

        if support_by_id:
            sec_sups = [sid for sid in sec_sups if sid in support_by_id]
        sec_sups = list(dict.fromkeys(sec_sups))

        # Match heading supports based on heading text stems and numbers
        h_text = str(sec.get("heading") or "")
        matched_h_sups = []
        if support_stems and h_text:
            h_words = tok_re.findall(h_text)
            h_stems = {_stem(w.lower()) for w in h_words if len(w) >= 3}
            h_nums = set(re.findall(r"\b\d+\b", h_text))
            for sid, s_stems in support_stems.items():
                shared_st = h_stems & s_stems
                s_text = getattr(support_by_id.get(sid), "text", "")
                s_nums = set(re.findall(r"\b\d+\b", s_text)) if s_text else set()
                shared_nums = h_nums & s_nums
                if len(shared_st) >= 2 or (shared_st and shared_nums) or (h_nums and shared_nums):
                    matched_h_sups.append(sid)

        existing_h_sups = list(sec.get("heading_support_ids") or [])
        combined_h_sups = list(
            dict.fromkeys(existing_h_sups + matched_h_sups + sec_sups[:5] or lead_sups[:3])
        )
        if support_by_id:
            combined_h_sups = [sid for sid in combined_h_sups if sid in support_by_id]
        sec["heading_support_ids"] = combined_h_sups

        raw_paras = sec.get("paragraphs") or []
        grounded_paras: list[dict[str, Any]] = []
        for p in raw_paras:
            p_text = p if isinstance(p, str) else p.get("text", "")
            existing_cited = [] if isinstance(p, str) else list(p.get("cited_support_ids") or [])

            if support_stems:
                p_words = tok_re.findall(p_text)
                p_stems = {_stem(w.lower()) for w in p_words if len(w) >= 3}
                p_nums = set(re.findall(r"\b\d+\b", p_text))
                # Match supports that share at least 2 content stems, or share numbers + stem
                matched_sups = []
                for sid, s_stems in support_stems.items():
                    shared_stems = p_stems & s_stems
                    s_text = getattr(support_by_id.get(sid), "text", "")
                    s_nums = set(re.findall(r"\b\d+\b", s_text)) if s_text else set()
                    shared_nums = p_nums & s_nums
                    if (
                        len(shared_stems) >= 2
                        or (shared_stems and shared_nums)
                        or len(shared_nums) >= 2
                    ):
                        matched_sups.append(sid)
                combined_sups = list(dict.fromkeys(existing_cited + matched_sups + sec_sups))
            else:
                combined_sups = list(dict.fromkeys(existing_cited + sec_sups))

            if support_by_id:
                combined_sups = [sid for sid in combined_sups if sid in support_by_id]

            para_dict = {
                "text": p_text,
                "cited_support_ids": combined_sups,
            }
            if isinstance(p, dict) and "claims" in p:
                para_dict["claims"] = p["claims"]
            grounded_paras.append(para_dict)
        sec["paragraphs"] = grounded_paras

    return parsed


class ArticleGenerator:
    """Generate a readable article, repairing locally and never dumping raw messages."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        raw_provider: AIProvider = create_provider(
            provider_name=config.settings.ai_provider,
            logger=logger,
            openai_api_key=config.openai_api_key,
            openai_base_url=config.openai_base_url,
            anthropic_api_key=config.anthropic_api_key,
            google_api_key=config.google_api_key,
            google_api_keys=config.google_api_backup_keys,
            openrouter_api_key=config.openrouter_api_key,
            openrouter_base_url=config.openrouter_base_url,
            openrouter_model=config.openrouter_model,
            openrouter_model_2=getattr(config, "openrouter_model_2", ""),
            ollama_base_url=config.settings.ollama_base_url,
            api_timeout=config.settings.article.editorial_api_timeout,
            reasoning_effort=config.settings.reasoning_effort,
        )
        self.provider: AIProvider = ensure_provider_cascade(
            raw_provider, logger=logger, slot_name=config.settings.ai_provider
        )
        self.model = config.settings.ai_model
        self.output_language = config.settings.output_language
        skill_path = getattr(
            config.settings.article, "prompt_template", ".agents/skills/news-style/SKILL.md"
        )
        self.skill_instructions = _load_skill_instructions(skill_path)
        city_profile_path = getattr(
            config.settings.article, "city_profile_path", "data/city_profiles/berdyansk.yaml"
        )
        try:
            self.city_context_resolver: CityContextResolver | None = CityContextResolver.from_yaml(
                city_profile_path
            )
            self.story_context_enricher: StoryContextEnricher | None = StoryContextEnricher(
                self.city_context_resolver
            )
        except (CityProfileError, FileNotFoundError) as exc:
            self.logger.warning("City context profile unavailable: %s", exc)
            self.city_context_resolver = None
            self.story_context_enricher = None

        self.role_resolver = SourceRoleResolver(config.channels)
        self.input_builder = EditorialInputBuilder(
            self.role_resolver, city_context_resolver=self.city_context_resolver
        )
        reasoning_effort = getattr(config.settings, "reasoning_effort", None)
        article_temp = getattr(getattr(config.settings, "article", None), "temperature", None)
        self.analyzer = EditorialAnalyzer(
            self.provider,
            self.model,
            logger,
            max_output_tokens=config.settings.article.editorial_analysis_max_output_tokens,
            compact_max_output_tokens=(
                config.settings.article.editorial_analysis_compact_max_output_tokens
            ),
            output_language=self.output_language,
            reasoning_effort=reasoning_effort,
            temperature=article_temp,
        )
        self.writer = EditorialWriter(
            self.provider,
            self.model,
            self.skill_instructions,
            logger,
            max_output_tokens=config.settings.article.editorial_writer_max_output_tokens,
            output_language=self.output_language,
            reasoning_effort=reasoning_effort,
            temperature=article_temp,
        )
        self.fact_checker = LightFactChecker(
            self.provider,
            self.model,
            logger,
            max_output_tokens=config.settings.article.editorial_audit_max_output_tokens,
            repair_max_output_tokens=config.settings.article.editorial_repair_max_output_tokens,
            output_language=self.output_language,
            reasoning_effort=reasoning_effort,
            temperature=article_temp,
        )
        self.fallback_builder = DeterministicStoryCardBuilder()
        self.fallback_renderer = StoryCardRenderer(output_language=self.output_language)

        self.historical_retriever: Any | None = None
        try:
            from src.runtime import get_runtime

            runtime = get_runtime()
            if runtime is not None and getattr(config, "embedding", None) is not None:
                from src.embedding_providers import create_embedding_provider
                from src.historical_context import HistoricalContextRetriever

                emb_provider = create_embedding_provider(
                    config=config,
                    logger=logger,
                )
                self.historical_retriever = HistoricalContextRetriever(
                    uow=runtime.uow,
                    embedding_provider=emb_provider,
                    model=config.embedding.model,
                    dimensions=config.embedding.dimensions,
                )
        except Exception as exc:
            self.logger.debug("Historical context retriever not initialized: %s", exc)
            self.historical_retriever = None

    def _compose_system_prompt(self) -> str:
        """Compatibility helper exposing the single writer prompt owner."""
        return (
            f"{self.skill_instructions}\n\n"
            f"Write in the configured output language: {self.output_language}.\n"
            "Story Cards are reporting notes, not a sentence template. Combine, reorder, "
            "compress and connect supplied material naturally into 3–5 thematic chapters. "
            "Do not create a new independently verifiable fact absent from the cards and source material. "
            "Return strict JSON only with headline, lead, paragraphs, and sections."
        )

    @staticmethod
    def _parse_article_response(text: str) -> Tuple[str, str, str]:
        """Extract title, lead and Markdown body from an article."""
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        title = lines[0].removeprefix("# ").strip() if lines else "Редакционная заметка"
        lead = next((line for line in lines[1:] if not line.startswith("#")), "")
        return title, lead, text.strip()

    @staticmethod
    def _validate_model_response(text: str) -> None:
        """Keep the old public structural check for callers and tests."""
        deterministic_preflight(text)

    def _build_bundle(self, messages_by_channel: Dict[str, List[Message]]) -> PreparedBundle:
        total = sum(len(messages) for messages in messages_by_channel.values())
        if total == 0:
            raise ValueError("No messages provided for article generation")
        bundle = self.input_builder.build(messages_by_channel, max_chars=None)
        if not bundle.records:
            raise ValueError("No substantive source messages remain for article generation")
        self.logger.info(
            "Prepared complete editorial bundle: %d candidate messages from %d collected",
            bundle.candidate_count,
            bundle.total_messages,
        )
        return bundle

    async def _analyze(self, bundle: PreparedBundle) -> EditorialAnalysis:  # noqa: C901
        """Analyze full bundle with adaptive, non-duplicative recovery transitions."""
        # 1. Full Normal
        try:
            return await self.analyzer.analyze(bundle, compact=False)
        except EditorialAnalysisError as exc:
            self._log_cascade_failure(exc)
            return await self._handle_analysis_recovery(exc, bundle)

    def _log_cascade_failure(
        self, error: EditorialAnalysisError, decision: str | None = None
    ) -> None:
        summary = (
            ", ".join(f"{f.slot}:{f.kind}:{f.exception_type}" for f in error.slot_failures)
            or error.reason
            or "unknown"
        )
        if decision:
            self.logger.warning(
                "Editorial analysis provider cascade failed: slots=[%s] decision=%s",
                summary,
                decision,
            )
        else:
            self.logger.warning(
                "Editorial analysis provider cascade failed: slots=[%s]",
                summary,
            )

    async def _handle_analysis_recovery(
        self, error: EditorialAnalysisError, bundle: PreparedBundle
    ) -> EditorialAnalysis:
        failure_kinds = set(error.failure_kinds)
        is_token_budget = (
            "token_budget" in failure_kinds
            or error.reason == "token_budget"
            or self._is_output_shape_failure(error)
        )
        is_context_size = (
            "context_size" in failure_kinds
            or error.reason == "context_size"
            or isinstance(error, ContextSizeRejectedError)
        )
        is_pure_outage = bool(failure_kinds) and failure_kinds <= {
            "auth",
            "quota",
            "server",
            "timeout",
        }
        is_other = "other" in failure_kinds or not failure_kinds

        # Transition 1: token_budget -> Full Compact
        if is_token_budget:
            self._log_cascade_failure(error, decision="compact_full_bundle")
            try:
                return await self.analyzer.analyze(bundle, compact=True)
            except ContextSizeRejectedError:
                self.logger.warning(
                    "Compact editorial analysis exceeded model context; switching to batched compact"
                )
                return await self.analyzer.analyze_batched(bundle, compact=True)
            except EditorialAnalysisError as compact_exc:
                compact_kinds = set(compact_exc.failure_kinds)
                if "context_size" in compact_kinds or (
                    "other" in compact_kinds and is_large_bundle_for_rescue(bundle)
                ):
                    self._log_cascade_failure(compact_exc, decision="batched_compact")
                    return await self.analyzer.analyze_batched(bundle, compact=True)
                self._log_cascade_failure(compact_exc, decision="fallback")
                raise

        # Transition 2: context_size (or mixed with timeout/server/quota) -> Batched Normal
        if is_context_size:
            self._log_cascade_failure(error, decision="batching")
            return await self.analyzer.analyze_batched(bundle, compact=False)

        # Transition 3: Pure Outage -> Degraded Fallback
        if is_pure_outage:
            self._log_cascade_failure(error, decision="fallback_pure_outage")
            raise error

        # Transition 4: Other/unknown on large bundle -> One bounded batched rescue
        if is_other and is_large_bundle_for_rescue(bundle):
            self._log_cascade_failure(error, decision="batched_rescue")
            return await self.analyzer.analyze_batched(bundle, compact=False)

        # Default: Fallback
        self._log_cascade_failure(error, decision="fallback")
        raise error

    @staticmethod
    def _is_output_shape_failure(error: EditorialAnalysisError) -> bool:
        return error.stage in {
            "empty_response",
            "json_parse",
            "response_shape",
            "story_card_parse",
        }

    def _select_writer_bundle(
        self, analysis: EditorialAnalysis, bundle: PreparedBundle
    ) -> PreparedBundle:
        refs: list[str] = []
        for card in analysis.cards:
            for ref in sorted(card.all_source_refs()):
                if ref not in refs:
                    refs.append(ref)
        # The analyzer is instructed to keep refs representative, but do not silently
        # discard evidence if a valid card contains more than the normal 96-ref budget.
        return self.input_builder.select_records(bundle, refs, max_refs=max(96, len(refs)))

    async def _render_story_card_fallback(
        self, analysis: EditorialAnalysis, reason: str
    ) -> Tuple[str, str, str]:
        """Render validated AI Story Cards when the free-form writer is unavailable."""
        self.logger.warning("Using validated Story Card render: %s", reason)
        draft = self.fallback_renderer.render(analysis.cards)
        markdown = draft.to_markdown()
        deterministic_preflight(markdown)
        publication_copy_preflight(markdown)
        self._save_debug_artifact("story_card_fallback.md", markdown)
        return self._parse_article_response(markdown)

    async def _fallback(self, bundle: PreparedBundle, reason: str) -> Tuple[str, str, str]:
        self.logger.warning("Editorial pipeline entered degraded path: %s", reason)
        cards = self.fallback_builder.build(bundle)
        self.logger.info("Deterministic fallback built %d normalized Story Cards", len(cards))
        draft = self.fallback_renderer.render(cards)
        markdown = draft.to_markdown()
        deterministic_preflight(markdown)
        publication_copy_preflight(markdown)
        self._save_debug_artifact("fallback_reason.txt", reason)
        self._save_debug_artifact(
            "fallback_story_cards.json", {"cards": [card.to_dict() for card in cards]}
        )
        return self._parse_article_response(markdown)

    def _clear_debug_artifacts(self) -> None:
        """Clear stale editorial debug artifacts before a fresh generation run."""
        article_config = self.config.settings.article
        if not getattr(article_config, "save_debug_artifacts", False):
            return
        directory = Path(getattr(article_config, "debug_artifact_dir", "data/debug/editorial"))
        if not directory.exists():
            return
        for name in RUN_DEBUG_ARTIFACTS:
            target = directory / name
            if target.exists():
                try:
                    target.unlink()
                except Exception as exc:
                    self.logger.warning("Could not clear stale debug artifact %s: %s", name, exc)

    def _save_debug_artifact(self, filename: str, content: Any) -> None:
        """Persist opt-in diagnostics without affecting publication."""
        article_config = self.config.settings.article
        if not getattr(article_config, "save_debug_artifacts", False):
            return
        directory = Path(getattr(article_config, "debug_artifact_dir", "data/debug/editorial"))
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / filename
            if isinstance(content, str):
                path.write_text(content, encoding="utf-8")
            else:
                path.write_text(
                    json.dumps(content, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
        except Exception as exc:
            self.logger.warning(
                "Could not save debug artifact %s: %s", filename, type(exc).__name__
            )

    def _save_fact_check_result(self, filename: str, result: FactCheckResult) -> None:
        self._save_debug_artifact(
            filename,
            {
                "status": result.status,
                "systemic_problem": result.systemic_problem,
                "issues": [issue.to_dict() for issue in result.issues],
            },
        )

    def _save_fact_check_failure(self, exc: Exception) -> None:
        stage = getattr(self.fact_checker, "last_stage", "unknown") or "unknown"
        reason = getattr(self.fact_checker, "last_reason", str(exc)) or str(exc)
        chars = getattr(self.fact_checker, "last_response_chars", None)
        self._save_debug_artifact(
            "fact_check_failure.json",
            {
                "stage": stage,
                "reason": reason,
                "response_chars": chars,
                "error": str(exc),
            },
        )

    async def _run_local_repair_loop(
        self,
        draft: ArticleDraft,
        result: FactCheckResult,
        analysis: EditorialAnalysis,
        bundle: PreparedBundle,
    ) -> tuple[ArticleDraft, FactCheckResult | None]:
        """Run up to 2 targeted repair attempts on draft.

        Returns (repaired_draft, latest_result) or (current_draft, None) if fact-check became unavailable.
        If a repair candidate fails preflight, it is discarded and the previous valid draft is kept.
        """
        current = draft
        for _ in range(2):
            if result.status != "FIX":
                return current, result

            current_units = current.audit_units()
            blocking_units = {
                issue.unit_id: current_units[issue.unit_id].text
                for issue in result.issues
                if issue.severity == "fix"
                and issue.publication_blocking
                and issue.unit_id in current_units
            }

            candidate = await self.fact_checker.repair(current, result, analysis, bundle)
            try:
                deterministic_preflight(candidate.to_markdown())
                current = candidate
            except Exception as exc:
                self.logger.warning(
                    "Discarding structurally invalid local repair; keeping previous draft: %s",
                    type(exc).__name__,
                )
            try:
                result = await self.fact_checker.check(current, analysis, bundle)
                self._save_fact_check_result("fact_check_final.json", result)
                self._save_fact_check_result("fact_check.json", result)
            except FactCheckUnavailableError as exc:
                self._save_fact_check_result("fact_check_final.json", result)
                self._save_fact_check_result("fact_check.json", result)
                self._save_fact_check_failure(exc)
                if blocking_units:
                    post_repair_units = current.audit_units()
                    unmodified_blocking = [
                        uid
                        for uid, orig_text in blocking_units.items()
                        if uid in post_repair_units and post_repair_units[uid].text == orig_text
                    ]
                    if unmodified_blocking:
                        raise UnsafeDraftError(
                            f"fact check unavailable during repair with unmodified publication-blocking unit(s): {', '.join(unmodified_blocking)}"
                        ) from exc
                    self.logger.warning(
                        "Fact check unavailable during repair; publishing modified draft with prior blocking fixes"
                    )
                return current, None
            if result.status != "FIX":
                return current, result
        return current, result

    async def _repair_and_check(  # noqa: C901
        self,
        draft: ArticleDraft,
        analysis: EditorialAnalysis,
        bundle: PreparedBundle,
        historical_background: str = "",
    ) -> ArticleDraft:
        try:
            result = await self.fact_checker.check(draft, analysis, bundle)
        except FactCheckUnavailableError as exc:
            stage = getattr(self.fact_checker, "last_stage", "unknown") or "unknown"
            reason = getattr(self.fact_checker, "last_reason", str(exc)) or str(exc)
            chars = getattr(self.fact_checker, "last_response_chars", None)
            self.logger.warning(
                "Light fact-check unavailable; publishing writer output: stage=%s reason=%s response_chars=%s",
                stage,
                reason,
                chars if chars is not None else "unknown",
            )
            if self.fact_checker.last_raw_response is not None:
                self._save_debug_artifact("fact_check_raw.txt", self.fact_checker.last_raw_response)
            self._save_fact_check_failure(exc)
            return draft

        if self.fact_checker.last_raw_response is not None:
            self._save_debug_artifact("fact_check_raw.txt", self.fact_checker.last_raw_response)

        self._save_fact_check_result("fact_check_initial.json", result)

        if result.status != "FIX":
            if result.status == "WARN":
                self.logger.warning(
                    "Publishing article with %d fact-check warning(s)", len(result.issues)
                )
            self._save_fact_check_result("fact_check_final.json", result)
            self._save_fact_check_result("fact_check.json", result)
            return draft

        # Phase 1: Local repair loop on initial draft (up to 2 passes)
        current, current_result = await self._run_local_repair_loop(draft, result, analysis, bundle)
        if current_result is None:
            return current

        if current_result.status != "FIX":
            if current_result.status == "WARN":
                self.logger.warning(
                    "Publishing article with %d fact-check warning(s)", len(current_result.issues)
                )
            self._save_fact_check_result("fact_check_final.json", current_result)
            self._save_fact_check_result("fact_check.json", current_result)
            return current

        # Phase 2: If still FIX and needs regeneration (systemic_problem or blocking fixes), escalate to ONE feedback-guided regeneration
        if current_result.needs_regeneration:
            self.logger.warning(
                "Fact-check requires regeneration (systemic=%s, blocking=%s); regenerating once with audit feedback",
                current_result.systemic_problem,
                current_result.has_blocking_fixes,
            )
            blocking_before_regeneration = current_result.has_blocking_fixes
            try:
                regenerated = await self.writer.write(
                    analysis,
                    bundle,
                    revision_feedback=current_result,
                    historical_background=historical_background,
                )
                deterministic_preflight(regenerated.to_markdown())
            except Exception as exc:
                if blocking_before_regeneration:
                    raise UnsafeDraftError(
                        f"feedback-guided regeneration failed with prior blocking fix: {type(exc).__name__}"
                    ) from exc
                self.logger.warning(
                    "Feedback-guided regeneration failed for non-blocking draft; preserving previous draft: %s",
                    type(exc).__name__,
                )
                regenerated = None

            if regenerated is not None:
                try:
                    regenerated_check = await self.fact_checker.check(regenerated, analysis, bundle)
                    self._save_fact_check_result("fact_check_final.json", regenerated_check)
                    self._save_fact_check_result("fact_check.json", regenerated_check)
                except FactCheckUnavailableError as exc:
                    self._save_fact_check_result("fact_check_final.json", current_result)
                    self._save_fact_check_result("fact_check.json", current_result)
                    self._save_fact_check_failure(exc)
                    self.logger.warning(
                        "Fact check unavailable after successful regeneration; publishing regenerated draft: %s",
                        exc,
                    )
                    return regenerated

                if regenerated_check.status != "FIX":
                    if regenerated_check.status == "WARN":
                        self.logger.warning(
                            "Publishing regenerated article with %d fact-check warning(s)",
                            len(regenerated_check.issues),
                        )
                    return regenerated

                # Phase 3: Run local repair loop on regenerated draft (up to 2 passes)
                current, current_result = await self._run_local_repair_loop(
                    regenerated,
                    regenerated_check,
                    analysis,
                    bundle,
                )
                if current_result is None:
                    return current
                if current_result.status != "FIX":
                    if current_result.status == "WARN":
                        self.logger.warning(
                            "Publishing regenerated article with %d fact-check warning(s)",
                            len(current_result.issues),
                        )
                    return current

        # Phase 4: Enforce publication gate on final draft
        if current_result is not None and current_result.status == "FIX":
            current = self._enforce_publication_gate(current, current_result)

        return current

    def _enforce_publication_gate(
        self, draft: ArticleDraft, result: FactCheckResult
    ) -> ArticleDraft:
        blocking = [
            issue
            for issue in result.issues
            if issue.severity == "fix" and issue.publication_blocking
        ]
        if blocking:
            safe_ids = ", ".join(f"{issue.unit_id}:{issue.code}" for issue in blocking)
            raise UnsafeDraftError(f"unresolved publication-blocking FIX remains: {safe_ids}")

        non_blocking = [
            issue
            for issue in result.issues
            if issue.severity == "fix" and not issue.publication_blocking
        ]
        if non_blocking:
            safe_ids = ", ".join(f"{issue.unit_id}:{issue.code}" for issue in non_blocking)
            self.logger.warning(
                "Publishing prose with %d unresolved non-blocking editorial FIX(s): %s",
                len(non_blocking),
                safe_ids,
            )

        return draft

    async def generate_from_frozen_input(
        self,
        frozen_input: Any,
        attempt_observer: Any | None = None,
    ) -> Tuple[str, str, str]:
        """Generate article directly from a sealed FrozenEditorialInput."""
        if (
            hasattr(frozen_input, "analysis")
            and frozen_input.analysis is not None
            and getattr(frozen_input.analysis, "article_context", None) is not None
        ):
            return await self.generate_from_event_article_context(
                frozen_input.analysis.article_context,
                attempt_observer=attempt_observer,
            )

        return await self.generate_from_analysis_and_bundle(
            analysis=frozen_input.analysis,
            writer_bundle=frozen_input.writer_bundle,
            attempt_observer=attempt_observer,
        )

    def _build_event_article_system_prompt(
        self,
        length_profile: ArticleLengthProfile | None = None,
    ) -> str:
        """Compose the Event-First article prompt from safety and narrative newsroom contracts."""
        narrative_contract = build_article_narrative_contract(
            output_language=self.output_language,
            length_profile=length_profile,
        )
        return f"""Вы — опытный выпускающий редактор и автор регионального издания.
Ваша задача — написать связную, объективную, детальную и увлекательную городскую хронику (вечерний лонгрид) на русском языке на основе проверенных фактов, оперативной хроники и сообщений.

{narrative_contract}

### Журналистский формат — Городская хроника (вечерний лонгрид):
1. ОБЪЁМ И ГЛУБИНА:
   - Статья должна представлять собой обстоятельное вечернее чтение городской хроники (ориентир 1200–1800 слов для насыщенных выпусков, 10–15 абзацев).
   - Категорически запрещен телеграфный стиль, сухое конспектирование или превращение статьи в короткую выжимку из 3 пунктов. Городская жизнь многогранна, и читатель хочет видеть полную картину дня.
2. СТРУКТУРА И ПЛАН ПОКРЫТИЯ (ARTICLE COVERAGE PLAN):
   - Следуйте тематическим разделам, предложенным в плане покрытия (обычно 3–6 разделов).
   - DEVELOP: ключевые сюжеты дня. Раскройте их в 1–2 подробных абзацах с сохранением микродеталей (районы, улицы, интервалы движения, стоимость, действия жителей и коммунальщиков).
   - WEAVE: сопутствующие темы городской жизни. Органично вплетайте их в канву соответствующего раздела.
   - BRIEF: короткие полезные городские сообщения. Обязательно упоминайте их хотя бы одним фактологическим предложением в конце раздела, чтобы обеспечить 100% покрытие плана. Ни одна запланированная тема не должна быть пропущена.
3. ТОНАЛЬНОСТЬ И ЯЗЫК:
   - Спокойный, уважительный, фактологический язык регионального журналиста.
   - Запрещены пустые клише и абстрактные формулировки («ситуация остается напряженной», «жители адаптируются к реалиям», «город живет в новых условиях»). Вместо абстракций приводите конкретные факты: кто что починил, как ходят автобусы, где есть вода и свет.
   - Заголовок (title): емкий, информативный газетный заголовок дня (например: «Бердянск в условиях перебоев с энергией: восстановительные работы, графики движения и городская хроника»). Запрещено делать заголовком вопросы жителей («Работает ли пенсионный фонд?»).

### ПРАВИЛО АНТИ-РЕКЛАМЫ И ОБОБЩЕНИЯ КОММЕРЧЕСКИХ ОБЪЯВЛЕНИЙ:
Объявления о частных услугах, коммерческих рейсах, платных клиниках и посредниках в городских чатах — это НЕ новости сами по себе. Их КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО цитировать дословно, копировать или превращать в каталоги услуг:

1. Частные перевозчики и междугородние рейсы:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО копировать цепочки городов маршрута («Киев — Днепр — Запорожье — Кривой Рог...»), конкретные даты выезда («25 сентября», «5 октября») и брать в кавычки рекламные фразы перевозчиков («едем», «есть места»).
   - КАК ОБЯЗАТЕЛЬНО ПИСАТЬ: Сожмите до одного краткого журналистского предложения о факте транспортного сообщения и уровне цен: «Сохраняются нерегулярные частные перевозки в сторону соседних регионов через Мариуполь и Мелитополь, стоимость мест остаётся высокой — от 400–450 долларов».

2. Посредники по банкам и пенсионным выплатам:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО перечислять списки коммерческих банков («ПриватБанк, А-Банк, Sense Bank...») и длинные списки рутинных операций («разблокировка карт, перевод пенсии, ЕЦП, виртуальные карты, актуализация данных...»).
   - КАК ОБЯЗАТЕЛЬНО ПИСАТЬ: Сожмите до одного предложения о социальном явлении: «В условиях перебоев с официальными сервисами в городе сохраняется спрос на частных посредников, помогающих с банковскими операциями и подтверждением пенсионных выплат».

3. Платные медицинские центры и клиники:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО перечислять длинные каталоги врачебных специальностей («ЛОР, кардиолог, дерматолог, терапевт, педиатр, гастроэнтеролог...») и публиковать графики работы лабораторий или забора крови.
   - КАК ОБЯЗАТЕЛЬНО ПИСАТЬ: Ограничьтесь кратким упоминанием: «Продолжает работу частный медцентр на ул. Тверской, 49, где ведут приём профильные врачи и работает диагностика».

4. ПРАВИЛО ОДНОГО ПРЕДЛОЖЕНИЯ ДЛЯ КОММЕРЧЕСКИХ ТЕМ:
   - Любая коммерческая сервисная тема (перевозчики, посредники, клиники) в статусе BRIEF должна занимать РОВНО ОДНО ёмкое журналистское предложение без перечислений и цитат.
   - Запрещены любые контакты, телефоны, ники в Telegram (@...), призывы обращаться за услугами.

### Обязательные правила журналистской точности:
1. Опирайтесь ТОЛЬКО на предоставленные в материалах факты и цитаты. Категорически ЗАПРЕЩЕНО выдумывать неподтвержденные детали, цифры, номера домов или адреса в других городах (Запорожье, Днепр, Киев, Мелитополь), если их нет в предоставленных карточках фактов. Не добавляйте внешние знания из своей памяти.
2. Не придумывайте официальных подтверждений, если источник — сообщение жителя. Передавайте статус честно: «по сообщениям жителей», «горожане отмечают», «как рассказывают жители».
3. Не раскрывайте техническую кухню сбора данных. Избегайте фраз «в чате», «участник чата написал», «в Telegram-канале». Используйте естественную городскую атрибуцию: «жители сообщают», «горожане обсуждают», «по наблюдениям жителей».
4. Прямая речь и живой голос города (QUOTE ALLOWLIST): сохраняйте характерные живые реплики горожан в кавычках («...»), точно воспроизводя подлинные слова жителей из предоставленных фактов и блока QUOTE ALLOWLIST. Это придаёт репортажу подлинность и живость. Если вы обобщаете мысль или пересказываете её своими словами — используйте естественную косвенную речь без кавычек.
5. Не вставляйте в текст технические ID вроде [story:...] или [SUPPORT...].
6. Язык статьи: {self.output_language}. Текст должен быть связным, грамотным, с живыми микродеталями.

### Формат ответа — строго валидный JSON:
{{
  "title": "Информативный газетный заголовок дня",
  "lead": "Вводный абзац статьи (2-3 предложения), обобщающий общую картину дня...",
  "sections": [
    {{
      "heading": "Название первой темы",
      "paragraphs": [
        "Первый содержательный абзац раздела...",
        "Второй содержательный абзац раздела..."
      ]
    }},
    {{
      "heading": "Название второй темы",
      "paragraphs": [
        "Первый содержательный абзац раздела..."
      ]
    }}
  ]
}}
"""

    async def generate_from_event_article_context(  # noqa: C901
        self,
        article_ctx: ArticleEditorialContext,
        attempt_observer: Any | None = None,
    ) -> Tuple[str, str, str]:
        """Synthesize long-form editorial article directly from ArticleEditorialContext in one LLM call."""
        if article_ctx is None:
            raise NoSubstantiveEditorialError("no article editorial context present")

        if (
            not article_ctx.support_index
            and not article_ctx.evidence_index
            and not article_ctx.operational_timeline
        ):
            raise NoSubstantiveEditorialError("no evidence or timeline present in article context")

        editorial_config = getattr(
            self.config.settings, "publication_editorial", PublicationEditorialConfig()
        )
        length_profile = derive_article_length_profile(article_ctx, editorial_config)
        develop_story_budget = max(0, min(3, length_profile.target_max_sections - 1))

        from src.publication.article_coverage import build_article_coverage_plan
        from src.publication.article_writer_context import render_article_writer_context

        coverage_plan = build_article_coverage_plan(
            article_ctx.story_cards,
            article_ctx,
            develop_story_budget=develop_story_budget,
        )
        context_str = render_article_writer_context(article_ctx, coverage_plan)
        system_prompt = self._build_event_article_system_prompt(length_profile=length_profile)
        user_prompt = f"РЕДАКЦИОННЫЙ МАТЕРИАЛ И ФАКТЫ:\n\n{context_str}"

        from src.publication.article_finalization import ArticleFinalizer

        max_writer_attempts = 1
        writer_draft: StructuredArticleDraft | None = None
        writer_error: Exception | None = None
        writer_attempt_id = 0
        current_user_prompt = user_prompt

        quote_allowlist = build_article_quote_allowlist(article_ctx)

        for writer_try in range(1, max_writer_attempts + 1):
            if attempt_observer is not None:
                writer_attempt_id = await attempt_observer.attempt_started(
                    "writer",
                    provider=self.config.settings.ai_provider,
                    model=self.model,
                    metadata={"attempt": writer_try},
                )

            try:
                article_temp = getattr(
                    getattr(self.config.settings, "article", None), "temperature", 0.3
                )
                response = await self.provider.chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": current_user_prompt},
                    ],
                    model=self.model,
                    temperature=article_temp,
                    max_tokens=self.config.settings.article.editorial_writer_max_output_tokens,
                    reasoning_effort=getattr(self.config.settings, "reasoning_effort", None),
                    response_format={"type": "json_object"},
                )
                raw_parsed = self._parse_event_article_response_json(response)
                parsed = _ground_draft_in_coverage_plan(raw_parsed, coverage_plan, article_ctx)
                candidate_draft = StructuredArticleDraft.from_dict(
                    parsed, quote_allowlist=quote_allowlist
                )
                candidate_val = validate_article_draft(
                    candidate_draft,
                    article_ctx,
                    config=editorial_config,
                    length_profile=length_profile,
                )
                if candidate_val.is_valid:
                    writer_draft = candidate_draft
                    writer_error = None
                    break
                else:
                    self.logger.warning(
                        "Writer attempt %d produced invalid draft: %s",
                        writer_try,
                        list(candidate_val.violations)[:5],
                    )
                    writer_draft = candidate_draft
                    if writer_try < max_writer_attempts:
                        blocking_issues = [iss for iss in candidate_val.issues if iss.blocking]
                        issue_lines = [
                            f"- [{iss.unit_id}] {iss.message}" for iss in blocking_issues[:6]
                        ]
                        violations_summary = "\n".join(issue_lines)
                        current_user_prompt = (
                            f"{user_prompt}\n\n"
                            f"### ВНИМАНИЕ: Предыдущий черновик отклонен валидатором со следующими ошибками:\n"
                            f"{violations_summary}\n\n"
                            f"ТРЕБОВАНИЯ ДЛЯ ИСПРАВЛЕНИЯ ЧЕРНОВИКА:\n"
                            f"1. Не придумывайте время (например, 21:00, 22:00) или даты, если их нет в тексте фактов.\n"
                            f"2. Не используйте причинные обороты ('из-за', 'вследствие', 'причиной стало').\n"
                            f"3. Не ставьте кавычки вокруг названий (школ, клубов, магазинов) — используйте косвенную речь без кавычек.\n"
                            f"4. Используйте ТОЛЬКО точные support_id из предоставленного списка фактов. Не выдумывайте ID."
                        )
            except Exception as exc:
                self.logger.warning(
                    "Event article writer execution attempt %d failed (%s: %s)",
                    writer_try,
                    type(exc).__name__,
                    exc,
                )
                if isinstance(exc, ProviderCascadeError) and writer_try >= max_writer_attempts:
                    raise
                writer_error = exc
                if writer_try >= max_writer_attempts:
                    break

        finalization_result = await ArticleFinalizer().finalize(
            writer_draft=writer_draft,
            writer_error=writer_error,
            writer_attempt_id=writer_attempt_id,
            context=article_ctx,
            coverage_plan=coverage_plan,
            editorial_config=editorial_config,
            length_profile=length_profile,
            attempt_observer=attempt_observer,
        )

        body = finalization_result.draft.render_markdown()
        return (finalization_result.draft.title, finalization_result.draft.lead, body)

    def _parse_event_article_response_json(self, response: str) -> dict[str, Any]:
        """Clean and parse JSON from article writer response string."""
        cleaned = (response or "").strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        if "{" in cleaned and "}" in cleaned:
            first_brace = cleaned.find("{")
            last_brace = cleaned.rfind("}")
            if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                cleaned = cleaned[first_brace : last_brace + 1]

        try:
            parsed = json.loads(cleaned, strict=False)
        except json.JSONDecodeError:
            # Repair common LLM formatting flaws
            fixed = re.sub(r",\s*([\}\]])", r"\1", cleaned)
            fixed = re.sub(r"\}\s*\{", "}, {", fixed)
            fixed = re.sub(r'"\s*\n\s*"', '",\n"', fixed)
            fixed = re.sub(r"\]\s*\{", "], {", fixed)
            fixed = re.sub(r"\}\s*\[", "}, [", fixed)
            parsed = json.loads(fixed, strict=False)

        if not isinstance(parsed, dict):
            raise ValueError("article writer response is not a JSON object")
        return parsed

    async def generate_from_analysis_and_bundle(  # noqa: C901
        self,
        analysis: EditorialAnalysis,
        writer_bundle: PreparedBundle,
        attempt_observer: Any | None = None,
        bundle_for_fallback: PreparedBundle | None = None,
    ) -> Tuple[str, str, str]:
        """Core writer and fallback pipeline from pre-built Story Cards and source bundle."""
        if not analysis.cards:
            self.logger.info(
                "Editorial analysis found no publishable local stories for the reporting period"
            )
            raise NoSubstantiveEditorialError(
                "no publishable local stories remain for reporting period"
            )

        fallback_bundle = bundle_for_fallback or writer_bundle

        if not writer_bundle.records:
            return await self._fallback(
                fallback_bundle, "editorial analysis returned no resolvable representative refs"
            )

        if self.story_context_enricher is not None:
            writer_bundle.story_contexts = self.story_context_enricher.enrich(
                analysis, writer_bundle
            )

        self.logger.info("Selected %d source records for writer", len(writer_bundle.records))
        self.logger.info(
            "Drafting article from %d Story Cards / %d source records",
            len(analysis.cards),
            len(writer_bundle.records),
        )
        self._save_debug_artifact("writer_bundle.txt", writer_bundle.prompt_text)
        self._save_debug_artifact("writer_input.txt", writer_bundle.prompt_text)
        self._save_debug_artifact(
            "writer_bundle.json",
            {
                "total_messages": writer_bundle.total_messages,
                "candidate_count": writer_bundle.candidate_count,
                "records": list(writer_bundle.records.keys()),
            },
        )

        writer_attempt_id = 0
        if attempt_observer is not None:
            writer_attempt_id = await attempt_observer.attempt_started(
                "writer",
                provider=self.config.settings.ai_provider,
                model=self.model,
            )

        historical_background_str = ""
        if self.historical_retriever is not None:
            try:
                hist_backgrounds = await self.historical_retriever.retrieve_for_stories(
                    analysis, edition_slug="berdyansk"
                )
                historical_background_str = self.historical_retriever.render_context(
                    hist_backgrounds
                )
            except Exception as exc:
                self.logger.warning("Historical background retrieval failed: %s", exc)

        try:
            draft = await self.writer.write(
                analysis,
                writer_bundle,
                historical_background=historical_background_str,
            )
            deterministic_preflight(draft.to_markdown())
            self._save_debug_artifact("writer_draft.json", draft.to_dict())
            if attempt_observer is not None and writer_attempt_id > 0:
                await attempt_observer.attempt_finished(writer_attempt_id, "succeeded")
        except Exception as exc:
            if attempt_observer is not None and writer_attempt_id > 0:
                await attempt_observer.attempt_finished(
                    writer_attempt_id, "failed", error_kind=type(exc).__name__
                )
            reason = f"writer unavailable: {type(exc).__name__}"
            return await self._execute_fallback_chain(
                analysis, fallback_bundle, reason, attempt_observer=attempt_observer
            )

        try:
            draft = await self._repair_and_check(
                draft,
                analysis,
                writer_bundle,
                historical_background=historical_background_str,
            )
        except UnsafeDraftError as exc:
            reason = str(exc)
            return await self._execute_fallback_chain(
                analysis, fallback_bundle, reason, attempt_observer=attempt_observer
            )
        except Exception as exc:
            self.logger.warning(
                "Editorial audit/repair failed; publishing writer output: %s",
                type(exc).__name__,
            )

        markdown = draft.to_markdown()
        try:
            deterministic_preflight(markdown)
        except ValueError as exc:
            reason = f"deterministic preflight failed: {exc}"
            return await self._execute_fallback_chain(
                analysis, fallback_bundle, reason, attempt_observer=attempt_observer
            )

        try:
            publication_copy_preflight(markdown)
        except ValueError as exc:
            self.logger.warning(
                "Publication-copy polish warning; publishing full Writer prose: %s",
                exc,
            )
            self._save_debug_artifact("publication_copy_warning.txt", str(exc))

        self._save_debug_artifact("final_article.md", markdown)
        return self._parse_article_response(markdown)

    async def _execute_fallback_chain(
        self,
        analysis: EditorialAnalysis,
        bundle: PreparedBundle,
        reason: str,
        attempt_observer: Any | None = None,
    ) -> Tuple[str, str, str]:
        # 1. Try validated story card fallback
        sc_attempt_id = 0
        if attempt_observer is not None:
            sc_attempt_id = await attempt_observer.attempt_started("story_renderer_fallback")
        try:
            res = await self._render_story_card_fallback(analysis, reason)
            if attempt_observer is not None and sc_attempt_id > 0:
                await attempt_observer.attempt_finished(sc_attempt_id, "succeeded")
            return res
        except Exception as card_exc:
            if attempt_observer is not None and sc_attempt_id > 0:
                await attempt_observer.attempt_finished(
                    sc_attempt_id, "failed", error_kind=type(card_exc).__name__
                )
            self.logger.warning("Validated Story Card render failed: %s", type(card_exc).__name__)

        # 2. Try deterministic fallback
        det_attempt_id = 0
        if attempt_observer is not None:
            det_attempt_id = await attempt_observer.attempt_started("deterministic_fallback")
        try:
            res = await self._fallback(bundle, reason)
            if attempt_observer is not None and det_attempt_id > 0:
                await attempt_observer.attempt_finished(det_attempt_id, "succeeded")
            return res
        except Exception as det_exc:
            if attempt_observer is not None and det_attempt_id > 0:
                await attempt_observer.attempt_finished(
                    det_attempt_id, "failed", error_kind=type(det_exc).__name__
                )
            raise

    async def generate_article(  # noqa: C901
        self, messages_by_channel: Dict[str, List[Message]]
    ) -> Tuple[str, str, str]:
        """Generate the main article or a thematic fallback for substantive input."""
        self._clear_debug_artifacts()
        bundle = self._build_bundle(messages_by_channel)
        self._save_debug_artifact("prepared_input.txt", bundle.prompt_text)
        try:
            analysis = await self._analyze(bundle)
            self._save_debug_artifact("story_cards.json", analysis.to_dict())
        except Exception as exc:
            self._save_debug_artifact("editorial_analysis_raw.txt", self.analyzer.last_raw_response)
            return await self._fallback(
                bundle, f"editorial analysis unavailable: {type(exc).__name__}"
            )

        if not analysis.cards:
            self.logger.info(
                "Editorial analysis found no publishable local stories for the reporting period"
            )
            raise NoSubstantiveEditorialError(
                "no publishable local stories remain for reporting period"
            )

        self.logger.info("Editorial analysis selected %d stories:", len(analysis.cards))
        for card in analysis.cards:
            self.logger.info(
                "  %s topic=%s importance=%s refs=%d",
                card.id,
                card.topic,
                card.importance,
                len(card.all_source_refs()),
            )

        writer_bundle = self._select_writer_bundle(analysis, bundle)
        return await self.generate_from_analysis_and_bundle(
            analysis=analysis,
            writer_bundle=writer_bundle,
            bundle_for_fallback=bundle,
        )
