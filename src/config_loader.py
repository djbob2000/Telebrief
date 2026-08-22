"""
Configuration loader for Telebrief.
Loads settings from config.yaml and environment variables.
"""

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, List

import yaml
from dotenv import load_dotenv


@dataclass
class FilterSpec:
    """Specification for a single message filter in a filter chain."""

    class_path: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ForumTopicConfig:
    """A selected forum topic within a Telegram group."""

    id: int
    name: str
    source_type: str | None = None


@dataclass
class ChannelConfig:
    """Configuration for a single Telegram channel/chat."""

    id: str | int  # str for @username, int for numeric Telegram channel ID
    name: str
    lookback_hours: int | None = None  # None = use global settings.lookback_hours
    prompt_extra: str = ""  # appended to system prompt when summarizing this channel
    filters: list[FilterSpec] | None = None  # None=use global, []=explicit no-op
    group: str | None = None  # must reference digest_groups[*].name, "Other", or None
    topics: list[ForumTopicConfig] = field(default_factory=list)
    source_type: str = "mixed"


@dataclass
class DigestGroupConfig:
    """Configuration for a single digest topic group."""

    name: str
    description: str
    prompt_extra: str = ""  # appended to system prompt for channels in this group


@dataclass
class CollectionConfig:
    """Generic ingestion collection scheduling.

    ``telegram_interval_minutes`` is the polling cadence bootstrap applies to
    every managed Telegram source: at bootstrap time it is mirrored into each
    source's ``collector_options.schedule.interval_minutes``. Valid range is
    5..360 minutes; enforced when loading YAML, not by the dataclass itself.
    """

    telegram_interval_minutes: int = 45


@dataclass
class PromptsConfig:
    """Configuration for prompt template and composer."""

    base_template: str = "src/prompts/base_summary.txt"
    composer: str = ""  # empty = DefaultComposer; otherwise dotted class path


@dataclass
class StorageConfig:
    """Configuration for the persistent message storage backend."""

    enabled: bool = False
    backend: str = "sqlite"  # "sqlite" | "postgres"
    path: str = "data/messages.db"
    url: str = field(
        default="", repr=False
    )  # postgres only; repr=False prevents credential exposure in logs


@dataclass
class DatabaseConfig:
    """Configuration for the PostgreSQL domain store and Procrastinate queue."""

    enabled: bool = False
    url: str = field(
        default="", repr=False
    )  # from DATABASE_URL env var; repr=False prevents credential exposure in logs
    min_pool_size: int = 1
    max_pool_size: int = 4
    domain_schema: str = "public"
    procrastinate_schema: str = "procrastinate"


