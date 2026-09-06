"""Configuration file and environment loader orchestration."""

from __future__ import annotations

import os
import sys

import yaml
from dotenv import load_dotenv

from src.config.parsers.common import (
    _parse_collection_config,
    _parse_database_config,
    _parse_embedding_config,
    _parse_filter_specs,
    _parse_mcp_config,
    _parse_prompts_config,
    _parse_storage_config,
)
from src.config.parsers.facebook import _parse_facebook_config
from src.config.parsers.publication import (
    _parse_article_config,
    _parse_article_schedule_config,
    _parse_digest_rubrics,
    _parse_digest_settings,
    _parse_edition_scopes,
    _parse_event_pipeline_config,
    _parse_publication_editorial_config,
    _resolve_ai_settings,
)
from src.config.parsers.telegram import (
    _parse_channels,
    _parse_telegram_config,
    _validate_channel_groups,
)
from src.config.schemas.common import FORBIDDEN_AI_MODELS
from src.config.schemas.root import VISION_MODES, Config, Settings


def _load_and_validate_env_vars(
    ai_provider: str,
    *,
    embedding_provider: str | None = None,
    ai_model: str = "",
) -> dict:
    """Load and validate required environment variables."""
    telegram_api_id = os.getenv("TELEGRAM_API_ID")
    telegram_api_hash = os.getenv("TELEGRAM_API_HASH")
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    openai_base_url = os.getenv("OPENAI_BASE_URL", "")
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    google_api_keys: list[str] = []
    primary_google_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
    if primary_google_key:
        google_api_keys.append(primary_google_key)

    for i in range(2, 21):
        k = os.getenv(f"GEMINI_API_KEY_{i}") or os.getenv(f"GOOGLE_API_KEY_{i}", "")
        if k and k not in google_api_keys:
            google_api_keys.append(k)

    google_api_key = primary_google_key
    google_api_key_2 = os.getenv("GEMINI_API_KEY_2") or os.getenv("GOOGLE_API_KEY_2", "")
    google_api_key_3 = os.getenv("GEMINI_API_KEY_3") or os.getenv("GOOGLE_API_KEY_3", "")

    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_base_url = os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
    openrouter_model = os.getenv("OPENROUTER_MODEL") or (
        ai_model if ai_provider == "openrouter" and ai_model else "openrouter/free"
    )
    openrouter_model_2 = os.getenv("OPENROUTER_MODEL_2", "")
    for m in (openrouter_model, openrouter_model_2):
        if m in FORBIDDEN_AI_MODELS:
            raise ValueError(
                f"Model {m!r} is strictly forbidden by project rules. "
                "Use 'minimax/minimax-m3:free:floor' or 'deepseek/deepseek-v4-flash-0731:floor'."
            )
    openrouter_image_model = (
        os.getenv("OPENROUTER_IMAGE_MODEL") or "google/gemini-3.1-flash-lite-image"
    )
    log_level = os.getenv("LOG_LEVEL", "INFO")

    missing_vars = []
    if not telegram_api_id:
        missing_vars.append("TELEGRAM_API_ID")
    if not telegram_api_hash:
        missing_vars.append("TELEGRAM_API_HASH")
    if not telegram_bot_token:
        missing_vars.append("TELEGRAM_BOT_TOKEN")

    if ai_provider == "openai" and not openai_api_key:
        missing_vars.append("OPENAI_API_KEY")
    elif ai_provider == "anthropic" and not anthropic_api_key:
        missing_vars.append("ANTHROPIC_API_KEY")
    elif ai_provider == "google" and not google_api_key:
        missing_vars.append("GEMINI_API_KEY")
    elif ai_provider == "openrouter" and not openrouter_api_key:
        missing_vars.append("OPENROUTER_API_KEY")

    if (
        embedding_provider == "openrouter"
        and not openrouter_api_key
        and "OPENROUTER_API_KEY" not in missing_vars
    ):
        missing_vars.append("OPENROUTER_API_KEY")
    elif (
        embedding_provider == "openai"
        and not openai_api_key
        and "OPENAI_API_KEY" not in missing_vars
    ):
        missing_vars.append("OPENAI_API_KEY")

    if missing_vars:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing_vars)}\n"
            f"Please set them in .env file (see .env.example)"
        )

    if telegram_api_id is None or telegram_api_hash is None or telegram_bot_token is None:
        raise ValueError("Missing required Telegram credentials")

    return {
        "telegram_api_id": int(telegram_api_id),
        "telegram_api_hash": telegram_api_hash,
        "telegram_bot_token": telegram_bot_token,
        "openai_api_key": openai_api_key,
        "openai_base_url": openai_base_url,
        "anthropic_api_key": anthropic_api_key,
        "google_api_key": google_api_key,
        "google_api_key_2": google_api_key_2,
        "google_api_key_3": google_api_key_3,
        "google_api_keys": google_api_keys,
        "openrouter_api_key": openrouter_api_key,
        "openrouter_base_url": openrouter_base_url,
        "openrouter_model": openrouter_model,
        "openrouter_model_2": openrouter_model_2,
        "openrouter_image_model": openrouter_image_model,
        "log_level": log_level,
    }


