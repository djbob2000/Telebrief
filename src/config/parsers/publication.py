"""Publication, digest, and article configuration parsers."""

from __future__ import annotations

import logging
import math
import os
import re
from typing import Literal, cast

from src.config.schemas.publication import (
    _DIGEST_RUBRIC_ID_RE,
    ALLOWED_REASONING_EFFORTS,
    DEFAULT_DIGEST_RUBRIC,
    EVENT_PIPELINE_MODES,
    ArticleConfig,
    ArticleScheduleConfig,
    DigestGroupConfig,
    DigestRubricConfig,
    DigestRubricsConfig,
    EditionScopeConfig,
    EventPipelineConfig,
    PublicationEditorialConfig,
)
from src.config.schemas.root import SUPPORTED_LANGUAGES

_SUPPORTED_PROVIDERS = {"openai", "ollama", "anthropic", "google", "openrouter"}
_PROVIDER_DEFAULT_MODELS = {
    "openai": "gpt-5-nano",
    "anthropic": "claude-sonnet-4-5-20250929",
    "ollama": "llama3",
    "google": "gemini-3.6-flash",
    "openrouter": "minimax/minimax-m3:free:floor",
}


def _resolve_ai_settings(settings_dict: dict) -> tuple:
    """Resolve ai_provider and ai_model from settings dict and environment variables."""
    raw_provider = os.getenv("AI_PROVIDER") or settings_dict.get("ai_provider", "openai")
    if not isinstance(raw_provider, str):
        raise ValueError(f"ai_provider must be a string, got {type(raw_provider).__name__}")
    ai_provider = raw_provider.lower()

    if ai_provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported ai_provider: '{ai_provider}'. "
            f"Supported providers: {', '.join(sorted(_SUPPORTED_PROVIDERS))}"
        )

    default_model = _PROVIDER_DEFAULT_MODELS[ai_provider]

    env_model = os.getenv("AI_MODEL")
    if not env_model:
        if ai_provider == "openrouter":
            env_model = os.getenv("OPENROUTER_MODEL")
        elif ai_provider == "openai":
            env_model = os.getenv("OPENAI_MODEL")

    ai_model = (
        env_model
        or settings_dict.get("ai_model")
        or (
            settings_dict.get("openai_model", default_model)
            if ai_provider == "openai"
            else default_model
        )
    )

    return ai_provider, ai_model


def _parse_digest_settings(
    settings_dict: dict,
) -> tuple[str, list[DigestGroupConfig], str]:
    """Parse digest_mode, digest_groups, and output_language from settings."""
    digest_mode = settings_dict.get("digest_mode", "channel")
    if digest_mode not in ("channel", "digest"):
        raise ValueError(f"Invalid digest_mode: '{digest_mode}'. Must be 'channel' or 'digest'.")

    digest_groups = []
    raw_groups = settings_dict.get("digest_groups") or []
    for i, g in enumerate(raw_groups):
        if not isinstance(g, dict) or "name" not in g or "description" not in g:
            raise ValueError(
                f"digest_groups[{i}] must be a dict with 'name' and 'description' fields"
            )
        if not isinstance(g["name"], str) or not isinstance(g["description"], str):
            raise ValueError(f"digest_groups[{i}] 'name' and 'description' must be strings")
        group_prompt_extra = g.get("prompt_extra", "")
        if not isinstance(group_prompt_extra, str):
            raise ValueError(
                f"digest_groups[{i}].prompt_extra must be a string, "
                f"got {type(group_prompt_extra).__name__}"
            )
        digest_groups.append(
            DigestGroupConfig(
                name=g["name"],
                description=g["description"],
                prompt_extra=group_prompt_extra,
            )
        )

    output_language = settings_dict.get("output_language", "Russian")
    if output_language not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"Unsupported output_language: '{output_language}'. "
            f"Supported languages: {', '.join(SUPPORTED_LANGUAGES)}"
        )

    if digest_mode == "digest" and not digest_groups:
        logger = logging.getLogger("telebrief")
        logger.warning(
            "digest mode enabled but no digest_groups configured — all content will go to 'Other'"
        )

    return digest_mode, digest_groups, output_language