@dataclass
class McpConfig:
    """Configuration for the built-in MCP server."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/mcp"


@dataclass
class ArticleConfig:
    """Configuration for daily editorial article generation."""

    enabled: bool = True
    schedule_time: str = "20:00"
    lookback_hours: int = 24
    author_name: str = "Бердянск Новости"
    fallback_save_dir: str = "data/articles"
    prompt_template: str = ".agents/skills/news-style/SKILL.md"
    generation_retries: int = 2  # Deprecated: retained for schema backwards compatibility
    generation_retry_delay: float = 1.0  # Deprecated: retained for schema backwards compatibility
    # Legacy shared budget retained for backward-compatible configs. New long-form
    # stages use the explicit budgets below when they are present.
    editorial_max_output_tokens: int = 65_536
    editorial_analysis_max_output_tokens: int = 65_536
    editorial_analysis_compact_max_output_tokens: int = 16_384
    editorial_writer_max_output_tokens: int = 65_536
    editorial_audit_max_output_tokens: int = 32_768
    editorial_repair_max_output_tokens: int = 8_192
    editorial_api_timeout: int = 300
    telegraph_access_token: str | None = None
    save_debug_artifacts: bool = False
    debug_artifact_dir: str = "data/debug/editorial"
    temperature: float | None = None


@dataclass
class Settings:
    """Application settings."""

    schedule_time: str
    timezone: str
    lookback_hours: int
    openai_model: str
    openai_temperature: float
    temperature: float = 0.7
    max_tokens_per_summary: int = 96000
    use_emojis: bool = True
    include_statistics: bool = True
    target_user_id: int = 0
    target_chat_id: str | int | None = None
    auto_cleanup_old_digests: bool = True
    max_messages_per_channel: int = 5000
    max_prompt_chars: int = 8000
    api_timeout: int = 30
    ai_provider: str = "openai"
    ai_model: str = ""
    ollama_base_url: str = "http://localhost:11434"
    output_language: str = "Russian"
    digest_mode: str = "channel"
    digest_groups: List[DigestGroupConfig] = field(default_factory=list)
    filters: list[FilterSpec] = field(default_factory=list)
    dedup_topics: bool = False
    # Transitional migration flag (Plan 2 Task 7): when true, digest/article
    # inputs are read from the PostgreSQL source history and Telethon is never
    # invoked at publication time. Requires database.enabled=true.
    persistent_ingestion: bool = False
    reasoning_effort: str | None = None
    article: ArticleConfig = field(default_factory=ArticleConfig)


@dataclass
class Config:
    """Complete application configuration."""

    channels: List[ChannelConfig]
    settings: Settings

    # Environment variables
    telegram_api_id: int
    telegram_api_hash: str
    telegram_bot_token: str
    openai_api_key: str
    log_level: str
    google_api_key: str = ""
    google_api_key_2: str = ""
    google_api_key_3: str = ""
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openrouter/free"
    openrouter_image_model: str = "google/gemini-3.1-flash-lite-image"
    openai_base_url: str = ""
    anthropic_api_key: str = ""
    google_api_keys: list[str] = field(default_factory=list)
    storage: StorageConfig = field(default_factory=StorageConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    collection: CollectionConfig = field(default_factory=CollectionConfig)

    @property
    def gemini_api_key(self) -> str:
        return self.google_api_key

    @property
    def gemini_api_key_2(self) -> str:
        return self.google_api_key_2

    @property
    def gemini_api_key_3(self) -> str:
        return self.google_api_key_3

    @property
    def gemini_api_key_4(self) -> str:
        return self.google_api_keys[3] if len(self.google_api_keys) > 3 else ""

    @property
    def gemini_api_key_5(self) -> str:
        return self.google_api_keys[4] if len(self.google_api_keys) > 4 else ""

    @property
    def google_api_backup_keys(self) -> list[str]:
        """Return all backup Gemini keys (from index 1 onwards)."""
        return self.google_api_keys[1:] if len(self.google_api_keys) > 1 else []


SUPPORTED_LANGUAGES = ("English", "Russian", "Spanish", "German", "French")
SOURCE_TYPES = ("news", "community", "official", "classifieds", "mixed")


def _normalize_source_type(value: object, label: str, *, allow_none: bool = False) -> str | None:
    """Normalize and validate an editorial source role."""
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        suffix = " or null" if allow_none else ""
        raise ValueError(f"{label} must be one of {', '.join(SOURCE_TYPES)}{suffix}")
    normalized = value.strip().lower()
    if normalized not in SOURCE_TYPES:
        raise ValueError(f"{label} must be one of {', '.join(SOURCE_TYPES)}, got {value!r}")
    return normalized


def effective_source_type(channel: ChannelConfig, topic: ForumTopicConfig | None = None) -> str:
    """Return the configured editorial role using topic > channel > mixed precedence."""
    return (topic.source_type if topic and topic.source_type else channel.source_type) or "mixed"


class SourceRoleResolver:
    """Resolve configured editorial roles without inferring them from names."""

    def __init__(self, channels: list[ChannelConfig]):
        self._channels = channels

    def resolve(self, channel_name: str, topic_id: int | None = None) -> str:
        for channel in self._channels:
            logical_prefix = f"{channel.name} — "
            if channel_name != channel.name and not channel_name.startswith(logical_prefix):
                continue

            topic_name = (
                channel_name[len(logical_prefix) :]
                if channel_name.startswith(logical_prefix)
                else None
            )
            for topic in channel.topics:
                if (topic_id is not None and topic.id == topic_id) or (
                    topic_name is not None and topic.name == topic_name
                ):
                    return effective_source_type(channel, topic)
            return effective_source_type(channel)
        return "mixed"


_SUPPORTED_PROVIDERS = {"openai", "ollama", "anthropic", "google", "openrouter"}
_PROVIDER_DEFAULT_MODELS = {
    "openai": "gpt-5-nano",
    "anthropic": "claude-sonnet-4-5-20250929",
    "ollama": "llama3",
    "google": "gemini-3.6-flash",
    "openrouter": "openrouter/free",
}


def _resolve_ai_settings(settings_dict: dict) -> tuple:
    """Resolve ai_provider and ai_model from settings dict.

    Returns:
        Tuple of (ai_provider, ai_model)

    Raises:
        ValueError: If ai_provider is unsupported
    """
    raw_provider = settings_dict.get("ai_provider", "openai")
    if not isinstance(raw_provider, str):
        raise ValueError(f"ai_provider must be a string, got {type(raw_provider).__name__}")
    ai_provider = raw_provider.lower()

    if ai_provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported ai_provider: '{ai_provider}'. "
            f"Supported providers: {', '.join(sorted(_SUPPORTED_PROVIDERS))}"
        )

    default_model = _PROVIDER_DEFAULT_MODELS[ai_provider]

    # ai_model takes priority; openai_model is only a fallback for the openai provider
    ai_model = settings_dict.get("ai_model") or (
        settings_dict.get("openai_model", default_model)
        if ai_provider == "openai"
        else default_model
    )

    return ai_provider, ai_model


def _parse_digest_settings(
    settings_dict: dict,
) -> tuple[str, list[DigestGroupConfig], str]:
    """Parse digest_mode, digest_groups, and output_language from settings.

    Returns:
        Tuple of (digest_mode, digest_groups, output_language)
    """
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


def _parse_article_config(settings_dict: dict) -> ArticleConfig:  # noqa: C901
    """Parse article settings from settings dict.

    Raises:
        ValueError: If settings.article has invalid types or negative lookback_hours.
    """
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

    prompt_template = raw.get("prompt_template", ".agents/skills/news-style/SKILL.md")
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

    # Explicit stage budgets override the legacy shared value. This keeps old
    # config files valid while allowing long-form analysis, writing and audit to
    # have different completion limits.
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


def _validate_dotted_path(value: str, label: str) -> str:
    """Validate a YAML-string dotted path (e.g. 'pkg.module.ClassName').

    Returns the stripped value. Raises ValueError if the value is not a non-empty
    string or does not parse as ≥2 dot-separated identifier segments.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string, got {value!r}")
    stripped = value.strip()
    segments = stripped.split(".")
    if len(segments) < 2 or not all(seg.isidentifier() for seg in segments):
        raise ValueError(
            f"{label} must be a dotted path (e.g. 'pkg.module.ClassName'), got {value!r}"
        )
    return stripped


