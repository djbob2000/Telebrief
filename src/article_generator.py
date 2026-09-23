"""Daily Story Card editorial pipeline with safe degraded publication paths."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

from src.ai_providers import (
    AIProvider,
    ProviderCascade,
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
from src.publication.article_composition import build_article_composition_plan
from src.publication.article_context import ArticleEditorialContext
from src.publication.article_coverage_diagnostics import (
    ArticleCoverageDiagnostics,
    diagnose_article_coverage,
)
from src.publication.article_length import (
    ArticleLengthProfile,
    derive_article_length_profile,
)
from src.publication.article_models import StructuredArticleDraft
from src.publication.article_quality import (
    ArticleReaderQualityReport,
    diagnose_article_quality,
)
from src.publication.article_quote_allowlist import build_article_quote_allowlist
from src.publication.article_validator import (
    ArticleValidationResult,
    validate_article_draft,
)
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
    from src.publication.article_semantic_support import _EDITORIAL_GLUE, _STOPWORDS

    tok_re = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)

    # Precompute glue stems and edition anchor terms
    glue_stems = {_stem(g) for g in _EDITORIAL_GLUE}
    if article_ctx and getattr(article_ctx, "edition_anchor_terms", None):
        glue_stems.update(
            {_stem(t.lower()) for t in article_ctx.edition_anchor_terms if len(t) >= 3}
        )
    glue_stems.update(
        {
            "бердян",
            "бердянск",
            "бердянськ",
            "город",
            "мисто",
            "жител",
            "горожан",
            "сообщ",
            "отмеч",
            "рассказ",
            "продолж",
        }
    )

    def _extract_distinctive_stems(text: str) -> set[str]:
        words = tok_re.findall(text)
        return {
            _stem(w.lower())
            for w in words
            if len(w) >= 3 and _stem(w.lower()) not in glue_stems and w.lower() not in _STOPWORDS
        }

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
            support_stems[sid] = _extract_distinctive_stems(full_t)

    # 1. Title & lead support IDs (rely only on writer-provided or lexical match)
    if parsed.get("title_support_ids"):
        parsed["title_support_ids"] = [
            sid for sid in parsed.get("title_support_ids", ()) if sid in support_by_id
        ]
    else:
        matched_t_sups: list[str] = []
        if support_stems and parsed.get("title"):
            t_text = str(parsed["title"])
            t_stems = _extract_distinctive_stems(t_text)
            t_nums = set(re.findall(r"\b\d+\b", t_text))
            for sid, s_stems in support_stems.items():
                shared_st = t_stems & s_stems
                s_text = getattr(support_by_id.get(sid), "text", "")
                s_nums_sup = set(re.findall(r"\b\d+\b", s_text)) if s_text else set()
                shared_nums = t_nums & s_nums_sup
                if len(shared_st) >= 2 or (shared_st and shared_nums) or len(shared_nums) >= 2:
                    matched_t_sups.append(sid)
        parsed["title_support_ids"] = [
            sid for sid in dict.fromkeys(matched_t_sups) if sid in support_by_id
        ]

    if support_stems and parsed.get("lead"):
        l_text = str(parsed["lead"])
        l_stems = _extract_distinctive_stems(l_text)
        l_nums = set(re.findall(r"\b\d+\b", l_text))
        matched_lead_sups: list[str] = []
        for sid in curr_pub_sups:
            s_stems = support_stems.get(sid, set())
            shared_st = l_stems & s_stems
            s_text = getattr(support_by_id.get(sid), "text", "")
            s_nums_sup = set(re.findall(r"\b\d+\b", s_text)) if s_text else set()
            shared_nums = l_nums & s_nums_sup
            if len(shared_st) >= 2 or (shared_st and shared_nums) or len(shared_nums) >= 2:
                matched_lead_sups.append(sid)
        existing_l_sups = [
            sid for sid in (parsed.get("lead_support_ids") or ()) if sid in support_by_id
        ]
        all_l_sups = existing_l_sups if existing_l_sups else list(dict.fromkeys(matched_lead_sups))
        if not all_l_sups and support_stems:
            best_sids = []
            best_score = 0
            for sid in curr_pub_sups or list(support_stems.keys()):
                s_stems = support_stems.get(sid, set())
                sc = len(l_stems & s_stems)
                if sc > best_score:
                    best_score = sc
                    best_sids = [sid]
                elif sc == best_score and sc > 0:
                    best_sids.append(sid)
            if best_sids:
                all_l_sups = best_sids[:3]

        if not all_l_sups and coverage_plan and getattr(coverage_plan, "stories", None):
            dev_sups = [
                sid
                for s in coverage_plan.stories
                if getattr(s, "prominence", "") == "DEVELOP"
                for sid in s.support_ids
            ]
            if dev_sups:
                all_l_sups = dev_sups[:3]

        parsed["lead_support_ids"] = all_l_sups
        if not parsed.get("lead_claims"):
            l_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", l_text) if s.strip()]
            lead_claims_list = []
            for sent in l_sentences:
                s_stems = _extract_distinctive_stems(sent)
                s_nums = set(re.findall(r"\b\d+\b", sent))
                matched_sent_sups = []
                for sid in all_l_sups:
                    sup_st = support_stems.get(sid, set())
                    shared_st = s_stems & sup_st
                    s_text = getattr(support_by_id.get(sid), "text", "")
                    s_nums_sup = set(re.findall(r"\b\d+\b", s_text)) if s_text else set()
                    shared_nums = s_nums & s_nums_sup
                    if (
                        len(shared_st) >= 2
                        or (shared_st and shared_nums)
                        or len(shared_nums) >= 2
                        or (shared_st and len(s_stems) <= 3)
                    ):
                        matched_sent_sups.append(sid)
                if not matched_sent_sups and all_l_sups:
                    matched_sent_sups = all_l_sups[:2]
                lead_claims_list.append(
                    {
                        "text": sent,
                        "cited_support_ids": matched_sent_sups,
                    }
                )
            parsed["lead_claims"] = lead_claims_list
    elif parsed.get("lead_support_ids"):
        parsed["lead_support_ids"] = [
            sid for sid in parsed.get("lead_support_ids", ()) if sid in support_by_id
        ]
    else:
        parsed["lead_support_ids"] = []

    # 2. Sections & paragraphs
    raw_sections = parsed.get("sections") or []

    for sec in raw_sections:
        if not isinstance(sec, dict):
            continue

        existing_h_sups = list(sec.get("heading_support_ids") or [])
        if existing_h_sups:
            combined_h_sups = list(dict.fromkeys(existing_h_sups))
        else:
            # Match heading supports based on heading text stems and numbers
            h_text = str(sec.get("heading") or "")
            matched_h_sups = []
            if support_stems and h_text:
                h_stems = _extract_distinctive_stems(h_text)
                h_nums = set(re.findall(r"\b\d+\b", h_text))
                for sid, s_stems in support_stems.items():
                    shared_st = h_stems & s_stems
                    s_text = getattr(support_by_id.get(sid), "text", "")
                    s_nums = set(re.findall(r"\b\d+\b", s_text)) if s_text else set()
                    shared_nums = h_nums & s_nums
                    if (
                        len(shared_st) >= 2
                        or (shared_st and shared_nums)
                        or (h_nums and shared_nums)
                    ):
                        matched_h_sups.append(sid)
            combined_h_sups = list(dict.fromkeys(matched_h_sups))

        if support_by_id:
            combined_h_sups = [sid for sid in combined_h_sups if sid in support_by_id]

        raw_paras = sec.get("paragraphs") or []
        grounded_paras: list[dict[str, Any]] = []
        for p in raw_paras:
            p_text = p if isinstance(p, str) else p.get("text", "")
            existing_cited = [] if isinstance(p, str) else list(p.get("cited_support_ids") or [])

            matched_sups = []
            if support_stems:
                p_stems = _extract_distinctive_stems(p_text)
                p_nums = set(re.findall(r"\b\d+\b", p_text))
                # Match supports that share at least 2 content stems, or share numbers + stem
                for sid, s_stems in support_stems.items():
                    shared_stems = p_stems & s_stems
                    s_text = getattr(support_by_id.get(sid), "text", "")
                    s_nums = set(re.findall(r"\b\d+\b", s_text)) if s_text else set()
                    shared_nums = p_nums & s_nums
                    if (
                        len(shared_stems) >= 2
                        or (shared_stems and shared_nums)
                        or len(shared_nums) >= 2
                        or (len(shared_stems) >= 1 and len(p_stems) <= 4)
                    ):
                        matched_sups.append(sid)

            # Writer-supplied citations are hints, not proof.  Keep them only
            # when the paragraph itself has a deterministic anchor in the
            # cited support.  Otherwise a fluent hallucination can carry an
            # unrelated support ID into the validator and appear grounded.
            grounded_existing = [sid for sid in existing_cited if sid in matched_sups]
            # If the writer supplied citations, keep only that grounded set.
            # Do not append every lexically similar support from another Story:
            # that turns broad vocabulary overlap into cross-story provenance.
            combined_sups = (
                list(dict.fromkeys(grounded_existing))
                if existing_cited
                else list(dict.fromkeys(matched_sups))
            )
            if not combined_sups and support_stems:
                # Do not invent provenance for an unmatched paragraph.  A
                # best-single-stem match, section-heading inheritance, or
                # DEVELOP-story fallback can attach an unrelated source to a
                # fluent but fabricated sentence.  The validator must see the
                # paragraph as unsupported and fail closed instead.
                combined_sups = []

            if support_by_id:
                combined_sups = [sid for sid in combined_sups if sid in support_by_id]

            para_dict = {
                "text": p_text,
                "cited_support_ids": combined_sups,
            }
            if isinstance(p, dict) and "claims" in p and p["claims"]:
                claims_list = []
                for cl in p["claims"]:
                    if isinstance(cl, dict):
                        cl_text = cl.get("text", "")
                        existing_c_sups = [
                            sid for sid in cl.get("cited_support_ids", ()) if sid in combined_sups
                        ]
                        if not existing_c_sups and combined_sups:
                            existing_c_sups = combined_sups
                        claims_list.append(
                            {
                                "text": cl_text,
                                "cited_support_ids": existing_c_sups,
                            }
                        )
                    else:
                        claims_list.append(cl)
                para_dict["claims"] = claims_list
            grounded_paras.append(para_dict)

        sec["paragraphs"] = grounded_paras

        # Heading supports: if empty, inherit from grounded paragraphs in this section
        if not combined_h_sups and grounded_paras:
            combined_h_sups = list(
                dict.fromkeys(sid for p in grounded_paras for sid in p.get("cited_support_ids", []))
            )
        sec["heading_support_ids"] = combined_h_sups

    return parsed


def _is_globally_incomplete(
    validation_result: ArticleValidationResult,
    diagnostics: ArticleCoverageDiagnostics,
) -> bool:
    """Classify broad coverage gaps for diagnostics, never as a retry trigger."""
    has_draft_blocking = any(
        iss.blocking and iss.unit_id in ("DRAFT", "") for iss in validation_result.issues
    )
    all_develop_covered = diagnostics.develop_story_coverage >= 1.0
    low_coverage = diagnostics.story_coverage < 0.80
    many_missing = len(diagnostics.uncovered_story_ids) > 3
    return has_draft_blocking or (not all_develop_covered) or low_coverage or many_missing


def _is_catastrophic_writer_response(
    draft: StructuredArticleDraft,
    validation_result: ArticleValidationResult,
    diagnostics: ArticleCoverageDiagnostics,
) -> bool:
    """Identify a refusal/truncated response, not an ordinary coverage shortfall."""
    return not draft.lead.strip()


def _build_writer_attempt_metadata(
    attempt_number: int,
    provider_name: str,
    model_name: str,
    response_text: str,
    val: ArticleValidationResult,
    diag: ArticleCoverageDiagnostics,
    provider_obj: Any = None,
    regeneration_reason: str | None = None,
    materialization: dict[str, object] | None = None,
    context_chars: int | None = None,
    prompt_chars: int | None = None,
    retry_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    validation_issues = getattr(val, "issues", None)
    if validation_issues is None:
        # Keep metadata generation tolerant of older/lightweight validation
        # objects used by compatibility callers. The Event-First validator
        # exposes structured `issues`; older callers may only expose rendered
        # violation strings.
        legacy_violations = getattr(val, "violations", ())
        validation_violations = (
            list(legacy_violations) if isinstance(legacy_violations, (list, tuple, set)) else []
        )
    else:
        validation_violations = [
            f"{issue.code}:{issue.unit_id}"
            for issue in validation_issues
            if getattr(issue, "blocking", False)
        ]
    meta: dict[str, Any] = {
        "attempt_number": attempt_number,
        "provider": provider_name,
        "model": model_name,
        "response_chars": len(response_text),
        "parsed_word_count": val.word_count,
        "parsed_section_count": val.section_count,
        "planned_story_count": diag.planned_story_count,
        "covered_story_count": diag.covered_story_count,
        "story_coverage": diag.story_coverage,
        "uncovered_story_ids": list(diag.uncovered_story_ids),
        "validation_violations": validation_violations,
    }
    if regeneration_reason:
        meta["regeneration_reason"] = regeneration_reason
    if materialization is not None:
        meta["materialization"] = dict(materialization)
    if context_chars is not None:
        meta["context_chars"] = context_chars
    if prompt_chars is not None:
        meta["prompt_chars"] = prompt_chars
    if retry_history:
        meta["writer_retry_history"] = retry_history
    prov_meta = getattr(provider_obj, "last_metadata", None)
    if isinstance(prov_meta, dict):
        for k in (
            "provider_slot",
            "actual_provider",
            "actual_model",
            "response_id",
            "finish_reason",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            if k in prov_meta:
                meta[k] = prov_meta[k]
    return meta


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
            openrouter_models=getattr(config, "openrouter_models", None),
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
            config.settings.article, "prompt_template", "src/prompts/news_style.md"
        )
        self.skill_instructions = _load_skill_instructions(skill_path)
        city_profile_path = getattr(
            config.settings.article, "city_profile_path", "data/city_profiles/berdyansk.yaml"
        )
        self.city_profile_path = Path(city_profile_path)
        self._place_resolvers_by_edition: dict[str, CityContextResolver | None] = {}
        try:
            self.city_context_resolver: CityContextResolver | None = CityContextResolver.from_yaml(
                self.city_profile_path
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

    def _place_resolver_for_article_context(
        self,
        article_ctx: ArticleEditorialContext,
    ) -> CityContextResolver | None:
        """Resolve geographic aliases only from the current edition's profile."""
        edition_slug = (article_ctx.edition_slug or "").strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", edition_slug):
            return None
        if edition_slug in self._place_resolvers_by_edition:
            return self._place_resolvers_by_edition[edition_slug]

        configured_profile_id = (
            self.city_context_resolver.profile_id.casefold()
            if self.city_context_resolver is not None
            else ""
        )
        if (
            self.city_context_resolver is not None
            and configured_profile_id
            and configured_profile_id == edition_slug
        ):
            self._place_resolvers_by_edition[edition_slug] = self.city_context_resolver
            return self.city_context_resolver

        edition_profile_path = self.city_profile_path.parent / f"{edition_slug}.yaml"
        if edition_profile_path.resolve() == self.city_profile_path.resolve():
            # A configured profile whose own identity disagrees with this run
            # is not safe to reuse under a different edition slug.
            self._place_resolvers_by_edition[edition_slug] = None
            return None

        try:
            resolver = CityContextResolver.from_yaml(edition_profile_path)
        except FileNotFoundError:
            resolver = None
        except CityProfileError as exc:
            self.logger.warning(
                "Edition city context profile unavailable for %s: %s", edition_slug, exc
            )
            resolver = None

        if resolver is not None and resolver.profile_id.casefold() != edition_slug:
            self.logger.warning(
                "Edition city context profile %s identifies as %s",
                edition_profile_path,
                resolver.profile_id,
            )
            resolver = None
        self._place_resolvers_by_edition[edition_slug] = resolver
        return resolver

    def _compose_system_prompt(self) -> str:
        """Compatibility helper exposing the single writer prompt owner."""
        return (
            f"{self.skill_instructions}\n\n"
            f"Write in the configured output language: {self.output_language}.\n"
            "Story cards and evidence packets are reporting material, not sentence templates. "
            "Combine and connect supported material naturally; let the article's shape follow the evidence. "
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
            edition_slug=getattr(frozen_input, "edition_slug", ""),
        )

    def _build_event_article_system_prompt(
        self,
        length_profile: ArticleLengthProfile | None = None,
        is_longitudinal: bool = False,
    ) -> str:
        """Compose the Event-First article prompt from safety and narrative newsroom contracts."""
        narrative_contract = build_article_narrative_contract(
            output_language=self.output_language,
            length_profile=length_profile,
        )
        longitudinal_block = ""
        if is_longitudinal:
            longitudinal_block = """
### СПЕЦИАЛЬНЫЙ ФОРМАТ: ЛОНГРИД-ПАНОРАМА (ИТОГИ НЕДЕЛИ / МЕСЯЦА):
- СТРУКТУРА: Развивайте подтверждённые сюжетные линии и используйте тематические разделы там, где этого требует материал.
- ХРОНОЛОГИЯ И ВЕХИ: Показывайте динамику развития событий во времени, используя точные даты и временные привязки (например: «[01.09]... к [04.09]...»).
- ФИНАЛ: Не повторяйте тезис лида как общий вывод. Завершайте на подтверждённом развитии, последствии или открытом вопросе, если материал даёт для этого основание.
"""
        return f"""Вы — опытный выпускающий редактор и автор регионального издания.
Ваша задача — написать связную, объективную, детальную и увлекательную городскую хронику (вечерний лонгрид) на русском языке на основе проверенных фактов, оперативной хроники и сообщений.

{narrative_contract}
{longitudinal_block}
### ЖЁСТКАЯ ГРАНИЦА ИСТОЧНИКА:
- Единственным источником фактов для статьи является материал между маркерами BEGIN ARTICLE MATERIAL и END ARTICLE MATERIAL в пользовательском сообщении.
- Не дополняйте отсутствующие сведения, не используйте внешние знания и не превращайте названия тем, служебный план или примеры из инструкций в факты выпуска.
- Если конкретная деталь не подтверждена в ARTICLE STORY PACKET или другом явно показанном материале, её нужно опустить, а не угадывать.

### Журналистский формат — Городская хроника (вечерний лонгрид):
1. ОБЪЁМ И ГЛУБИНА:
   - Статья должна представлять собой обстоятельное вечернее чтение городской хроники. Используйте диапазон объёма из переданного профиля длины; не добавляйте абзацы ради достижения нижней границы, если материал этого не поддерживает.
   - DEVELOP получает больше места; WEAVE связывает значимые линии; BRIEF сохраняется кратко, но конкретно. Depth controls space, not whether useful material may appear.
2. ЕСТЕСТВЕННАЯ ФОРМА И СОСТАВ:
   - Откройте текст подтверждённой центральной линией и задайте естественную временную рамку выпуска по локальному времени, если она доступна.
   - Организуйте статью в связные движения и используйте содержательные подзаголовки там, где они помогают чтению. Число заголовков, адресов и абзацев определяет материал; не задавайте квот и не отводите по абзацу каждой Story.
   - Следуйте связям, явно названным в ARTICLE COMPOSITION ROADMAP. Синтезируйте общее состояние, но сохраняйте существенные местные исключения, практические последствия и временные различия.
   - Дайте каждому полезному сообщению естественное место один раз. Не перечисляйте адреса ради демонстрации охвата и не объединяйте независимые сюжеты без фактического основания.
   - Не повторяйте тезис лида в каждом разделе и не заканчивайте общим пересказом этого тезиса.
   - effective_from/effective_until задают время события или состояния услуги. observed_at — только время сообщения и атрибуции; оно не устанавливает начало события.
3. КОНКРЕТНЫЕ ДЕТАЛИ И УВАЖИТЕЛЬНЫЙ ЯЗЫК:
   - Сохраняйте значимые подтверждённые микро-детали: место, интервал, сумму, действие жителей, практическое последствие или конкретное состояние услуги.
   - Пересказывайте личные просьбы и эмоциональные сообщения уважительно и косвенной речью. Не создавайте сцены, намерения, близость мест или причины, которых нет в источнике.
   - Не превращайте самостоятельный короткий BRIEF-сюжет в искусственную главу, если материал не даёт для неё связной темы; оставьте его компактным упоминанием без выдуманного моста.
4. ТОНАЛЬНОСТЬ, ЯЗЫК И АТРИБУЦИЯ:
   - Спокойный, уважительный, фактологический язык регионального журналиста.
   - РАЗНООБРАЗИЕ АТРИБУЦИИ: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО начинать каждое предложение подряд одинаковой фразой «По сообщениям жителей...». Варьируйте форму подачи: «горожане делятся», «очевидцы рассказывают», «жители микрорайона отмечают», «по наблюдениям с мест», «как выяснилось», либо формулируйте мысль напрямую от третьего лица с естественной атрибуцией в середине предложения.
   - Запрещены пустые клише и абстрактные формулировки («ситуация остается напряженной», «жители адаптируются к реалиям», «город живет в новых условиях»). Вместо абстракций приводите конкретные факты: кто что починил, как ходят автобусы, где есть вода и свет, сколько стоил бензин для генератора или провод для роутера.
   - Заголовок (title): емкий, информативный заголовок дня, отражающий ключевое подтверждённое событие. Не делайте заголовком вопросы жителей.

### ПРАВИЛО АНТИ-РЕКЛАМЫ И ОБОБЩЕНИЯ КОММЕРЧЕСКИХ ОБЪЯВЛЕНИЙ:
Объявления о частных услугах, коммерческих рейсах, платных клиниках и посредниках в городских чатах — это НЕ новости сами по себе. Их КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО цитировать дословно, копировать или превращать в каталоги услуг:

1. Частные перевозчики и междугородние рейсы:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО копировать цепочки городов маршрута, конкретные даты выезда и брать в кавычки рекламные фразы перевозчиков.
   - КАК ОБЯЗАТЕЛЬНО ПИСАТЬ: Сожмите до одного краткого журналистского предложения о факте транспортного сообщения на основе предоставленных данных (например, отметив сохранение нерегулярных рейсов по ключевым направлениям и высокий уровень цен, без копирования списков городов и контактов).

2. Посредники по банкам и пенсионным выплатам:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО перечислять списки коммерческих банков и длинные списки рутинных операций (разблокировка карт, перевод пенсии, актуализация данных).
   - ЕСЛИ ТАКАЯ ТЕМА ЕСТЬ В ИСХОДНЫХ ФАКТАХ: Сожмите до одного нейтрального предложения о социальном явлении. Если такой темы нет в карточках фактов — КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать о ней!

3. Платные медицинские центры и клиники:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО перечислять длинные каталоги врачебных специальностей («ЛОР, кардиолог, дерматолог, терапевт, педиатр, гастроэнтеролог...») и публиковать графики работы лабораторий или забора крови.
   - КАК ОБЯЗАТЕЛЬНО ПИСАТЬ: Ограничьтесь кратким упоминанием профиля работы или факта приёма специалистов на основе предоставленных данных, без длинных списков врачебных специальностей и без адресов, если они не подтверждены фактами.

4. ПРАВИЛО ОСВЕЩЕНИЯ КОММЕРЧЕСКИХ И СЕРВИСНЫХ ТЕМ:
   - Сохраняйте полезные для жителей микродетали (цены, скидки, адреса, изменения в графике, факты закрытия или распродаж). Опускайте только телефонные номера, ссылки, мессенджеры и пустые рекламные слоганы.
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать любые мета-отчёты или мета-комментарии об опущенных контактах, телефонах или датах («(контактные данные опущены)», «телефоны не указываются», «контакты скрыты», «номера не публикуются»). Промо-данные опускаются абсолютно молча, без комментариев в тексте!

5. Спортивные и образовательные учреждения:
   - Полезен факт набора, работы секции или учебного учреждения. Сохраняйте конкретные возрастные категории или годы рождения (например: «мальчики 2017, 2018 г.р. и девочки 2016, 2017 г.р.»), если они указаны в источниках — это ценная информация для родителей. Опускайте только контактные телефоны и рекламу тренеров.

### Обязательные правила журналистской точности:
1. Опирайтесь ТОЛЬКО на предоставленные в материалах факты и цитаты. Категорически ЗАПРЕЩЕНО выдумывать неподтвержденные детали, цифры, номера домов или адреса в других населённых пунктах, если их нет в предоставленных карточках фактов. Не добавляйте внешние знания из своей памяти.
2. Не придумывайте официальных подтверждений, если источник — сообщение жителя. Передавайте статус честно: «по сообщениям жителей», «горожане отмечают», «как рассказывают жители».
3. НЕ РАСКРЫВАЙТЕ ИСТОЧНИКИ, ТЕХНИЧЕСКУЮ КУХНЮ И НЕ ИСПОЛЬЗУЙТЕ СЛЕНГ:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать слова и маркеры чатовой кухни: «перекличка», «в перекличках», «в чате», «в местных чатах», «участник чата», «в каналах», «в пабликах», «в соцсетях». Не вскрывайте и не называйте источники информации.
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать разговорный и сетевой сленг («фигня», «хрень», «херня», «хреново», «нафиг», «пофиг» и т.п.). Даже если в сообщениях жителей встречаются подобные просторечия, в тексте статьи переводите их в качественный литературный язык («сохраняются перебои», «ситуация остается сложной», «проблемы с напряжением»).
   - Переводите любые упоминания чатовой активности и перекличек в естественную городскую журналистскую атрибуцию: «по сообщениям жителей», «горожане отмечают», «по наблюдениям с мест», «в разных районах города сообщают», «сведения расходятся», либо формулируйте мысль прямо от сути события.
4. Прямая речь, синтез сообщений жителей и запрет на «ленту чата» (QUOTE ALLOWLIST):
   - ЖЁСТКИЙ ЛИМИТ ЦИТАТ: Не более 2 прямых цитат в кавычках («...») на один раздел (section). Все остальные наблюдения жителей передавайте ТОЛЬКО косвенной речью без кавычек. Если у вас 5 сообщений жителей — выберите одну самую яркую цитату, а остальные 4 перескажите своими словами.
   - Запрет на «ленту чата»: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО перечислять реплики и цитаты жителей через запятую или тире подряд (например: «Цитата 1», «Цитата 2», «Цитата 3» — такие сообщения...). Это разрушает повествование и превращает статью в сырой дамп чата.
   - Синтез вместо перечисления: Когда несколько жителей сообщают об одном и том же (например, проблемы с коммунальными услугами в разных районах), синтезируйте массив сообщений в связный журналистский рассказ с географией и хронологией: кто, где и с какого времени наблюдает проблему, используя естественную косвенную речь.
   - Плавные связки и точечное цитирование: Прямая речь в кавычках («...») из блока QUOTE ALLOWLIST должна использоваться точечно (не более 1–2 ярких цитат на раздел) как иллюстрация живого голоса города, с плавной авторской подводкой или атрибуцией.
   - Если вы обобщаете мысль или пересказываете её своими словами — используйте естественную косвенную речь без кавычек.
5. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ МЕТА-КОММЕНТАРИИ И МЕТА-ОТЧЁТЫ ОБ ОПУЩЕННЫХ ДАННЫХ:
   - Никогда не пишите в тексте статьи фразы в скобках или ремарки вроде «(контактные данные опущены)», «(телефон не приводится)», «номера скрыты» или «как сообщалось ранее».
   - Статья пишется для читателя как естественный газетный очерк. Ненужные рекламные телефоны или служебные ремарки просто не упоминаются.
6. Не вставляйте в текст технические ID вроде [story:...] или [SUPPORT...].
7. Язык статьи: {self.output_language}. Текст должен быть связным, грамотным, с живыми микродеталями.
8. ЗАПРЕТ НА ЗАЦИКЛИВАНИЕ И ПОВТОР ОДИНАКОВЫХ ПРЕДЛОЖЕНИЙ (ANTI-LOOPING):
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО повторять одно и то же предложение или мысль дважды или трижды подряд в рамках одного абзаца или раздела. Каждое предложение должно нести новую мысль или развивать повествование.
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО дублировать целые предложения или абзацы в разных разделах статьи.
9. СООТВЕТСТВИЕ ЗАГОЛОВКОВ РАЗДЕЛОВ И ИХ СОДЕРЖИМОГО (HEADING-TEXT CONGRUENCE):
   - Если заголовок раздела содержит конкретное перечисление тем или подтем после двоеточия (например: «## Связь, сервисы и мелочи жизни: операторы, ограничение мобильного интернета и поиск кошки»), вы ОБЯЗАНЫ в тексте абзацев этого раздела раскрыть КАЖДУЮ обещанную в заголовке тему.
   - Категорически запрещено анонсировать в заголовке темы, о которых в абзацах раздела нет ни слова. Если тема не освещается в тексте раздела — не включайте её в заголовок!
10. ГЕОГРАФИЧЕСКАЯ СВЯЗНОСТЬ И ЗАПРЕТ НА ПЕРЕНОС ОТНОСИТЕЛЬНЫХ РАССТОЯНИЙ:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО механически переносить фразы об относительном расстоянии («буквально через два квартала», «через дорогу», «в соседнем дворе») из реплик жителей между сообщениями из разных районов города. Если житель говорит, что между двумя улицами «два квартала», эта фраза относится исключительно к тем двум улицам внутри одного микрорайона (например, на Горе). Недопустимо писать, что улицы Горы (Морозова, Пионерская) находятся «через два квартала» от улиц Центра (Карла Маркса, Горбенко) — между ними 3–4 километра и крутой 50-метровый подъём на гору! Всегда соблюдайте топографию и взаимное расположение районов из ГЕОГРАФИЧЕСКОГО КОНТЕКСТА.
11. РАЗДЕЛЕНИЕ КОММУНАЛЬНЫХ СФЕР (СВЕТ vs ВОДА vs СВЯЗЬ):
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО переносить сообщения из одной сферы в другую (например, реплику жителя в чате об отсутствии света по адресу К. Маркса, 51 запрещено превращать в отсутствие или график подачи воды по этому адресу).
12. ФИЛЬТРАЦИЯ ЧАТОВОГО САРКАЗМА И ЭМОЦИОНАЛЬНЫХ СПОРОВ:
   - Субъективные эмоциональные жалобы жителей, риторические восклицания и зависть соседей («вся гора с водой практически круглосуточно, а тут срамота», «какое круглосуточно», «боюсь сглазить») — это эмоциональный фон, а не оперативные факты. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО придумывать к ним вводные слова вроде «обычно» и превращать саркастические реплики в утверждения о штатном режиме работы служб (например, заявлять, что «на Горе вода обычно идёт круглосуточно, как в центре»).

### Формат ответа — обычный Markdown:
Первая строка — газетный заголовок без технических идентификаторов.
Затем один связный лид отдельным абзацем.
Каждая тематическая глава начинается с заголовка второго уровня (`## ...`),
после него идут обычные абзацы статьи. Не добавляйте JSON, списки support ID,
служебные комментарии или пояснения о формате ответа.
"""

    async def generate_from_event_article_context(  # noqa: C901
        self,
        article_ctx: ArticleEditorialContext,
        coverage_plan: Any | None = None,
        attempt_observer: Any | None = None,
    ) -> Tuple[str, str, str]:
        """Synthesize long-form editorial article directly from ArticleEditorialContext in one LLM call."""
        if article_ctx is None:
            raise NoSubstantiveEditorialError("no article editorial context present")
        place_resolver = self._place_resolver_for_article_context(article_ctx)

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
        develop_story_budget = 0

        lookback_hours = 24
        if article_ctx.publication_window is not None:
            delta = (
                article_ctx.publication_window.snapshot_at
                - article_ctx.publication_window.lookback_start
            )
            lookback_hours = int(delta.total_seconds() // 3600)
        is_longitudinal = lookback_hours >= 120

        from src.publication.article_coverage import build_article_coverage_plan
        from src.publication.article_material import project_article_material
        from src.publication.article_writer_context import (
            render_article_writer_context_with_stats,
        )

        if coverage_plan is None and getattr(article_ctx, "coverage_plan", None) is not None:
            coverage_plan = article_ctx.coverage_plan

        if coverage_plan is None:
            if is_longitudinal and article_ctx.story_cards:
                from src.publication.story_threads import (
                    build_longitudinal_coverage_plan,
                    cluster_stories_into_threads,
                    extract_story_thread_maps,
                )

                story_dates_map, story_sups_map = extract_story_thread_maps(
                    article_ctx.support_index
                )
                threads = cluster_stories_into_threads(
                    cards=article_ctx.story_cards,
                    story_dates=story_dates_map,
                    story_support_ids=story_sups_map,
                )
                coverage_plan = build_longitudinal_coverage_plan(threads)
            else:
                coverage_plan = build_article_coverage_plan(
                    article_ctx.story_cards,
                    article_ctx,
                    develop_story_budget=develop_story_budget,
                )

        material_projection = project_article_material(article_ctx)
        composition_plan = build_article_composition_plan(
            coverage_plan,
            article_ctx,
            material_projection,
        )
        context_str, materialization_stats = render_article_writer_context_with_stats(
            article_ctx,
            coverage_plan,
            include_coverage_plan=True,
            material_projection=material_projection,
            composition_plan=composition_plan,
        )
        materialization_metadata = (
            materialization_stats.to_metadata() if materialization_stats is not None else None
        )
        system_prompt = self._build_event_article_system_prompt(
            length_profile=length_profile,
            is_longitudinal=is_longitudinal,
        )
        user_prompt = (
            f"РЕДАКЦИОННЫЙ МАТЕРИАЛ И ФАКТЫ:\n\nBEGIN ARTICLE MATERIAL\n{context_str}\nEND ARTICLE MATERIAL\n\n"
            "ЗАДАНИЕ ВЫПУСКАЮЩЕМУ РЕДАКТОРУ:\n"
            "Напишите связную городскую вечернюю хронику в Markdown. ARTICLE COMPOSITION ROADMAP — единственная редакционная карта; точные факты и доказательства находятся в Story packets.\n"
            "1. ИЕРАРХИЯ: DEVELOP получает больше пространства, WEAVE органично связывает значимые линии, BRIEF остаётся кратким и конкретным. Сохраняйте полезные малые сообщения.\n"
            "2. СВЯЗНОСТЬ: Следуйте только явно указанным в карте отношениям. Сохраняйте существенные различия места, состояния услуги и времени. Каждому полезному сообщению дайте одно естественное место; не перечисляйте адреса ради формального покрытия и не сливайте независимые темы без основания.\n"
            "3. ВРЕМЯ: effective_from/effective_until описывают время события или состояния услуги. observed_at показывает время сообщения и уместно для хронологии сообщений/атрибуции, но не устанавливает начало события. Не выводите событие из времени сообщения.\n"
            "4. ФОРМА: Первая строка — заголовок, затем лид и связный текст. Используйте тематические подзаголовки, когда они помогают чтению. Число заголовков, адресов и абзацев определяет материал: не задавайте квоты и не отводите абзац каждой Story. Независимый короткий сюжет можно оставить кратким упоминанием без искусственной главы. Не повторяйте тезис лида в каждом разделе и как общий финал.\n"
            "5. ФАКТЫ И СТИЛЬ: Пишите литературным русским языком. Не выдумывайте причин, деталей, связей или завершения событий; не раскрывайте внутреннюю механику сбора сообщений.\n"
            f"Объём — примерно {length_profile.target_min_words}–{length_profile.target_max_words} слов "
            f"(проверочный диапазон: {length_profile.hard_min_words}–{length_profile.hard_max_words}), если фактический материал поддерживает такой объём.\n"
            "Верните только Markdown статьи, без JSON-обёртки и пояснений."
        )

        self.logger.info(
            "Article writer materialized input: stories=%d context_chars=%d prompt_chars=%d",
            len(getattr(coverage_plan, "stories", ())),
            len(context_str),
            len(system_prompt) + len(user_prompt),
        )

        writer_input_metadata: dict[str, Any] = {
            "context_chars": len(context_str),
            "prompt_chars": len(system_prompt) + len(user_prompt),
            "context_hash": hashlib.sha256(context_str.encode("utf-8")).hexdigest(),
            "prompt_hash": hashlib.sha256(
                f"{system_prompt}\0{user_prompt}".encode("utf-8")
            ).hexdigest(),
        }
        if article_ctx.publication_window is not None:
            snapshot_at = article_ctx.publication_window.snapshot_at
            try:
                edition_zone = ZoneInfo(article_ctx.edition_timezone or "UTC")
            except Exception:
                edition_zone = ZoneInfo("UTC")
            if snapshot_at.tzinfo is None:
                snapshot_at = snapshot_at.replace(tzinfo=ZoneInfo("UTC"))
            writer_input_metadata["as_of"] = snapshot_at.astimezone(edition_zone).isoformat()
            writer_input_metadata["as_of_utc"] = snapshot_at.astimezone(ZoneInfo("UTC")).isoformat()
            writer_input_metadata["edition_timezone"] = article_ctx.edition_timezone
        if materialization_metadata is not None:
            writer_input_metadata["materialization"] = materialization_metadata
        writer_input_metadata["material_projection"] = material_projection.to_metadata()
        writer_input_metadata["composition"] = composition_plan.to_metadata()

        from src.publication.article_finalization import ArticleFinalizer

        writer_draft: StructuredArticleDraft | None = None
        writer_error: Exception | None = None
        writer_attempt_id = 0
        writer_validation: ArticleValidationResult | None = None
        writer_quality_before_edit: ArticleReaderQualityReport | None = None
        writer_quality_after_edit: ArticleReaderQualityReport | None = None
        writer_retry_history: list[dict[str, Any]] = []

        suppressed_support_ids = {
            support_id
            for support_id, action in material_projection.actions_by_support_id.items()
            if action == "SUPPRESS_PROMOTION_ONLY"
        }
        quote_allowlist = build_article_quote_allowlist(
            article_ctx,
            excluded_support_ids=suppressed_support_ids,
            excluded_story_ids=material_projection.suppressed_story_ids,
            candidate_text_by_support_id=material_projection.text_by_support_id,
        )

        if attempt_observer is not None:
            writer_attempt_id = await attempt_observer.attempt_started(
                "writer",
                provider=self.config.settings.ai_provider,
                model=self.model,
                metadata={"attempt": 1, **writer_input_metadata},
            )

        try:
            article_temp = getattr(
                getattr(self.config.settings, "article", None), "temperature", 0.3
            )
            writer_max_tokens = min(
                self.config.settings.article.editorial_writer_max_output_tokens, 65536
            )
            writer_reasoning_effort = (
                getattr(self.config.settings, "reasoning_effort", "low") or "low"
            )
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            async def call_writer(slot_name: str | None = None) -> str:
                if slot_name and isinstance(self.provider, ProviderCascade):
                    return await self.provider.chat_completion_for_slot(
                        slot_name,
                        messages,
                        self.model,
                        temperature=article_temp,
                        max_tokens=writer_max_tokens,
                        reasoning_effort=writer_reasoning_effort,
                    )
                return await self.provider.chat_completion(
                    messages=messages,
                    model=self.model,
                    temperature=article_temp,
                    max_tokens=writer_max_tokens,
                    reasoning_effort=writer_reasoning_effort,
                )

            def evaluate_writer_response(
                raw_response: str,
            ) -> tuple[
                StructuredArticleDraft,
                ArticleValidationResult,
                ArticleCoverageDiagnostics,
                ArticleReaderQualityReport,
            ]:
                raw_parsed = self._parse_event_article_response(raw_response)
                parsed = _ground_draft_in_coverage_plan(raw_parsed, coverage_plan, article_ctx)
                draft = StructuredArticleDraft.from_dict(parsed, quote_allowlist=quote_allowlist)
                validation = validate_article_draft(
                    draft,
                    article_ctx,
                    config=editorial_config,
                    length_profile=length_profile,
                    material_projection=material_projection,
                )
                diagnostics = diagnose_article_coverage(draft, coverage_plan, context=article_ctx)
                quality = diagnose_article_quality(
                    draft,
                    coverage_plan,
                    article_ctx,
                    material_projection=material_projection,
                    place_resolver=place_resolver,
                )
                return draft, validation, diagnostics, quality

            def current_provider_slot() -> str | None:
                metadata = getattr(self.provider, "last_metadata", None)
                if isinstance(metadata, dict):
                    slot = metadata.get("provider_slot")
                    return slot if isinstance(slot, str) else None
                return None

            def record_writer_attempt(
                attempt_number: int,
                raw_response: str,
                validation: ArticleValidationResult,
                diagnostics: ArticleCoverageDiagnostics,
                quality: ArticleReaderQualityReport,
                catastrophic: bool,
                error: Exception | None = None,
            ) -> None:
                metadata = getattr(self.provider, "last_metadata", None)
                item: dict[str, Any] = {
                    "attempt_number": attempt_number,
                    "response_chars": len(raw_response),
                    "parsed_word_count": validation.word_count,
                    "parsed_section_count": validation.section_count,
                    "planned_story_count": diagnostics.planned_story_count,
                    "covered_story_count": diagnostics.covered_story_count,
                    "story_coverage": diagnostics.story_coverage,
                    "catastrophic": catastrophic,
                    "quality": quality.to_metadata(),
                }
                if isinstance(metadata, dict):
                    for key in (
                        "provider_slot",
                        "actual_provider",
                        "actual_model",
                        "finish_reason",
                    ):
                        if key in metadata:
                            item[key] = metadata[key]
                if error is not None:
                    item["error_type"] = type(error).__name__
                writer_retry_history.append(item)

            response = await call_writer()
            candidate_draft, candidate_val, candidate_diag, candidate_quality = (
                evaluate_writer_response(response)
            )
            catastrophic = _is_catastrophic_writer_response(
                candidate_draft, candidate_val, candidate_diag
            )
            record_writer_attempt(
                1, response, candidate_val, candidate_diag, candidate_quality, catastrophic
            )

            if catastrophic:
                initial_slot = current_provider_slot()
                self.logger.warning(
                    "Writer returned a catastrophic refusal/empty draft (%d words, %d/%d stories); "
                    "retrying the same provider slot once",
                    candidate_val.word_count,
                    candidate_diag.covered_story_count,
                    candidate_diag.planned_story_count,
                )
                for retry_index in range(2):
                    if retry_index == 1 and not isinstance(self.provider, ProviderCascade):
                        break
                    retry_slot = initial_slot
                    if retry_index == 1 and isinstance(self.provider, ProviderCascade):
                        retry_slot = self.provider.next_slot_name(initial_slot)
                        if retry_slot is None:
                            break
                        self.logger.warning(
                            "Writer refusal repeated on slot %s; switching to next provider slot %s",
                            initial_slot or "unknown",
                            retry_slot,
                        )
                    try:
                        response = await call_writer(retry_slot)
                        (
                            candidate_draft,
                            candidate_val,
                            candidate_diag,
                            candidate_quality,
                        ) = evaluate_writer_response(response)
                    except Exception as retry_exc:
                        record_writer_attempt(
                            retry_index + 2,
                            "",
                            candidate_val,
                            candidate_diag,
                            candidate_quality,
                            True,
                            error=retry_exc,
                        )
                        if retry_index == 1:
                            raise
                        continue
                    catastrophic = _is_catastrophic_writer_response(
                        candidate_draft, candidate_val, candidate_diag
                    )
                    record_writer_attempt(
                        retry_index + 2,
                        response,
                        candidate_val,
                        candidate_diag,
                        candidate_quality,
                        catastrophic,
                    )
                    if not catastrophic:
                        break
                    initial_slot = current_provider_slot() or initial_slot

            writer_validation = candidate_val
            writer_quality_before_edit = candidate_quality
            writer_quality_after_edit = candidate_quality

            is_incomplete = _is_globally_incomplete(candidate_val, candidate_diag)
            attempt_1_meta = _build_writer_attempt_metadata(
                attempt_number=1,
                provider_name=getattr(self.config.settings, "ai_provider", "unknown"),
                model_name=self.model,
                response_text=response,
                val=candidate_val,
                diag=candidate_diag,
                provider_obj=self.provider,
                regeneration_reason=None,
                materialization=materialization_metadata,
                context_chars=len(context_str),
                prompt_chars=len(system_prompt) + len(user_prompt),
                retry_history=writer_retry_history,
            )
            attempt_1_meta["quality"] = candidate_quality.to_metadata()
            attempt_1_meta["quality_before_edit"] = candidate_quality.to_metadata()
            attempt_1_meta["quality_after_edit"] = candidate_quality.to_metadata()
            attempt_1_meta["composition"] = composition_plan.to_metadata()
            attempt_1_meta["material_projection"] = material_projection.to_metadata()
            for metadata_key in (
                "context_hash",
                "prompt_hash",
                "as_of",
                "as_of_utc",
                "edition_timezone",
            ):
                if metadata_key in writer_input_metadata:
                    attempt_1_meta[metadata_key] = writer_input_metadata[metadata_key]

            # Coverage diagnostics must not trigger a second full writer call.
            # The article is a hierarchical long read, not a checklist whose
            # missing BRIEF/WEAVE items justify resending the entire context.
            if is_incomplete:
                self.logger.warning(
                    "Writer draft has incomplete coverage (%d words, %d/%d stories covered); "
                    "retaining the single writer result for finalization",
                    candidate_val.word_count,
                    candidate_diag.covered_story_count,
                    candidate_diag.planned_story_count,
                )
                attempt_1_meta["coverage_retry_suppressed"] = True
            writer_meta = attempt_1_meta
            writer_meta["editor_retry_count"] = 0
            writer_meta["editor_patched_unit_ids"] = []

            if candidate_val.is_valid and not candidate_quality.needs_edit:
                writer_draft = candidate_draft
                writer_error = None
            else:
                self.logger.warning(
                    "Writer produced invalid/incomplete or low-quality draft: factual=%s quality=%s",
                    list(candidate_val.violations)[:5],
                    [f.code for f in candidate_quality.repair_findings][:5],
                )
                writer_draft = candidate_draft

                # Targeted copy-editor / fact-checker pass when not globally incomplete or when draft is substantial
                hard_min = (
                    length_profile.hard_min_words
                    if length_profile is not None
                    else getattr(editorial_config, "article_min_words", 500)
                )
                is_substantial = (
                    candidate_val.word_count >= hard_min and candidate_val.section_count >= 2
                )
                if (
                    not is_incomplete or is_substantial or candidate_quality.needs_edit
                ) and getattr(editorial_config, "article_editor_enabled", False):
                    from src.publication.article_editor import ArticleEditor

                    editor_max_tokens = getattr(
                        getattr(self.config.settings, "article", None),
                        "editorial_repair_max_output_tokens",
                        32768,
                    )
                    editor = ArticleEditor(
                        provider=self.provider,
                        model=self.model,
                        max_output_tokens=editor_max_tokens,
                    )
                    editor_attempts = getattr(editorial_config, "article_editor_max_attempts", 2)
                    edited_draft, edited_val = await editor.edit_draft(
                        candidate_draft,
                        candidate_val,
                        article_ctx,
                        config=editorial_config,
                        length_profile=length_profile,
                        attempt_observer=attempt_observer,
                        max_attempts=editor_attempts,
                        quality_report=candidate_quality,
                        coverage_plan=coverage_plan,
                        material_projection=material_projection,
                        place_resolver=place_resolver,
                    )
                    edited_quality = diagnose_article_quality(
                        edited_draft,
                        coverage_plan,
                        article_ctx,
                        material_projection=material_projection,
                        place_resolver=place_resolver,
                    )
                    writer_quality_after_edit = edited_quality
                    writer_meta["quality_after_edit"] = edited_quality.to_metadata()
                    if edited_val.is_valid and not edited_quality.needs_edit:
                        self.logger.info(
                            "ArticleEditor successfully resolved validation issues; draft accepted"
                        )
                        writer_draft = edited_draft
                        writer_error = None
                        writer_validation = edited_val
                        candidate_quality = edited_quality
                    else:
                        writer_draft = edited_draft
                        writer_validation = edited_val
                        candidate_quality = edited_quality
                    writer_meta["editor_retry_count"] = editor.last_attempt_count
                    writer_meta["editor_patched_unit_ids"] = list(editor.last_patched_unit_ids)
        except Exception as exc:
            self.logger.warning(
                "Event article writer execution failed (%s: %s)",
                type(exc).__name__,
                exc,
            )
            if isinstance(exc, ProviderCascadeError):
                if attempt_observer is not None and writer_attempt_id:
                    await attempt_observer.attempt_finished(
                        writer_attempt_id,
                        status="failed",
                        error_kind="provider_cascade_error",
                        metadata={"exception_type": type(exc).__name__},
                    )
                raise
            writer_error = exc
            writer_meta = None

        finalization_result = await ArticleFinalizer().finalize(
            writer_draft=writer_draft,
            writer_error=writer_error,
            writer_attempt_id=writer_attempt_id,
            context=article_ctx,
            coverage_plan=coverage_plan,
            editorial_config=editorial_config,
            length_profile=length_profile,
            attempt_observer=attempt_observer,
            writer_metadata=writer_meta,
            writer_validation=writer_validation,
            quality_report=writer_quality_before_edit,
            quality_report_after_edit=writer_quality_after_edit,
            material_projection=material_projection,
            place_resolver=place_resolver,
        )

        body = finalization_result.draft.render_markdown()
        return (finalization_result.draft.title, finalization_result.draft.lead, body)

    def _parse_event_article_response(self, response: str) -> dict[str, Any]:
        """Parse the writer's Markdown, retaining JSON compatibility for older callers."""
        cleaned = (response or "").strip()
        json_candidate = cleaned.lstrip()
        if json_candidate.startswith("{") or json_candidate.startswith("```json"):
            try:
                return self._parse_event_article_response_json(response)
            except (ValueError, json.JSONDecodeError):
                # A malformed JSON response can still contain a readable Markdown
                # article.  Fall through to the text parser instead of issuing a
                # second writer request.
                pass
        return self._parse_event_article_response_markdown(response)

    @staticmethod
    def _parse_event_article_response_markdown(response: str) -> dict[str, Any]:
        """Convert the writer's reader-facing Markdown into the internal draft shape."""
        lines = (response or "").strip().splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        title = ""
        lead_paragraphs: list[str] = []
        sections: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        paragraph_lines: list[str] = []

        def flush_paragraph() -> None:
            if not paragraph_lines:
                return
            paragraph = " ".join(line.strip() for line in paragraph_lines).strip()
            paragraph_lines.clear()
            if not paragraph:
                return
            if current is None:
                lead_paragraphs.append(paragraph)
            else:
                current["paragraphs"].append(paragraph)

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                flush_paragraph()
                continue
            if line.startswith("---") or line.startswith("***") or line.startswith("___"):
                flush_paragraph()
                continue
            if line.startswith("# "):
                flush_paragraph()
                if not title:
                    title = line[2:].strip()
                continue
            if line.startswith("## ") or line.startswith("### "):
                flush_paragraph()
                heading = line.lstrip("#").strip()
                current = {"heading": heading, "paragraphs": []}
                sections.append(current)
                continue
            paragraph_lines.append(line)
        flush_paragraph()

        if not title and lead_paragraphs:
            title = lead_paragraphs.pop(0)
        lead = " ".join(lead_paragraphs).strip()
        if not sections and lead:
            sections = [{"heading": "Городская хроника", "paragraphs": [lead]}]
            lead = ""
        if not title:
            title = "Городская хроника"

        return {"title": title, "lead": lead, "sections": sections}

    def _parse_event_article_response_json(self, response: str) -> dict[str, Any]:
        """Clean and parse legacy JSON article responses."""
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
            try:
                parsed = json.loads(fixed, strict=False)
            except json.JSONDecodeError:
                # Additional pass: escape unescaped control chars or newlines in strings
                # and fix missing comma between quoted string and next key
                fixed2 = re.sub(r'"\s*\n\s*"([a-zA-Z0-9_]+)":', r'",\n"\1":', fixed)
                fixed2 = re.sub(
                    r'([0-9]|true|false|null)\s*\n\s*"([a-zA-Z0-9_]+)":', r'\1,\n"\2":', fixed2
                )
                parsed = json.loads(fixed2, strict=False)

        if not isinstance(parsed, dict):
            raise ValueError("article writer response is not a JSON object")

        from src.publication.article_models import _normalize_homoglyphs

        _TECHNICAL_KEYS = {
            "cited_support_ids",
            "support_ids",
            "title_support_ids",
            "lead_support_ids",
            "heading_support_ids",
            "cited_evidence_ids",
            "evidence_ids",
            "fragment_ids",
            "story_ids",
        }

        def _deep_clean(val: Any, key_name: str = "") -> Any:
            if key_name in _TECHNICAL_KEYS:
                return val
            if isinstance(val, str):
                cleaned_str = _normalize_homoglyphs(val)
                # Clean telegram semicolons mid-sentence into commas
                cleaned_str = re.sub(r";(?=\s+[а-яё])", ",", cleaned_str)
                return cleaned_str
            if isinstance(val, list):
                return [_deep_clean(item, key_name) for item in val]
            if isinstance(val, dict):
                return {k: _deep_clean(v, k) for k, v in val.items()}
            return val

        cleaned_payload = _deep_clean(parsed)
        if not isinstance(cleaned_payload, dict):
            raise ValueError("cleaned article writer response is not a JSON object")
        return cleaned_payload

    async def generate_from_analysis_and_bundle(  # noqa: C901
        self,
        analysis: EditorialAnalysis,
        writer_bundle: PreparedBundle,
        attempt_observer: Any | None = None,
        bundle_for_fallback: PreparedBundle | None = None,
        edition_slug: str = "",
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
        if self.historical_retriever is not None and edition_slug.strip():
            try:
                hist_backgrounds = await self.historical_retriever.retrieve_for_stories(
                    analysis, edition_slug=edition_slug.strip()
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
        self,
        messages_by_channel: Dict[str, List[Message]],
        *,
        edition_slug: str = "",
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
            edition_slug=edition_slug,
        )