def load_config(config_path: str | None = None, *, path: str | None = None) -> Config:
    """Load configuration from YAML file and environment variables."""
    if path is not None:
        if config_path is not None and config_path != path:
            raise ValueError("Pass the config file via config_path or path, not both")
        config_path = path
    elif config_path is None:
        config_path = "config.yaml"

    legacy_mod = sys.modules.get("src.config_loader")
    if legacy_mod is not None and hasattr(legacy_mod, "load_dotenv"):
        legacy_mod.load_dotenv()
    else:
        load_dotenv()

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        yaml_config = yaml.safe_load(f)

    channels = _parse_channels(yaml_config)
    storage_config = _parse_storage_config(yaml_config)
    database_config = _parse_database_config(yaml_config)
    collection_config = _parse_collection_config(yaml_config)
    prompts_config = _parse_prompts_config(yaml_config)
    mcp_config = _parse_mcp_config(yaml_config)
    telegram_config = _parse_telegram_config(yaml_config)
    facebook_config = _parse_facebook_config(yaml_config)

    settings_dict = yaml_config.get("settings", {})
    if "schedule_jobs" in settings_dict:
        raise ValueError(
            "settings.schedule_jobs is no longer supported; "
            "use schedule_time and lookback_hours for one daily digest"
        )
    ai_provider, ai_model = _resolve_ai_settings(settings_dict)
    digest_mode, digest_groups, output_language = _parse_digest_settings(settings_dict)
    persistent_ingestion = settings_dict.get("persistent_ingestion", False)
    if not isinstance(persistent_ingestion, bool):
        raise ValueError(
            f"settings.persistent_ingestion must be a bool, got {type(persistent_ingestion).__name__}"
        )
    raw_global_filters = settings_dict.get("filters")
    global_filters = _parse_filter_specs(
        raw_global_filters if raw_global_filters is not None else [],
        "settings.filters",
    )
    vision_mode = settings_dict.get("vision_mode", "relevance_only")
    if not isinstance(vision_mode, str) or vision_mode not in VISION_MODES:
        raise ValueError(
            f"settings.vision_mode must be one of {', '.join(VISION_MODES)}, got {vision_mode!r}"
        )
    pre_publish_lead_minutes = settings_dict.get("pre_publish_lead_minutes", 15)
    if (
        not isinstance(pre_publish_lead_minutes, int)
        or isinstance(pre_publish_lead_minutes, bool)
        or not 0 <= pre_publish_lead_minutes <= 120
    ):
        raise ValueError(
            "settings.pre_publish_lead_minutes must be an int between 0 and 120, "
            f"got {pre_publish_lead_minutes!r}"
        )

    settings = Settings(
        schedule_time=settings_dict.get("schedule_time", "08:00"),
        timezone=settings_dict.get("timezone", "UTC"),
        lookback_hours=settings_dict.get("lookback_hours", 24),
        openai_model=settings_dict.get("openai_model", "gpt-5-nano"),
        openai_temperature=settings_dict.get("openai_temperature", 0.7),
        temperature=settings_dict.get("temperature", settings_dict.get("openai_temperature", 0.7)),
        max_tokens_per_summary=settings_dict.get("max_tokens_per_summary", 96000),
        use_emojis=settings_dict.get("use_emojis", True),
        include_statistics=settings_dict.get("include_statistics", True),
        target_user_id=settings_dict.get("target_user_id", 0),
        target_chat_id=settings_dict.get("target_chat_id", settings_dict.get("target_user_id", 0)),
        auto_cleanup_old_digests=settings_dict.get("auto_cleanup_old_digests", True),
        max_messages_per_channel=settings_dict.get("max_messages_per_channel", 5000),
        max_prompt_chars=settings_dict.get("max_prompt_chars", 8000),
        api_timeout=int(settings_dict.get("api_timeout", 300)),
        ai_provider=ai_provider,
        ai_model=ai_model,
        ollama_base_url=settings_dict.get("ollama_base_url", "http://localhost:11434"),
        output_language=output_language,
        digest_mode=digest_mode,
        digest_groups=digest_groups,
        filters=global_filters,
        dedup_topics=bool(settings_dict.get("dedup_topics", False)),
        persistent_ingestion=persistent_ingestion,
        reasoning_effort=(
            str(settings_dict["reasoning_effort"]).strip()
            if settings_dict.get("reasoning_effort") is not None
            else None
        ),
        vision_mode=vision_mode,
        pre_publish_lead_minutes=pre_publish_lead_minutes,
        article=_parse_article_config(settings_dict),
        event_pipeline=_parse_event_pipeline_config(settings_dict),
        digest_rubrics=_parse_digest_rubrics(settings_dict),
        edition_scopes=_parse_edition_scopes(settings_dict),
        publication_editorial=_parse_publication_editorial_config(settings_dict),
        weekly_article=_parse_article_schedule_config(
            settings_dict,
            "weekly_article",
            default_day="sunday",
            default_time="19:00",
            default_words=2000,
            default_lookback=168,
        ),
        monthly_article=_parse_article_schedule_config(
            settings_dict,
            "monthly_article",
            default_day=1,
            default_time="10:00",
            default_words=3500,
            default_lookback=720,
        ),
    )

    if settings.target_user_id == 0:
        raise ValueError(
            "target_user_id not configured in config.yaml. "
            "Get your Telegram user ID from @userinfobot"
        )

    if settings.persistent_ingestion and not database_config.enabled:
        raise ValueError(
            "settings.persistent_ingestion requires database.enabled=true in config.yaml: "
            "digest inputs are read from the PostgreSQL source history when the flag is set"
        )

    _validate_channel_groups(channels, digest_groups, output_language)

    raw_embedding = yaml_config.get("embedding")
    embedding_provider = raw_embedding.get("provider") if isinstance(raw_embedding, dict) else None

    env_vars = _load_and_validate_env_vars(
        ai_provider,
        embedding_provider=embedding_provider,
        ai_model=ai_model,
    )

    return Config(
        channels=channels,
        settings=settings,
        storage=storage_config,
        prompts=prompts_config,
        mcp=mcp_config,
        database=database_config,
        collection=collection_config,
        telegram=telegram_config,
        embedding=_parse_embedding_config(yaml_config, env_vars=env_vars),
        facebook=facebook_config,
        **env_vars,
    )