def _parse_filter_specs(raw_list: object, path_label: str) -> list[FilterSpec]:
    """Parse and validate a list of filter specs from YAML.

    Raises:
        ValueError: If the list or any entry has wrong type or missing required fields.
    """
    if not isinstance(raw_list, list):
        raise ValueError(f"'{path_label}' must be a list, got {type(raw_list).__name__}")
    specs: list[FilterSpec] = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            raise ValueError(f"{path_label}[{i}] must be a mapping, got {type(item).__name__}")
        if "class_path" not in item:
            raise ValueError(f"{path_label}[{i}] missing required field 'class_path'")
        class_path = _validate_dotted_path(item["class_path"], f"{path_label}[{i}].class_path")
        config = item.get("config", {})
        if not isinstance(config, dict):
            raise ValueError(
                f"{path_label}[{i}].config must be a mapping, got {type(config).__name__}"
            )
        specs.append(FilterSpec(class_path=class_path, config=config))
    return specs


def _validate_channel_lookback(i: int, ch: dict) -> int | None:
    lookback_hours = ch.get("lookback_hours")
    if lookback_hours is None:
        return None
    if not isinstance(lookback_hours, int) or isinstance(lookback_hours, bool):
        raise ValueError(
            f"channels[{i}].lookback_hours must be an int, got {type(lookback_hours).__name__}"
        )
    if lookback_hours <= 0:
        raise ValueError(f"channels[{i}].lookback_hours must be positive, got {lookback_hours}")
    return lookback_hours


def _validate_channel_group(i: int, ch: dict) -> str | None:
    group = ch.get("group")
    if group is None:
        return None
    if not isinstance(group, str) or not group.strip():
        raise ValueError(f"channels[{i}].group must be a non-empty string or null, got {group!r}")
    return group.strip()


