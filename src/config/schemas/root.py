"""Root application Settings and Config schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from src.config.schemas.common import (
    CollectionConfig,
    DatabaseConfig,
    EmbeddingConfig,
    FilterSpec,
    McpConfig,
    PromptsConfig,
    StorageConfig,
)
from src.config.schemas.facebook import FacebookConfig
from src.config.schemas.publication import (
    ArticleConfig,
    ArticleScheduleConfig,
    DigestGroupConfig,
    DigestRubricsConfig,
    EditionScopeConfig,
    EventPipelineConfig,
    PublicationEditorialConfig,
)
from src.config.schemas.telegram import ChannelConfig, TelegramConfig

SUPPORTED_LANGUAGES = ("English", "Russian", "Spanish", "German", "French")
VISION_MODES = ("off", "relevance_only", "full")


@dataclass
class Settings:
    """Application settings."""

    schedule_time: str
    timezone: str
    lookback_hours: int
    openai_model: str
    openai_temperature: float | None = None
    temperature: float | None = None
    max_tokens_per_summary: int = 96000
    use_emojis: bool = True
    include_statistics: bool = True
    target_user_id: int = 0
    target_chat_id: str | int | None = None
    auto_cleanup_old_digests: bool = True
    max_messages_per_channel: int = 5000
    max_prompt_chars: int = 8000
    api_timeout: int = 300

    ai_provider: str = "openai"
    ai_model: str = ""
    ollama_base_url: str = "http://localhost:11434"
    output_language: str = "Russian"
    digest_mode: str = "channel"
    digest_groups: List[DigestGroupConfig] = field(default_factory=list)
    filters: list[FilterSpec] = field(default_factory=list)
    dedup_topics: bool = False
    persistent_ingestion: bool = False
    reasoning_effort: str | None = None
    vision_mode: str = "relevance_only"
    pre_publish_lead_minutes: int = 15
    publication_snapshot_lag_minutes: int = 30
    article: ArticleConfig = field(default_factory=ArticleConfig)
    event_pipeline: EventPipelineConfig = field(default_factory=EventPipelineConfig)
    digest_rubrics: DigestRubricsConfig = field(default_factory=DigestRubricsConfig)
    edition_scopes: dict[str, EditionScopeConfig] = field(default_factory=dict)
    publication_editorial: PublicationEditorialConfig = field(
        default_factory=PublicationEditorialConfig
    )
    weekly_article: ArticleScheduleConfig = field(
        default_factory=lambda: ArticleScheduleConfig(
            enabled=True,
            schedule_day="sunday",
            schedule_time="19:00",
            target_word_count=2000,
            lookback_hours=168,
        )
    )
    monthly_article: ArticleScheduleConfig = field(
        default_factory=lambda: ArticleScheduleConfig(
            enabled=True,
            schedule_day=1,
            schedule_time="20:00",
            target_word_count=3500,
            lookback_hours=720,
        )
    )


@dataclass
class Config:
    """Complete application configuration."""

    channels: List[ChannelConfig]
    settings: Settings
    telegram_api_id: int
    telegram_api_hash: str
    telegram_bot_token: str
    openai_api_key: str
    log_level: str
    google_api_key: str = field(default="", repr=False)
    google_api_key_2: str = field(default="", repr=False)
    google_api_key_3: str = field(default="", repr=False)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openrouter/free"
    openrouter_model_2: str = ""
    openrouter_models: list[str] = field(default_factory=list)
    openrouter_image_model: str = "google/gemini-3.1-flash-lite-image"
    openai_base_url: str = ""
    anthropic_api_key: str = ""
    google_api_keys: list[str] = field(default_factory=list, repr=False)
    storage: StorageConfig = field(default_factory=StorageConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    facebook: FacebookConfig = field(default_factory=FacebookConfig)

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
        return self.google_api_keys[1:] if len(self.google_api_keys) > 1 else []