def _parse_digest_rubrics(settings_dict: dict) -> DigestRubricsConfig:
    """Parse and validate digest presentation rubrics from settings dict."""
    raw = settings_dict.get("digest_rubrics")
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError("settings.digest_rubrics must be a dict")
        min_similarity = raw.get("min_similarity", 0.38)
        if (
            isinstance(min_similarity, bool)
            or not isinstance(min_similarity, (int, float))
            or not (0.0 <= float(min_similarity) <= 1.0)
        ):
            raise ValueError("min_similarity must be between 0.0 and 1.0")
        min_similarity = float(min_similarity)

        raw_items = raw.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("digest_rubrics must contain at least one rubric")

        parsed_items: list[DigestRubricConfig] = []
        seen_ids: set[str] = set()

        for idx, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise ValueError(f"digest_rubrics.items[{idx}] must be a dict")
            rubric_id = item.get("id")
            if not isinstance(rubric_id, str) or not _DIGEST_RUBRIC_ID_RE.match(rubric_id):
                raise ValueError(f"invalid digest rubric id: {rubric_id!r}")
            if rubric_id in seen_ids:
                raise ValueError(f"duplicate digest rubric id: {rubric_id!r}")
            seen_ids.add(rubric_id)

            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"digest_rubrics.items[{idx}].name must be a non-empty string")

            description = item.get("description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(
                    f"digest_rubrics.items[{idx}].description must be a non-empty string"
                )

            emoji = item.get("emoji", "")
            if not isinstance(emoji, str):
                raise ValueError(f"digest_rubrics.items[{idx}].emoji must be a string")

            fallback = item.get("fallback", False)
            if not isinstance(fallback, bool):
                raise ValueError(f"digest_rubrics.items[{idx}].fallback must be a boolean")

            parsed_items.append(
                DigestRubricConfig(
                    id=rubric_id,
                    name=name.strip(),
                    description=description.strip(),
                    emoji=emoji.strip(),
                    fallback=fallback,
                )
            )

        fallback_matches = [r for r in parsed_items if r.fallback]
        if len(fallback_matches) != 1:
            raise ValueError("digest_rubrics must contain exactly one fallback rubric")

        return DigestRubricsConfig(
            min_similarity=min_similarity,
            items=tuple(parsed_items),
        )

    raw_groups = settings_dict.get("digest_groups")
    if isinstance(raw_groups, list) and raw_groups:
        compat_items: list[DigestRubricConfig] = []
        seen_ids = set()
        fallback_idx = None

        for idx, g in enumerate(raw_groups):
            if isinstance(g, dict):
                g_name = str(g.get("name", "")).strip()
                if g_name in ("Other", "Другое", "Прочее"):
                    fallback_idx = idx

        if fallback_idx is None:
            fallback_idx = len(raw_groups) - 1

        for idx, g in enumerate(raw_groups):
            if not isinstance(g, dict):
                continue
            g_name = str(g.get("name", f"group_{idx}")).strip()
            g_desc = str(g.get("description", g_name)).strip()
            raw_id = re.sub(r"[^a-z0-9_-]+", "_", g_name.lower()).strip("_")
            if not raw_id or not _DIGEST_RUBRIC_ID_RE.match(raw_id):
                raw_id = f"group_{idx}"
            orig_id = raw_id
            counter = 1
            while raw_id in seen_ids:
                raw_id = f"{orig_id}_{counter}"
                counter += 1
            seen_ids.add(raw_id)

            compat_items.append(
                DigestRubricConfig(
                    id=raw_id,
                    name=g_name,
                    description=g_desc,
                    fallback=(idx == fallback_idx),
                )
            )

        if compat_items:
            return DigestRubricsConfig(
                min_similarity=0.38,
                items=tuple(compat_items),
            )

    return DigestRubricsConfig(
        min_similarity=0.38,
        items=(DEFAULT_DIGEST_RUBRIC,),
    )