def _validate_channel_source_type(i: int, ch: dict) -> str:
    """Validate a channel's optional editorial source role."""
    return (
        _normalize_source_type(ch.get("source_type", "mixed"), f"channels[{i}].source_type")
        or "mixed"
    )


def _validate_channel_topics(i: int, ch: dict) -> list[ForumTopicConfig]:
    raw_topics = ch.get("topics", [])
    if not isinstance(raw_topics, list):
        raise ValueError(f"channels[{i}].topics must be a list, got {type(raw_topics).__name__}")

    topics: list[ForumTopicConfig] = []
    for topic_index, raw_topic in enumerate(raw_topics):
        path = f"channels[{i}].topics[{topic_index}]"
        if not isinstance(raw_topic, dict):
            raise ValueError(f"{path} must be a mapping, got {type(raw_topic).__name__}")
        topic_id = raw_topic.get("id")
        if not isinstance(topic_id, int) or isinstance(topic_id, bool) or topic_id <= 0:
            raise ValueError(f"{path}.id must be a positive int, got {topic_id!r}")
        topic_name = raw_topic.get("name")
        if not isinstance(topic_name, str) or not topic_name.strip():
            raise ValueError(f"{path}.name must be a non-empty string, got {topic_name!r}")
        topic_source_type = _normalize_source_type(
            raw_topic.get("source_type"), f"{path}.source_type", allow_none=True
        )
        topics.append(
            ForumTopicConfig(
                id=topic_id,
                name=topic_name.strip(),
                source_type=topic_source_type,
            )
        )
    return topics


def _validate_channel_id_name(i: int, ch: dict) -> None:
    for required in ("id", "name"):
        if required not in ch:
            raise ValueError(f"channels[{i}] missing required field '{required}'")
    if not isinstance(ch["name"], str) or not ch["name"].strip():
        raise ValueError(f"channels[{i}].name must be a non-empty string, got {ch['name']!r}")
    if not isinstance(ch["id"], (str, int)) or isinstance(ch["id"], bool):
        raise ValueError(f"channels[{i}].id must be a string or int, got {type(ch['id']).__name__}")


def _parse_channel_entry(i: int, ch: object) -> ChannelConfig:
    """Parse and validate a single channel entry from YAML.

    Raises:
        ValueError: If the entry has wrong type, missing required fields, or invalid values
    """
    if not isinstance(ch, dict):
        raise ValueError(f"channels[{i}] must be a mapping, got {type(ch).__name__}")
    _validate_channel_id_name(i, ch)
    lookback_hours = _validate_channel_lookback(i, ch)
    prompt_extra = ch.get("prompt_extra", "")
    if not isinstance(prompt_extra, str):
        raise ValueError(
            f"channels[{i}].prompt_extra must be a string, got {type(prompt_extra).__name__}"
        )
    raw_filters = ch.get("filters")
    channel_filters: list[FilterSpec] | None = None
    if raw_filters is not None:
        channel_filters = _parse_filter_specs(raw_filters, f"channels[{i}].filters")
    return ChannelConfig(
        id=ch["id"],
        name=ch["name"],
        lookback_hours=lookback_hours,
        prompt_extra=prompt_extra,
        filters=channel_filters,
        group=_validate_channel_group(i, ch),
        topics=_validate_channel_topics(i, ch),
        source_type=_validate_channel_source_type(i, ch),
    )


def _parse_storage_config(yaml_config: dict) -> StorageConfig:
    """Parse and validate the optional top-level storage: block.

    Raises:
        ValueError: If any field has wrong type or invalid value.
    """
    raw = yaml_config.get("storage")
    if raw is None:
        return StorageConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"'storage' must be a mapping, got {type(raw).__name__}")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"storage.enabled must be a bool, got {type(enabled).__name__}")

    backend = raw.get("backend", "sqlite")
    if not isinstance(backend, str):
        raise ValueError(f"storage.backend must be a string, got {type(backend).__name__}")
    if backend not in ("sqlite", "postgres"):
        raise ValueError(f"storage.backend must be 'sqlite' or 'postgres', got {backend!r}")

    path = raw.get("path", "data/messages.db")
    if backend == "sqlite" and (not isinstance(path, str) or not path.strip()):
        raise ValueError("storage.path must be a non-empty string when backend is 'sqlite'")

    url = raw.get("url", "")
    if not isinstance(url, str):
        raise ValueError(f"storage.url must be a string, got {type(url).__name__}")
    if backend == "postgres" and enabled and not url.strip():
        raise ValueError("storage.url must be set when backend is 'postgres' and enabled is true")

    return StorageConfig(enabled=enabled, backend=backend, path=path, url=url)


