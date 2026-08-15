"""Daily Story Card editorial pipeline with safe degraded publication paths."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from src.ai_providers import AIProvider, create_provider, ensure_provider_cascade
from src.city_context import CityContextResolver, CityProfileError, StoryContextEnricher
from src.collector import Message
from src.config_loader import Config, SourceRoleResolver
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
)
from src.editorial_fallback import (
    DeterministicStoryCardBuilder,
    NoSubstantiveMaterialError,
    StoryCardRenderer,
)
from src.editorial_input import EditorialInputBuilder
from src.editorial_models import EditorialAnalysis, PreparedBundle
from src.editorial_writer import ArticleDraft, EditorialWriter


class UnsafeDraftError(RuntimeError):
    """Raised when an unresolved high-risk fragment is central to the draft."""


class NoSubstantiveEditorialError(NoSubstantiveMaterialError):
    """Valid editorial analysis found no publishable local story."""


def _load_skill_instructions(path: str) -> str:
    """Load the configured skill once, stripping optional YAML frontmatter."""
    skill_path = Path(path)
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
            google_api_keys=(config.google_api_key_2, config.google_api_key_3),
            openrouter_api_key=config.openrouter_api_key,
            openrouter_base_url=config.openrouter_base_url,
            openrouter_model=config.openrouter_model,
            ollama_base_url=config.settings.ollama_base_url,
            api_timeout=config.settings.article.editorial_api_timeout,
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
        self.analyzer = EditorialAnalyzer(
            self.provider,
            self.model,
            logger,
            max_output_tokens=config.settings.article.editorial_analysis_max_output_tokens,
            compact_max_output_tokens=(
                config.settings.article.editorial_analysis_compact_max_output_tokens
            ),
            output_language=self.output_language,
        )
        self.writer = EditorialWriter(
            self.provider,
            self.model,
            self.skill_instructions,
            logger,
            max_output_tokens=config.settings.article.editorial_writer_max_output_tokens,
            output_language=self.output_language,
        )
        self.fact_checker = LightFactChecker(
            self.provider,
            self.model,
            logger,
            max_output_tokens=config.settings.article.editorial_audit_max_output_tokens,
            repair_max_output_tokens=config.settings.article.editorial_repair_max_output_tokens,
            output_language=self.output_language,
        )
        self.fallback_builder = DeterministicStoryCardBuilder()
        self.fallback_renderer = StoryCardRenderer(output_language=self.output_language)

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
        except ContextSizeRejectedError:
            self.logger.warning(
                "Editorial analysis exceeded model context; using explicit context batching"
            )
            return await self.analyzer.analyze_batched(bundle, compact=False)
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
        self._save_debug_artifact("story_card_fallback.md", markdown)
        return self._parse_article_response(markdown)

    async def _fallback(self, bundle: PreparedBundle, reason: str) -> Tuple[str, str, str]:
        self.logger.warning("Editorial pipeline entered degraded path: %s", reason)
        cards = self.fallback_builder.build(bundle)
        self.logger.info("Deterministic fallback built %d normalized Story Cards", len(cards))
        draft = self.fallback_renderer.render(cards)
        markdown = draft.to_markdown()
        deterministic_preflight(markdown)
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

    async def _repair_and_check(  # noqa: C901
        self, draft: ArticleDraft, analysis: EditorialAnalysis, bundle: PreparedBundle
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

        if result.systemic_problem:
            self.logger.warning("Fact-check found a systemic issue; regenerating once")
            regenerated = await self.writer.write(analysis, bundle)
            deterministic_preflight(regenerated.to_markdown())
            try:
                regenerated_check = await self.fact_checker.check(regenerated, analysis, bundle)
                self._save_fact_check_result("fact_check_final.json", regenerated_check)
                self._save_fact_check_result("fact_check.json", regenerated_check)
            except FactCheckUnavailableError as exc:
                self._save_fact_check_result("fact_check_final.json", result)
                self._save_fact_check_result("fact_check.json", result)
                self._save_fact_check_failure(exc)
                return regenerated

            if regenerated_check.systemic_problem and regenerated_check.status == "FIX":
                raise UnsafeDraftError("systemic fact-check issue remains after regeneration")
            if regenerated_check.status == "FIX":
                regenerated = self._remove_unresolved_local_fixes(regenerated, regenerated_check)
                deterministic_preflight(regenerated.to_markdown())
            return regenerated

        current = draft
        for _ in range(2):
            current = await self.fact_checker.repair(current, result, analysis, bundle)
            deterministic_preflight(current.to_markdown())
            try:
                result = await self.fact_checker.check(current, analysis, bundle)
                self._save_fact_check_result("fact_check_final.json", result)
                self._save_fact_check_result("fact_check.json", result)
            except FactCheckUnavailableError as exc:
                self._save_fact_check_result("fact_check_final.json", result)
                self._save_fact_check_result("fact_check.json", result)
                self._save_fact_check_failure(exc)
                return current

            if result.systemic_problem and result.status == "FIX":
                raise UnsafeDraftError("systemic fact-check issue remains after local repair")
            if result.status != "FIX":
                return current

        return self._remove_unresolved_local_fixes(current, result)

    def _remove_unresolved_local_fixes(
        self, draft: ArticleDraft, result: FactCheckResult
    ) -> ArticleDraft:
        unresolved = [issue for issue in result.issues if issue.severity == "fix"]
        if any(
            issue.unit_id in {"TITLE", "LEAD"} or issue.unit_id.startswith("H")
            for issue in unresolved
        ):
            raise UnsafeDraftError("unresolved FIX remains in headline, lead, or section heading")
        high_risk_codes = (
            "medical",
            "casualty",
            "weapon",
            "safety",
            "accus",
            "identity",
            "legal",
            "financial",
        )
        if any(
            any(token in issue.code.lower() for token in high_risk_codes) for issue in unresolved
        ):
            raise UnsafeDraftError("unresolved high-risk FIX remains in article")
        current = draft.apply_replacements({issue.unit_id: "" for issue in unresolved})
        self.logger.warning("Removed %d unresolved local FIX fragment(s)", len(unresolved))
        return current

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
        if not writer_bundle.records:
            return await self._fallback(
                bundle, "editorial analysis returned no resolvable representative refs"
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

        try:
            draft = await self.writer.write(analysis, writer_bundle)
            deterministic_preflight(draft.to_markdown())
            self._save_debug_artifact("writer_draft.json", draft.to_dict())
        except Exception as exc:
            reason = f"writer unavailable: {type(exc).__name__}"
            try:
                return await self._render_story_card_fallback(analysis, reason)
            except Exception as card_exc:
                self.logger.warning(
                    "Validated Story Card render failed: %s", type(card_exc).__name__
                )
                return await self._fallback(bundle, reason)

        try:
            draft = await self._repair_and_check(draft, analysis, writer_bundle)
        except UnsafeDraftError as exc:
            return await self._fallback(bundle, str(exc))
        except Exception as exc:
            self.logger.warning(
                "Editorial audit/repair failed; publishing writer output: %s",
                type(exc).__name__,
            )

        markdown = draft.to_markdown()
        deterministic_preflight(markdown)
        self._save_debug_artifact("final_article.md", markdown)
        return self._parse_article_response(markdown)