def _parse_edition_scopes(settings_dict: dict) -> dict[str, EditionScopeConfig]:
    raw = settings_dict.get("edition_scopes") or {}
    if not isinstance(raw, dict):
        raise ValueError("settings.edition_scopes must be a mapping")

    parsed: dict[str, EditionScopeConfig] = {}
    for raw_slug, item in raw.items():
        if not isinstance(raw_slug, str) or not raw_slug.strip():
            raise ValueError("edition scope slug must be a non-empty string")
        slug = raw_slug.strip()
        if not isinstance(item, dict):
            raise ValueError(f"edition_scopes.{slug} must be a mapping")

        name = item.get("name")
        places = item.get("focus_places")
        direct_impact_only = item.get("direct_impact_only", True)
        notes = item.get("notes", [])

        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"edition_scopes.{slug}.name must be a non-empty string")
        if not isinstance(places, list) or not places:
            raise ValueError(f"edition_scopes.{slug}.focus_places must be a non-empty list")
        if not isinstance(direct_impact_only, bool):
            raise ValueError(f"edition_scopes.{slug}.direct_impact_only must be a bool")
        if not isinstance(notes, list) or not all(isinstance(x, str) and x.strip() for x in notes):
            raise ValueError(f"edition_scopes.{slug}.notes must be a list of non-empty strings")

        normalized_places: list[str] = []
        seen: set[str] = set()
        for place in places:
            if not isinstance(place, str) or not place.strip():
                raise ValueError(f"edition_scopes.{slug}.focus_places must contain strings")
            clean = place.strip()
            key = clean.casefold()
            if key in seen:
                raise ValueError(f"duplicate focus place in edition_scopes.{slug}: {clean!r}")
            seen.add(key)
            normalized_places.append(clean)

        parsed[slug] = EditionScopeConfig(
            name=name.strip(),
            focus_places=tuple(normalized_places),
            direct_impact_only=direct_impact_only,
            notes=tuple(x.strip() for x in notes),
        )
    return parsed


def _parse_article_config(settings_dict: dict) -> ArticleConfig:
    """Parse article settings from settings dict."""
    raw = settings_dict.get("article")
    if raw is None:
        return ArticleConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"settings.article must be a mapping, got {type(raw).__name__}")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"settings.article.enabled must be a bool, got {type(enabled).__name__}")

    schedule_time = raw.get("schedule_time", "20:00")
    if not isinstance(schedule_time, str) or not schedule_time.strip():
        raise ValueError(
            f"settings.article.schedule_time must be a string, got {type(schedule_time).__name__}"
        )

    lookback_hours = raw.get("lookback_hours", 24)
    if (
        not isinstance(lookback_hours, int)
        or isinstance(lookback_hours, bool)
        or lookback_hours <= 0
    ):
        raise ValueError(
            f"settings.article.lookback_hours must be a positive int, got {lookback_hours!r}"
        )

    author_name = raw.get("author_name", "Бердянск Новости")
    if not isinstance(author_name, str) or not author_name.strip():
        raise ValueError(
            f"settings.article.author_name must be a non-empty string, got {author_name!r}"
        )

    fallback_save_dir = raw.get("fallback_save_dir", "data/articles")
    if not isinstance(fallback_save_dir, str) or not fallback_save_dir.strip():
        raise ValueError(
            f"settings.article.fallback_save_dir must be a non-empty string, got {fallback_save_dir!r}"
        )

    prompt_template = raw.get("prompt_template", "src/prompts/news_style.md")
    if not isinstance(prompt_template, str) or not prompt_template.strip():
        raise ValueError(
            f"settings.article.prompt_template must be a non-empty string, got {prompt_template!r}"
        )

    generation_retries = raw.get("generation_retries", 2)
    if (
        isinstance(generation_retries, bool)
        or not isinstance(generation_retries, int)
        or not 0 <= generation_retries <= 5
    ):
        raise ValueError("settings.article.generation_retries must be an integer between 0 and 5")

    generation_retry_delay = raw.get("generation_retry_delay", 1.0)
    if (
        isinstance(generation_retry_delay, bool)
        or not isinstance(generation_retry_delay, (int, float))
        or not math.isfinite(float(generation_retry_delay))
        or generation_retry_delay < 0
    ):
        raise ValueError("settings.article.generation_retry_delay must be a non-negative number")

    editorial_max_output_tokens = raw.get("editorial_max_output_tokens", 65_536)
    if (
        isinstance(editorial_max_output_tokens, bool)
        or not isinstance(editorial_max_output_tokens, int)
        or editorial_max_output_tokens <= 0
    ):
        raise ValueError("settings.article.editorial_max_output_tokens must be a positive integer")

    def _positive_budget(name: str, default: int) -> int:
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"settings.article.{name} must be a positive integer")
        return int(value)

    editorial_analysis_max_output_tokens = _positive_budget(
        "editorial_analysis_max_output_tokens", editorial_max_output_tokens
    )
    editorial_analysis_compact_max_output_tokens = _positive_budget(
        "editorial_analysis_compact_max_output_tokens",
        min(editorial_analysis_max_output_tokens, 16_384),
    )
    editorial_writer_max_output_tokens = _positive_budget(
        "editorial_writer_max_output_tokens", editorial_max_output_tokens
    )
    editorial_audit_max_output_tokens = _positive_budget(
        "editorial_audit_max_output_tokens", 32_768
    )
    editorial_repair_max_output_tokens = _positive_budget(
        "editorial_repair_max_output_tokens", min(editorial_audit_max_output_tokens, 8_192)
    )

    editorial_api_timeout = raw.get("editorial_api_timeout", 300)
    if (
        isinstance(editorial_api_timeout, bool)
        or not isinstance(editorial_api_timeout, int)
        or editorial_api_timeout <= 0
    ):
        raise ValueError("settings.article.editorial_api_timeout must be a positive integer")

    token = raw.get("telegraph_access_token")
    if token is not None and not isinstance(token, str):
        raise ValueError(
            f"settings.article.telegraph_access_token must be a string or null, got {type(token).__name__}"
        )

    save_debug_artifacts = raw.get("save_debug_artifacts", False)
    if not isinstance(save_debug_artifacts, bool):
        raise ValueError("settings.article.save_debug_artifacts must be a bool")

    debug_artifact_dir = raw.get("debug_artifact_dir", "data/debug/editorial")
    if not isinstance(debug_artifact_dir, str) or not debug_artifact_dir.strip():
        raise ValueError("settings.article.debug_artifact_dir must be a non-empty string")

    raw_temp = raw.get("temperature")
    article_temp: float | None = None
    if raw_temp is not None:
        if isinstance(raw_temp, bool) or not isinstance(raw_temp, (int, float)):
            raise ValueError("settings.article.temperature must be a float or null")
        article_temp = float(raw_temp)
        if article_temp < 0.0 or article_temp > 2.0:
            raise ValueError("settings.article.temperature must be between 0.0 and 2.0")

    return ArticleConfig(
        enabled=enabled,
        schedule_time=schedule_time.strip(),
        lookback_hours=lookback_hours,
        author_name=author_name.strip(),
        fallback_save_dir=fallback_save_dir.strip(),
        prompt_template=prompt_template.strip(),
        generation_retries=generation_retries,
        generation_retry_delay=float(generation_retry_delay),
        editorial_max_output_tokens=editorial_max_output_tokens,
        editorial_analysis_max_output_tokens=editorial_analysis_max_output_tokens,
        editorial_analysis_compact_max_output_tokens=editorial_analysis_compact_max_output_tokens,
        editorial_writer_max_output_tokens=editorial_writer_max_output_tokens,
        editorial_audit_max_output_tokens=editorial_audit_max_output_tokens,
        editorial_repair_max_output_tokens=editorial_repair_max_output_tokens,
        editorial_api_timeout=editorial_api_timeout,
        telegraph_access_token=token.strip() if token else None,
        save_debug_artifacts=save_debug_artifacts,
        debug_artifact_dir=debug_artifact_dir.strip(),
        temperature=article_temp,
    )