def _parse_collection_config(yaml_config: dict) -> CollectionConfig:
    """Parse and validate the optional top-level collection: block.

    Absent block yields CollectionConfig() with telegram_interval_minutes=45.

    Raises:
        ValueError: If the block is not a mapping or the interval is not an
            int within 5..360.
    """
    raw = yaml_config.get("collection")
    if raw is None:
        return CollectionConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"'collection' must be a mapping, got {type(raw).__name__}")

    interval = raw.get("telegram_interval_minutes", 45)
    if isinstance(interval, bool) or not isinstance(interval, int):
        raise ValueError(
            "collection.telegram_interval_minutes must be an int between 5 and 360, "
            f"got {interval!r}"
        )
    if not 5 <= interval <= 360:
        raise ValueError(
            f"collection.telegram_interval_minutes must be between 5 and 360, got {interval}"
        )
    return CollectionConfig(telegram_interval_minutes=interval)


def _parse_database_config(yaml_config: dict, *, require_enabled: bool = False) -> DatabaseConfig:
    """Parse and validate the optional top-level database: block.

    The connection URL is read exclusively from the DATABASE_URL environment
    variable and must never be logged. When require_enabled is true (tooling
    that cannot run without PostgreSQL), a disabled or URL-less configuration
    is rejected with a clear error.

    Raises:
        ValueError: If any field has wrong type or an invalid value.
    """
    raw = yaml_config.get("database")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"'database' must be a mapping, got {type(raw).__name__}")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"database.enabled must be a bool, got {type(enabled).__name__}")

    min_pool_size = raw.get("min_pool_size", 1)
    max_pool_size = raw.get("max_pool_size", 4)
    if isinstance(min_pool_size, bool) or not isinstance(min_pool_size, int):
        raise ValueError(f"database.min_pool_size must be an int, got {min_pool_size!r}")
    if isinstance(max_pool_size, bool) or not isinstance(max_pool_size, int):
        raise ValueError(f"database.max_pool_size must be an int, got {max_pool_size!r}")
    if not 1 <= min_pool_size <= max_pool_size <= 10:
        raise ValueError(
            "database pool sizes must satisfy 1 <= min_pool_size <= max_pool_size <= 10, "
            f"got min_pool_size={min_pool_size}, max_pool_size={max_pool_size}"
        )

    domain_schema = raw.get("domain_schema", "public")
    if not isinstance(domain_schema, str) or not domain_schema.strip():
        raise ValueError("database.domain_schema must be a non-empty string")

    procrastinate_schema = raw.get("procrastinate_schema", "procrastinate")
    if not isinstance(procrastinate_schema, str) or not procrastinate_schema.strip():
        raise ValueError("database.procrastinate_schema must be a non-empty string")

    url = os.getenv("DATABASE_URL", "")

    if require_enabled and not enabled:
        raise ValueError(
            "database must be enabled when require_enabled is set: "
            "set database.enabled: true in config.yaml"
        )
    if enabled and not url.strip():
        raise ValueError(
            "DATABASE_URL must be set in the environment when database.enabled is true"
        )

    return DatabaseConfig(
        enabled=enabled,
        url=url.strip(),
        min_pool_size=min_pool_size,
        max_pool_size=max_pool_size,
        domain_schema=domain_schema.strip(),
        procrastinate_schema=procrastinate_schema.strip(),
    )


