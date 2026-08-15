"""Daily Story Card editorial pipeline with safe degraded publication paths."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from src.ai_providers import AIProvider, create_provider
from src.collector import Message
from src.config_loader import Config, SourceRoleResolver
from src.editorial_analysis import ContextSizeRejectedError, EditorialAnalyzer
from src.editorial_audit import FactCheckUnavailableError, LightFactChecker, deterministic_preflight
from src.editorial_fallback import DeterministicStoryCardBuilder, StoryCardRenderer
from src.editorial_input import EditorialInputBuilder
from src.editorial_models import EditorialAnalysis, PreparedBundle
from src.editorial_writer import ArticleDraft, EditorialWriter


class UnsafeDraftError(RuntimeError):
    """Raised when an unresolved high-risk fragment is central to the draft."""


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


class ArticleGenerator:
    """Generate a readable article, repairing locally and never dumping raw messages."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.provider: AIProvider = create_provider(
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
            api_timeout=config.settings.api_timeout,
        )
        self.model = config.settings.ai_model
        self.output_language = config.settings.output_language
        skill_path = getattr(
            config.settings.article, "prompt_template", ".agents/skills/news-style/SKILL.md"
        )
        self.skill_instructions = _load_skill_instructions(skill_path)
        self.role_resolver = SourceRoleResolver(config.channels)
        self.input_builder = EditorialInputBuilder(self.role_resolver)
        self.analyzer = EditorialAnalyzer(self.provider, self.model, logger)
        self.writer = EditorialWriter(self.provider, self.model, self.skill_instructions, logger)
        self.fact_checker = LightFactChecker(self.provider, self.model, logger)
        self.fallback_builder = DeterministicStoryCardBuilder()
        self.fallback_renderer = StoryCardRenderer()

    def _compose_system_prompt(self) -> str:
        """Compatibility helper exposing the single writer prompt owner."""
        return (
            f"{self.skill_instructions}\n\n"
            f"Write in the configured output language: {self.output_language}.\n"
            "Story Cards are reporting notes, not a sentence template. Combine, reorder, "
            "compress and connect supplied material naturally. Do not create a new independently "
            "verifiable fact absent from the cards and source material. Return strict JSON only."
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

    async def _analyze(self, bundle: PreparedBundle) -> EditorialAnalysis:
        retries = max(0, int(getattr(self.config.settings.article, "generation_retries", 2)))
        delay = max(
            0.0, float(getattr(self.config.settings.article, "generation_retry_delay", 1.0))
        )
        for attempt in range(retries + 1):
            try:
                return await self.analyzer.analyze(bundle)
            except ContextSizeRejectedError:
                self.logger.warning(
                    "Editorial analysis exceeded model context; using explicit context batching"
                )
                return await self.analyzer.analyze_batched(bundle)
            except Exception:
                if attempt >= retries:
                    raise
                if delay:
                    await asyncio.sleep(delay * (attempt + 1))
                self.logger.warning("Editorial analysis attempt %d failed; retrying", attempt + 1)
        raise RuntimeError("Editorial analysis exhausted retries")

    async def _fallback(self, bundle: PreparedBundle, reason: str) -> Tuple[str, str, str]:
        self.logger.warning("Using deterministic editorial fallback: %s", reason)
        cards = self.fallback_builder.build(bundle)
        draft = self.fallback_renderer.render(cards)
        markdown = draft.to_markdown()
        deterministic_preflight(markdown)
        self._save_debug_artifact("fallback_reason.txt", reason)
        self._save_debug_artifact(
            "fallback_story_cards.json", {"cards": [card.to_dict() for card in cards]}
        )
        return self._parse_article_response(markdown)

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

    async def _repair_and_check(  # noqa: C901
        self, draft: ArticleDraft, analysis: EditorialAnalysis, bundle: PreparedBundle
    ) -> ArticleDraft:
        try:
            result = await self.fact_checker.check(draft, analysis, bundle)
        except FactCheckUnavailableError as exc:
            self.logger.warning("Light fact-check unavailable; publishing writer output: %s", exc)
            return draft
        self._save_debug_artifact(
            "fact_check.json",
            {
                "status": result.status,
                "systemic_problem": result.systemic_problem,
                "issues": [issue.to_dict() for issue in result.issues],
            },
        )
        if result.status != "FIX":
            if result.status == "WARN":
                self.logger.warning(
                    "Publishing article with %d fact-check warning(s)", len(result.issues)
                )
            return draft

        if result.systemic_problem:
            self.logger.warning("Fact-check found a systemic issue; regenerating once")
            regenerated = await self.writer.write(analysis, bundle)
            deterministic_preflight(regenerated.to_markdown())
            try:
                regenerated_check = await self.fact_checker.check(regenerated, analysis, bundle)
                if regenerated_check.status == "FIX" and regenerated_check.systemic_problem:
                    raise UnsafeDraftError("systemic fact-check issue remains after regeneration")
            except FactCheckUnavailableError:
                pass
            return regenerated

        current = draft
        for repair_pass in range(2):
            current = await self.fact_checker.repair(current, result, analysis, bundle)
            deterministic_preflight(current.to_markdown())
            if repair_pass == 1:
                break
            try:
                result = await self.fact_checker.check(current, analysis, bundle)
            except FactCheckUnavailableError:
                return current
            if result.systemic_problem and result.status == "FIX":
                raise UnsafeDraftError("systemic fact-check issue remains after local repair")
            if result.status != "FIX":
                return current
        unresolved = [issue for issue in result.issues if issue.severity == "fix"]
        if any(issue.unit_id in {"TITLE", "LEAD"} for issue in unresolved):
            raise UnsafeDraftError("unresolved FIX remains in headline or lead")
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
        current = current.apply_replacements({issue.unit_id: "" for issue in unresolved})
        self.logger.warning("Removed %d unresolved local FIX fragment(s)", len(unresolved))
        return current

    async def generate_article(
        self, messages_by_channel: Dict[str, List[Message]]
    ) -> Tuple[str, str, str]:
        """Generate the main article or a thematic fallback for substantive input."""
        bundle = self._build_bundle(messages_by_channel)
        self._save_debug_artifact("prepared_input.txt", bundle.prompt_text)
        try:
            analysis = await self._analyze(bundle)
            self._save_debug_artifact("story_cards.json", analysis.to_dict())
        except Exception as exc:
            return await self._fallback(
                bundle, f"editorial analysis unavailable: {type(exc).__name__}"
            )

        if not analysis.cards:
            return await self._fallback(bundle, "editorial analysis returned no Story Cards")

        try:
            draft = await self.writer.write(analysis, bundle)
            deterministic_preflight(draft.to_markdown())
            self._save_debug_artifact("writer_draft.json", draft.to_dict())
        except Exception as exc:
            return await self._fallback(bundle, f"writer unavailable: {type(exc).__name__}")

        try:
            draft = await self._repair_and_check(draft, analysis, bundle)
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