def _parse_article_schedule_config(
    settings_dict: dict,
    key: str,
    default_day: str | int = "sunday",
    default_time: str = "19:00",
    default_words: int = 2000,
    default_lookback: int = 168,
) -> ArticleScheduleConfig:
    """Parse periodic article schedule settings (weekly_article or monthly_article)."""
    raw = settings_dict.get(key)
    if raw is None:
        return ArticleScheduleConfig(
            enabled=True,
            schedule_day=default_day,
            schedule_time=default_time,
            target_word_count=default_words,
            lookback_hours=default_lookback,
        )
    if not isinstance(raw, dict):
        raise ValueError(f"settings.{key} must be a mapping, got {type(raw).__name__}")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"settings.{key}.enabled must be a bool, got {type(enabled).__name__}")

    schedule_day = raw.get("schedule_day", default_day)
    if not isinstance(schedule_day, (str, int)):
        raise ValueError(
            f"settings.{key}.schedule_day must be a string or integer, got {type(schedule_day).__name__}"
        )

    schedule_time = raw.get("schedule_time", default_time)
    if not isinstance(schedule_time, str) or not schedule_time.strip():
        raise ValueError(
            f"settings.{key}.schedule_time must be a string, got {type(schedule_time).__name__}"
        )

    target_word_count = raw.get("target_word_count", default_words)
    if (
        not isinstance(target_word_count, int)
        or isinstance(target_word_count, bool)
        or target_word_count <= 0
    ):
        raise ValueError(
            f"settings.{key}.target_word_count must be a positive int, got {target_word_count!r}"
        )

    lookback_hours = raw.get("lookback_hours", default_lookback)
    if (
        not isinstance(lookback_hours, int)
        or isinstance(lookback_hours, bool)
        or lookback_hours <= 0
    ):
        raise ValueError(
            f"settings.{key}.lookback_hours must be a positive int, got {lookback_hours!r}"
        )

    return ArticleScheduleConfig(
        enabled=enabled,
        schedule_day=schedule_day
        if isinstance(schedule_day, int)
        else schedule_day.strip().lower(),
        schedule_time=schedule_time.strip(),
        target_word_count=target_word_count,
        lookback_hours=lookback_hours,
    )