def load_database_config(
    path: str = "config.yaml", *, require_enabled: bool = False
) -> DatabaseConfig:
    """
    Load only the PostgreSQL database configuration.

    Unlike load_config(), this does not require Telegram/AI credentials, so it
    can be used by standalone workers and maintenance commands.

    Args:
        path: Path to config.yaml file
        require_enabled: Reject configurations that leave the database disabled

    Returns:
        DatabaseConfig object

    Raises:
        FileNotFoundError: If config file not found
        ValueError: If the database block has invalid values
    """
    load_dotenv()

    if not os.path.exists(path):
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        yaml_config = yaml.safe_load(f)

    if not isinstance(yaml_config, dict):
        raise ValueError(
            f"config.yaml must contain a top-level mapping, got {type(yaml_config).__name__}"
        )

    return _parse_database_config(yaml_config, require_enabled=require_enabled)


def _parse_mcp_config(yaml_config: dict) -> McpConfig:
    """Parse and validate the optional top-level mcp: block.

    Raises:
        ValueError: If any field has wrong type or invalid value.
    """
    raw = yaml_config.get("mcp")
    if raw is None:
        return McpConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"'mcp' must be a mapping, got {type(raw).__name__}")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"mcp.enabled must be a bool, got {type(enabled).__name__}")

    host = raw.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("mcp.host must be a non-empty string")

    port = raw.get("port", 8765)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError(f"mcp.port must be an int in 1..65535, got {port!r}")

    path = raw.get("path", "/mcp")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"mcp.path must be a string starting with '/', got {path!r}")

    return McpConfig(enabled=enabled, host=host.strip(), port=port, path=path)


def _parse_prompts_config(yaml_config: dict) -> PromptsConfig:
    """Parse and validate the optional top-level prompts: block.

    Raises:
        ValueError: If any field has wrong type or invalid value.
    """
    raw = yaml_config.get("prompts")
    if raw is None:
        return PromptsConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"'prompts' must be a mapping, got {type(raw).__name__}")

    base_template = raw.get("base_template", "src/prompts/base_summary.txt")
    if not isinstance(base_template, str) or not base_template.strip():
        raise ValueError("prompts.base_template must be a non-empty string")
    base_template = base_template.strip()

    composer = raw.get("composer", "")
    if not isinstance(composer, str):
        raise ValueError(f"prompts.composer must be a string, got {type(composer).__name__}")
    if composer.strip():
        composer = _validate_dotted_path(composer, "prompts.composer")
    else:
        composer = ""

    return PromptsConfig(base_template=base_template, composer=composer)


def _validate_channel_groups(
    channels: List[ChannelConfig],
    digest_groups: list[DigestGroupConfig],
    output_language: str,
) -> None:
    """Cross-validate that channels[*].group references a known group name.

    Valid values: any digest_groups[*].name, the literal "Other", or the
    localized group_other string for the current output_language.

    Raises:
        ValueError: listing all channels with invalid group references.
    """
    from src.ui_strings import get_ui_strings

    ui = get_ui_strings(output_language)
    localized_other = ui.get("group_other", "Other")
    valid_names = {g.name for g in digest_groups} | {"Other", localized_other}

    bad: list[str] = []
    for ch in channels:
        if ch.group is not None and ch.group not in valid_names:
            bad.append(f"channel {ch.name!r}: group {ch.group!r}")

    if bad:
        raise ValueError(
            "Unknown group references in channels config:\n"
            + "\n".join(f"  {b}" for b in bad)
            + f"\nValid groups: {', '.join(sorted(valid_names))}"
        )


def _parse_channels(yaml_config: dict) -> List[ChannelConfig]:
    """Parse and validate channel configs from YAML.

    Raises:
        ValueError: If channels list is empty, entries are invalid, or names are duplicated
    """
    if not isinstance(yaml_config, dict):
        raise ValueError(
            f"config.yaml must contain a top-level mapping, got {type(yaml_config).__name__}"
        )
    channels_value = yaml_config.get("channels", [])
    if not isinstance(channels_value, list):
        raise ValueError(
            f"config.yaml field 'channels' must be a list, got {type(channels_value).__name__}"
        )
    channels = [_parse_channel_entry(i, ch) for i, ch in enumerate(channels_value)]

    if not channels:
        raise ValueError("No channels configured in config.yaml")

    seen: set[str] = set()
    duplicates: set[str] = set()
    for c in channels:
        if c.name in seen:
            duplicates.add(c.name)
        seen.add(c.name)
    if duplicates:
        raise ValueError(f"Duplicate channel names in config.yaml: {', '.join(sorted(duplicates))}")

    return channels