def _parse_event_pipeline_config(settings_dict: dict) -> EventPipelineConfig:
    """Parse and validate settings.event_pipeline configuration."""
    raw = settings_dict.get("event_pipeline")
    if raw is None:
        return EventPipelineConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"'event_pipeline' must be a mapping, got {type(raw).__name__}")

    raw_mode = raw.get("mode", "event_first")
    if not isinstance(raw_mode, str) or raw_mode not in EVENT_PIPELINE_MODES:
        raise ValueError(
            f"settings.event_pipeline.mode must be one of {', '.join(EVENT_PIPELINE_MODES)}, "
            f"got {raw_mode!r}"
        )

    def _val_pos_int(key: str, default: int) -> int:
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ValueError(f"settings.event_pipeline.{key} must be a positive integer, got {v!r}")
        return int(v)

    def _val_range_int(key: str, default: int, min_val: int, max_val: int) -> int:
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or not (min_val <= v <= max_val):
            raise ValueError(
                f"settings.event_pipeline.{key} must be between {min_val} and {max_val}, got {v!r}"
            )
        return int(v)

    def _val_nonneg_int(key: str, default: int) -> int:
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(
                f"settings.event_pipeline.{key} must be a non-negative integer, got {v!r}"
            )
        return int(v)

    def _val_unit_float(key: str, default: float) -> float:
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0.0 < float(v) <= 1.0):
            raise ValueError(
                f"settings.event_pipeline.{key} must be between 0.0 and 1.0, got {v!r}"
            )
        return float(v)

    def _val_reasoning_effort(key: str, default: str | None) -> str | None:
        v = raw.get(key, default)
        if v is not None and (not isinstance(v, str) or v not in ALLOWED_REASONING_EFFORTS):
            raise ValueError(
                f"settings.event_pipeline.{key} must be one of "
                f"{sorted(ALLOWED_REASONING_EFFORTS)} or null, got {v!r}"
            )
        return v

    def _val_bool(key: str, default: bool) -> bool:
        v = raw.get(key, default)
        if not isinstance(v, bool):
            raise ValueError(f"settings.event_pipeline.{key} must be a boolean, got {v!r}")
        return bool(v)

    typed_mode = cast(Literal["event_first"], raw_mode)

    return EventPipelineConfig(
        mode=typed_mode,
        background_authority_enabled=_val_bool("background_authority_enabled", False),
        fragment_max_chars=_val_pos_int("fragment_max_chars", 1200),
        active_window_hours=_val_pos_int("active_window_hours", 72),
        join_similarity=_val_unit_float("join_similarity", 0.84),
        max_cluster_candidates=_val_pos_int("max_cluster_candidates", 20),
        embedding_batch_size=_val_pos_int("embedding_batch_size", 128),
        direct_analysis_min_fragments=_val_pos_int("direct_analysis_min_fragments", 3),
        direct_analysis_min_unique_sources=_val_pos_int("direct_analysis_min_unique_sources", 2),
        triage_batch_size=_val_range_int("triage_batch_size", 10, 1, 10),
        triage_max_output_tokens=_val_pos_int("triage_max_output_tokens", 12_288),
        triage_reasoning_effort=_val_reasoning_effort("triage_reasoning_effort", "low"),
        analysis_max_output_tokens=_val_pos_int("analysis_max_output_tokens", 8_192),
        analysis_reasoning_effort=_val_reasoning_effort("analysis_reasoning_effort", "low"),
        triage_max_attempts_per_assignment=_val_pos_int("triage_max_attempts_per_assignment", 2),
        analysis_max_attempts_per_assignment=_val_pos_int(
            "analysis_max_attempts_per_assignment", 2
        ),
        triage_excerpt_chars=_val_pos_int("triage_excerpt_chars", 320),
        triage_min_ignore_confidence=_val_unit_float("triage_min_ignore_confidence", 0.95),
        analysis_quiet_seconds=_val_nonneg_int("analysis_quiet_seconds", 120),
        analysis_min_interval_seconds=_val_nonneg_int("analysis_min_interval_seconds", 600),
        analysis_min_new_fragments=_val_pos_int("analysis_min_new_fragments", 3),
        analysis_max_calls_per_story_per_hour=_val_pos_int(
            "analysis_max_calls_per_story_per_hour", 4
        ),
        provider_retry_backoff_seconds=_val_nonneg_int("provider_retry_backoff_seconds", 300),
        provider_retry_backoff_max_seconds=_val_pos_int(
            "provider_retry_backoff_max_seconds", 3_600
        ),
        analysis_max_input_chars=_val_pos_int("analysis_max_input_chars", 24000),
        representative_fragment_limit=_val_pos_int("representative_fragment_limit", 16),
        rich_analysis_max_calls_per_cycle=_val_pos_int("rich_analysis_max_calls_per_cycle", 40),
        event_processing_cycle_lease_seconds=_val_pos_int(
            "event_processing_cycle_lease_seconds", 600
        ),
        revision_claim_lease_seconds=_val_pos_int("revision_claim_lease_seconds", 1_800),
        event_processing_stage_lease_seconds=_val_pos_int(
            "event_processing_stage_lease_seconds", 600
        ),
        authority_provider_timeout_seconds=_val_pos_int("authority_provider_timeout_seconds", 540),
        triage_split_max_extra_calls_per_cycle=_val_pos_int(
            "triage_split_max_extra_calls_per_cycle", 8
        ),
        live_batch_size=_val_pos_int("live_batch_size", 100),
        backfill_batch_size=_val_pos_int("backfill_batch_size", 500),
    )