def _load_and_validate_env_vars(ai_provider: str) -> dict:
    """Load and validate required environment variables.

    Returns:
        Dict with keys matching Config env var fields.
    """
    telegram_api_id = os.getenv("TELEGRAM_API_ID")
    telegram_api_hash = os.getenv("TELEGRAM_API_HASH")
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    openai_base_url = os.getenv("OPENAI_BASE_URL", "")
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    # Collect all Gemini/Google API keys dynamically (GEMINI_API_KEY, GEMINI_API_KEY_2..N)
    google_api_keys: list[str] = []
    primary_google_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
    if primary_google_key:
        google_api_keys.append(primary_google_key)

    # Check numbered keys 2..20
    for i in range(2, 21):
        k = os.getenv(f"GEMINI_API_KEY_{i}") or os.getenv(f"GOOGLE_API_KEY_{i}", "")
        if k and k not in google_api_keys:
            google_api_keys.append(k)

    google_api_key = primary_google_key
    google_api_key_2 = os.getenv("GEMINI_API_KEY_2") or os.getenv("GOOGLE_API_KEY_2", "")
    google_api_key_3 = os.getenv("GEMINI_API_KEY_3") or os.getenv("GOOGLE_API_KEY_3", "")

    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_base_url = os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
    openrouter_model = os.getenv("OPENROUTER_MODEL") or "openrouter/free"
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
        "openrouter_image_model": openrouter_image_model,
        "log_level": log_level,
    }


def load_config(config_path: str | None = None, *, path: str | None = None) -> Config:
    """
    Load configuration from YAML file and environment variables.

    Args:
        config_path: Path to config.yaml file
        path: Keyword-only alias for config_path (mirrors load_database_config).
            If both are given they must be equal, otherwise ValueError is raised.

    Returns:
        Config object with all settings

    Raises:
        FileNotFoundError: If config.yaml not found
        ValueError: If required environment variables missing
    """
    if path is not None:
        if config_path is not None and config_path != path:
            raise ValueError("Pass the config file via config_path or path, not both")
        config_path = path
    elif config_path is None:
        config_path = "config.yaml"

    # Load environment variables from .env file
    load_dotenv()

    # Load YAML configuration
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        yaml_config = yaml.safe_load(f)

    # Parse channels
    channels = _parse_channels(yaml_config)

    # Parse storage config
    storage_config = _parse_storage_config(yaml_config)

    # Parse domain database config
    database_config = _parse_database_config(yaml_config)

    # Parse generic collection scheduling config
    collection_config = _parse_collection_config(yaml_config)

    # Parse prompts config
    prompts_config = _parse_prompts_config(yaml_config)

    # Parse MCP server config
    mcp_config = _parse_mcp_config(yaml_config)

    # Parse settings
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
        api_timeout=int(settings_dict.get("api_timeout", 30)),
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
        article=_parse_article_config(settings_dict),
    )

    if settings.target_user_id == 0:
        raise ValueError(
            "target_user_id not configured in config.yaml. "
            "Get your Telegram user ID from @userinfobot"
        )

    # The persistent read path has no live-Telegram fallback: without the
    # domain database there would be nothing to read digest inputs from.
    if settings.persistent_ingestion and not database_config.enabled:
        raise ValueError(
            "settings.persistent_ingestion requires database.enabled=true in config.yaml: "
            "digest inputs are read from the PostgreSQL source history when the flag is set"
        )

    # Cross-validate channel group references against known digest_groups
    _validate_channel_groups(channels, digest_groups, output_language)

    env_vars = _load_and_validate_env_vars(ai_provider)

    return Config(
        channels=channels,
        settings=settings,
        storage=storage_config,
        prompts=prompts_config,
        mcp=mcp_config,
        database=database_config,
        collection=collection_config,
        **env_vars,
    )


if __name__ == "__main__":
    # Test configuration loading
    try:
        config = load_config()
        print("✅ Configuration loaded successfully!")
        print(f"Channels: {len(config.channels)}")
        print(f"Target user: {config.settings.target_user_id}")
        print(f"AI provider: {config.settings.ai_provider}, model: {config.settings.ai_model}")
    except Exception as e:
        print(f"❌ Configuration error: {e}")