def _parse_publication_editorial_config(settings_dict: dict) -> PublicationEditorialConfig:
    """Parse and validate settings.publication_editorial configuration."""
    raw = settings_dict.get("publication_editorial")
    if raw is None:
        return PublicationEditorialConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"'publication_editorial' must be a mapping, got {type(raw).__name__}")

    def _val_pos_int(key: str, default: int) -> int:
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ValueError(
                f"settings.publication_editorial.{key} must be a positive integer, got {v!r}"
            )
        return int(v)

    def _val_nonneg_int(key: str, default: int) -> int:
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(
                f"settings.publication_editorial.{key} must be a non-negative integer, got {v!r}"
            )
        return int(v)

    def _val_reasoning_effort(key: str, default: str | None) -> str | None:
        v = raw.get(key, default)
        if v is not None and (not isinstance(v, str) or v not in ALLOWED_REASONING_EFFORTS):
            raise ValueError(
                f"settings.publication_editorial.{key} must be one of "
                f"{sorted(ALLOWED_REASONING_EFFORTS)} or null, got {v!r}"
            )
        return v

    mode_val = raw.get("digest_narrative_mode", "deterministic")
    if not isinstance(mode_val, str) or mode_val not in (
        "deterministic",
        "single_call",
        "journalistic",
    ):
        raise ValueError(
            f"settings.publication_editorial.digest_narrative_mode must be 'deterministic', 'single_call', or 'journalistic', got {mode_val!r}"
        )

    return PublicationEditorialConfig(
        conflict_window_minutes=_val_pos_int("conflict_window_minutes", 90),
        article_min_words=_val_pos_int("article_min_words", 800),
        article_max_words=_val_pos_int("article_max_words", 2200),
        article_min_sections=_val_pos_int("article_min_sections", 3),
        article_max_sections=_val_pos_int("article_max_sections", 8),
        article_max_direct_quotes=_val_nonneg_int("article_max_direct_quotes", 4),
        digest_narrative_mode=mode_val,
        digest_narrative_max_cards_per_block=_val_pos_int(
            "digest_narrative_max_cards_per_block", 6
        ),
        digest_narrative_max_output_tokens=_val_pos_int("digest_narrative_max_output_tokens", 4096),
        selection_max_output_tokens=_val_pos_int("selection_max_output_tokens", 4096),
        selection_reasoning_effort=_val_reasoning_effort("selection_reasoning_effort", "low"),
        digest_city_situation_max_items=_val_pos_int("digest_city_situation_max_items", 7),
        digest_city_situation_max_details_per_item=_val_pos_int(
            "digest_city_situation_max_details_per_item", 2
        ),
        digest_city_situation_max_positive_items=_val_nonneg_int(
            "digest_city_situation_max_positive_items", 2
        ),
        article_allow_deterministic_fallback=bool(
            raw.get("article_allow_deterministic_fallback", False)
        ),
        article_editor_enabled=bool(raw.get("article_editor_enabled", False)),
        article_editor_max_attempts=_val_pos_int("article_editor_max_attempts", 2),
    )
