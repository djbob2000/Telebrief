# Modular Architecture Restructuring (Phase 1: Config Subsystem & Clean Packaging) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose the 2,010-line monolithic `src/config_loader.py` into a clean, modular `src/config/` package with dedicated domain schemas and parsers while providing 100% backward-compatible re-exports via `src/config_loader.py` to keep all 2,030 existing tests green.

**Architecture:** Split configuration into two logical layers under `src/config/`: `schemas/` (strictly-typed immutable/dataclass models partitioned by domain: common, telegram, facebook, publication, root) and `parsers/` (domain-focused parsing, normalization, and bounds-checking functions). A top-level `loader.py` handles environment resolution and orchestrates loading YAML into `Config`. `src/config_loader.py` becomes a thin backwards-compatibility shim that re-exports all symbols.

**Tech Stack:** Python 3.12/3.14, `dataclasses`, `pyyaml`, `python-dotenv`, `pytest`, `pytest-xdist`.

---

## File Structure Map

```text
src/config/
├── __init__.py                  # Public facade: Config, load_config, Settings, schemas
├── schemas/
│   ├── __init__.py              # Re-exports all configuration dataclasses & constants
│   ├── common.py                # FilterSpec, Storage, Database, Mcp, Prompts, Embedding, Collection
│   ├── telegram.py              # ForumTopicConfig, ChannelConfig, TelegramConfig, SourceRoleResolver
│   ├── facebook.py              # FacebookCommentsConfig, FacebookAuthProfileBootstrap, FacebookConfig
│   ├── publication.py           # Rubrics, EditionScope, Article, Pipeline, PublicationEditorial
│   └── root.py                  # Settings, Config
├── parsers/
│   ├── __init__.py              # Re-exports domain parsing functions
│   ├── common.py                # Storage, database, mcp, prompts, embeddings, filter parsers
│   ├── telegram.py              # Channel, topic, telegram options parsers & validators
│   ├── facebook.py              # Facebook configuration parser
│   └── publication.py           # Rubrics, article, digest, pipeline, editorial parsers
└── loader.py                    # Environment variable loading, YAML file loading, main load_config()

src/config_loader.py             # Backwards-compatibility facade re-exporting from src.config
tests/config/test_modular_config.py  # New unit test suite verifying src.config import parity
```

---

### Task 1: Create `src/config/schemas/` Domain Models

**Files:**
- Create: `src/config/schemas/__init__.py`
- Create: `src/config/schemas/common.py`
- Create: `src/config/schemas/telegram.py`
- Create: `src/config/schemas/facebook.py`
- Create: `src/config/schemas/publication.py`
- Create: `src/config/schemas/root.py`
- Test: `tests/config/test_config_schemas.py`

- [ ] **Step 1: Write the failing test for domain configuration schemas**

Create `tests/config/test_config_schemas.py`:

```python
"""Unit tests verifying src.config.schemas exports and invariants."""

from src.config.schemas.common import (
    CollectionConfig,
    DatabaseConfig,
    EmbeddingConfig,
    FilterSpec,
    McpConfig,
    PromptsConfig,
    StorageConfig,
)
from src.config.schemas.facebook import (
    FacebookAuthProfileBootstrap,
    FacebookCommentsConfig,
    FacebookConfig,
    FacebookSourceBootstrap,
)
from src.config.schemas.publication import (
    DEFAULT_DIGEST_RUBRIC,
    ArticleConfig,
    DigestGroupConfig,
    DigestRubricConfig,
    DigestRubricsConfig,
    EditionScopeConfig,
    EventPipelineConfig,
    PublicationEditorialConfig,
)
from src.config.schemas.root import Config, Settings
from src.config.schemas.telegram import (
    ChannelConfig,
    ForumTopicConfig,
    SourceRoleResolver,
    TelegramConfig,
    effective_source_type,
)


def test_schema_instantiation_defaults():
    collection = CollectionConfig()
    assert collection.telegram_interval_minutes == 45

    db = DatabaseConfig()
    assert db.enabled is False
    assert db.min_pool_size == 1

    emb = EmbeddingConfig()
    assert emb.provider == "google"
    assert emb.dimensions == 1536

    article = ArticleConfig()
    assert article.enabled is True
    assert article.editorial_max_output_tokens == 65_536

    pipeline = EventPipelineConfig()
    assert pipeline.mode == "legacy_claims"
    assert pipeline.fragment_max_chars == 1200


def test_publication_editorial_validation():
    pub = PublicationEditorialConfig()
    assert pub.conflict_window_minutes == 90
    assert pub.digest_narrative_mode == "deterministic"


def test_source_role_resolver():
    ch = ChannelConfig(id="@test", name="Test Channel", source_type="news")
    resolver = SourceRoleResolver([ch])
    assert resolver.resolve("Test Channel") == "news"
    assert effective_source_type(ch) == "news"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/config/test_config_schemas.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.config'`

- [ ] **Step 3: Implement `src/config/schemas/` models**

Create `src/config/schemas/common.py`:
```python
"""Common and infrastructure configuration schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FORBIDDEN_AI_MODELS: frozenset[str] = frozenset({"deepseek/deepseek-chat"})
EMBEDDING_PROVIDERS: frozenset[str] = frozenset({"google", "openrouter", "openai"})
MIN_EMBEDDING_DIMENSIONS: int = 128
MAX_EMBEDDING_DIMENSIONS: int = 8192


@dataclass
class FilterSpec:
    """Specification for a single message filter in a filter chain."""

    class_path: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectionConfig:
    """Generic ingestion collection scheduling."""

    telegram_interval_minutes: int = 45


@dataclass
class EmbeddingConfig:
    """Semantic embedding settings."""

    provider: str = "google"
    model: str = "gemini-embedding-2"
    dimensions: int = 1536
    timeout: int = 45
    api_key: str = field(default="", repr=False)


@dataclass
class PromptsConfig:
    """Configuration for prompt template and composer."""

    base_template: str = "src/prompts/base_summary.txt"
    composer: str = ""


@dataclass
class StorageConfig:
    """Configuration for persistent message storage backend."""

    enabled: bool = False
    backend: str = "sqlite"
    path: str = "data/messages.db"
    url: str = field(default="", repr=False)


@dataclass
class DatabaseConfig:
    """Configuration for PostgreSQL domain store and Procrastinate queue."""

    enabled: bool = False
    url: str = field(default="", repr=False)
    min_pool_size: int = 1
    max_pool_size: int = 3
    domain_schema: str = "public"
    procrastinate_schema: str = "procrastinate"


@dataclass
class McpConfig:
    """Configuration for built-in MCP server."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/mcp"
```

Create `src/config/schemas/telegram.py`:
```python
"""Telegram and channel configuration schemas."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.config.schemas.common import FilterSpec

SOURCE_TYPES = ("news", "community", "official", "classifieds", "mixed")


@dataclass
class ForumTopicConfig:
    """A selected forum topic within a Telegram group."""

    id: int
    name: str
    source_type: str | None = None


@dataclass
class ChannelConfig:
    """Configuration for a single Telegram channel/chat."""

    id: str | int
    name: str
    lookback_hours: int | None = None
    prompt_extra: str = ""
    filters: list[FilterSpec] | None = None
    group: str | None = None
    topics: list[ForumTopicConfig] = field(default_factory=list)
    source_type: str = "mixed"


@dataclass
class TelegramConfig:
    """Telegram operational settings."""

    processing_mode: str = "knowledge_full"


def effective_source_type(channel: ChannelConfig, topic: ForumTopicConfig | None = None) -> str:
    """Return configured editorial role using topic > channel > mixed precedence."""
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
```

Create `src/config/schemas/facebook.py`:
```python
"""Facebook provider configuration schemas."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FacebookCommentsConfig:
    include_replies: bool = True
    max_comments_per_post: int = 500
    max_replies_per_comment: int = 100
    max_pages_per_refresh: int = 20
    max_duration_per_post_seconds: int = 120


@dataclass
class FacebookAuthProfileBootstrap:
    name: str
    storage_ref: str


@dataclass
class FacebookSourceBootstrap:
    name: str
    kind: str
    url: str
    role: str = "community"
    auth_profile: str = "default"
    enabled: bool = True
    scan_times: list[str] = field(default_factory=lambda: ["08:00", "12:00", "16:00", "19:30"])
    timezone: str = "UTC"


@dataclass
class FacebookConfig:
    enabled: bool = False
    editorial_enabled: bool = True
    auth_root: str = "/var/lib/telebrief/auth"
    auth_profiles: list[FacebookAuthProfileBootstrap] = field(default_factory=list)
    sources: list[FacebookSourceBootstrap] = field(default_factory=list)
    comments: FacebookCommentsConfig = field(default_factory=FacebookCommentsConfig)
```

Create `src/config/schemas/publication.py`:
```python
"""Editorial and publication configuration schemas."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

_DIGEST_RUBRIC_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
EVENT_PIPELINE_MODES = ("legacy_claims", "event_first_shadow", "event_first")


@dataclass
class DigestGroupConfig:
    """Configuration for a single digest topic group."""

    name: str
    description: str
    prompt_extra: str = ""


@dataclass(frozen=True)
class DigestRubricConfig:
    """Configuration for a single digest presentation rubric."""

    id: str
    name: str
    description: str
    emoji: str = ""
    fallback: bool = False


DEFAULT_DIGEST_RUBRIC = DigestRubricConfig(
    id="other",
    name="Другое",
    description="Важные локальные события",
    emoji="📌",
    fallback=True,
)


@dataclass(frozen=True)
class DigestRubricsConfig:
    """Edition-level configuration for digest display rubrics."""

    min_similarity: float = 0.38
    items: tuple[DigestRubricConfig, ...] = (DEFAULT_DIGEST_RUBRIC,)

    @property
    def fallback(self) -> DigestRubricConfig:
        matches = [item for item in self.items if item.fallback]
        if len(matches) != 1:
            raise ValueError("digest_rubrics must contain exactly one fallback rubric")
        return matches[0]


@dataclass(frozen=True)
class EditionScopeConfig:
    name: str
    focus_places: tuple[str, ...]
    direct_impact_only: bool = True
    notes: tuple[str, ...] = ()


@dataclass
class ArticleConfig:
    """Configuration for daily editorial article generation."""

    enabled: bool = True
    schedule_time: str = "20:00"
    lookback_hours: int = 24
    author_name: str = "Бердянск Новости"
    fallback_save_dir: str = "data/articles"
    prompt_template: str = "src/prompts/news_style.md"
    generation_retries: int = 2
    generation_retry_delay: float = 1.0
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


@dataclass(frozen=True)
class EventPipelineConfig:
    """Cost-bounded event-first processing pipeline configuration."""

    mode: Literal["legacy_claims", "event_first_shadow", "event_first"] = "legacy_claims"
    fragment_max_chars: int = 1200
    active_window_hours: int = 72
    join_similarity: float = 0.84
    max_cluster_candidates: int = 20
    embedding_batch_size: int = 128
    direct_analysis_min_fragments: int = 3
    direct_analysis_min_unique_sources: int = 2
    triage_batch_size: int = 30
    triage_excerpt_chars: int = 320
    triage_min_ignore_confidence: float = 0.95
    analysis_quiet_seconds: int = 120
    analysis_min_interval_seconds: int = 600
    analysis_min_new_fragments: int = 3
    analysis_max_calls_per_story_per_hour: int = 4
    provider_retry_backoff_seconds: int = 300
    analysis_max_input_chars: int = 24000
    representative_fragment_limit: int = 16
    rich_analysis_max_calls_per_cycle: int = 40
    live_batch_size: int = 100
    backfill_batch_size: int = 500


@dataclass(frozen=True)
class PublicationEditorialConfig:
    """Editorial parameters for publication digests and articles."""

    conflict_window_minutes: int = 90
    article_min_words: int = 800
    article_max_words: int = 2200
    article_min_sections: int = 3
    article_max_sections: int = 8
    article_max_direct_quotes: int = 4

    article_claim_min_content_coverage: float = 0.50
    digest_narrative_mode: str = "deterministic"
    digest_narrative_max_cards_per_block: int = 6

    digest_narrative_max_output_tokens: int = 4096
    digest_city_situation_max_items: int = 7
    digest_city_situation_max_details_per_item: int = 2
    digest_city_situation_max_positive_items: int = 2
    article_allow_deterministic_fallback: bool = False
    article_editor_enabled: bool = False
    article_editor_max_attempts: int = 2

    def __post_init__(self) -> None:
        if self.conflict_window_minutes <= 0:
            raise ValueError("conflict_window_minutes must be positive")
        if self.article_min_words <= 0 or self.article_max_words <= 0:
            raise ValueError("article word limits must be positive")
        if self.article_min_words > self.article_max_words:
            raise ValueError("article_min_words cannot be greater than article_max_words")
        if self.article_min_sections <= 0 or self.article_max_sections <= 0:
            raise ValueError("article section limits must be positive")
        if self.article_min_sections > self.article_max_sections:
            raise ValueError("article_min_sections cannot be greater than article_max_sections")
        if self.article_max_direct_quotes < 0:
            raise ValueError("article_max_direct_quotes cannot be negative")
        if not (0.5 <= self.article_claim_min_content_coverage <= 1.0):
            raise ValueError("article_claim_min_content_coverage must be between 0.5 and 1.0")
        if self.digest_narrative_mode not in ("deterministic", "single_call", "journalistic"):
            raise ValueError(
                f"digest_narrative_mode must be 'deterministic', 'single_call', or 'journalistic', got {self.digest_narrative_mode!r}"
            )
        if self.digest_narrative_max_cards_per_block <= 0:
            raise ValueError("digest_narrative_max_cards_per_block must be a positive integer")
        if self.digest_narrative_max_output_tokens <= 0:
            raise ValueError("digest_narrative_max_output_tokens must be a positive integer")
        if not (1 <= self.digest_city_situation_max_items <= 12):
            raise ValueError("digest_city_situation_max_items must be between 1 and 12")
        if not (1 <= self.digest_city_situation_max_details_per_item <= 4):
            raise ValueError("digest_city_situation_max_details_per_item must be between 1 and 4")
        if not (0 <= self.digest_city_situation_max_positive_items <= 4):
            raise ValueError("digest_city_situation_max_positive_items must be between 0 and 4")
```

Create `src/config/schemas/root.py`:
```python
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
    article: ArticleConfig = field(default_factory=ArticleConfig)
    event_pipeline: EventPipelineConfig = field(default_factory=EventPipelineConfig)
    digest_rubrics: DigestRubricsConfig = field(default_factory=DigestRubricsConfig)
    edition_scopes: dict[str, EditionScopeConfig] = field(default_factory=dict)
    publication_editorial: PublicationEditorialConfig = field(
        default_factory=PublicationEditorialConfig
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
```

Create `src/config/schemas/__init__.py`:
```python
"""Export all configuration schema dataclasses and constants."""

from src.config.schemas.common import (
    EMBEDDING_PROVIDERS,
    FORBIDDEN_AI_MODELS,
    MAX_EMBEDDING_DIMENSIONS,
    MIN_EMBEDDING_DIMENSIONS,
    CollectionConfig,
    DatabaseConfig,
    EmbeddingConfig,
    FilterSpec,
    McpConfig,
    PromptsConfig,
    StorageConfig,
)
from src.config.schemas.facebook import (
    FacebookAuthProfileBootstrap,
    FacebookCommentsConfig,
    FacebookConfig,
    FacebookSourceBootstrap,
)
from src.config.schemas.publication import (
    DEFAULT_DIGEST_RUBRIC,
    EVENT_PIPELINE_MODES,
    ArticleConfig,
    DigestGroupConfig,
    DigestRubricConfig,
    DigestRubricsConfig,
    EditionScopeConfig,
    EventPipelineConfig,
    PublicationEditorialConfig,
)
from src.config.schemas.root import (
    SOURCE_TYPES,
    SUPPORTED_LANGUAGES,
    VISION_MODES,
    Config,
    Settings,
)
from src.config.schemas.telegram import (
    ChannelConfig,
    ForumTopicConfig,
    SourceRoleResolver,
    TelegramConfig,
    effective_source_type,
)

__all__ = [
    "DEFAULT_DIGEST_RUBRIC",
    "EMBEDDING_PROVIDERS",
    "EVENT_PIPELINE_MODES",
    "FORBIDDEN_AI_MODELS",
    "MAX_EMBEDDING_DIMENSIONS",
    "MIN_EMBEDDING_DIMENSIONS",
    "SOURCE_TYPES",
    "SUPPORTED_LANGUAGES",
    "VISION_MODES",
    "ArticleConfig",
    "ChannelConfig",
    "CollectionConfig",
    "Config",
    "DatabaseConfig",
    "DigestGroupConfig",
    "DigestRubricConfig",
    "DigestRubricsConfig",
    "EditionScopeConfig",
    "EmbeddingConfig",
    "EventPipelineConfig",
    "FacebookAuthProfileBootstrap",
    "FacebookCommentsConfig",
    "FacebookConfig",
    "FacebookSourceBootstrap",
    "FilterSpec",
    "ForumTopicConfig",
    "McpConfig",
    "PromptsConfig",
    "PublicationEditorialConfig",
    "Settings",
    "SourceRoleResolver",
    "StorageConfig",
    "TelegramConfig",
    "effective_source_type",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/config/test_config_schemas.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config/schemas tests/config/test_config_schemas.py
git commit -m "refactor(config): introduce modular src/config/schemas domain models"
```

---

### Task 2: Create `src/config/parsers/` Domain Parsers

**Files:**
- Create: `src/config/parsers/__init__.py`
- Create: `src/config/parsers/common.py`
- Create: `src/config/parsers/telegram.py`
- Create: `src/config/parsers/facebook.py`
- Create: `src/config/parsers/publication.py`
- Test: `tests/config/test_config_parsers.py`

- [ ] **Step 1: Write the failing test for domain parsers**

Create `tests/config/test_config_parsers.py`:

```python
"""Unit tests verifying modular config parsing routines."""

import pytest

from src.config.parsers.common import (
    _parse_collection_config,
    _parse_database_config,
    _parse_filter_specs,
)
from src.config.parsers.publication import (
    _parse_digest_rubrics,
    _parse_edition_scopes,
)
from src.config.parsers.telegram import (
    _normalize_source_type,
    _parse_channels,
)


def test_parse_collection_config():
    cfg = _parse_collection_config({"collection": {"telegram_interval_minutes": 30}})
    assert cfg.telegram_interval_minutes == 30


def test_parse_collection_config_bounds():
    with pytest.raises(ValueError, match="collection.telegram_interval_minutes must be between"):
        _parse_collection_config({"collection": {"telegram_interval_minutes": 400}})


def test_normalize_source_type():
    assert _normalize_source_type("news", "label") == "news"
    with pytest.raises(ValueError, match="label must be one of"):
        _normalize_source_type("invalid_type", "label")


def test_parse_filter_specs():
    specs = _parse_filter_specs(
        [{"class_path": "src.filters.TestFilter", "config": {"threshold": 10}}],
        "settings.filters",
    )
    assert len(specs) == 1
    assert specs[0].class_path == "src.filters.TestFilter"


def test_parse_digest_rubrics():
    rubrics = _parse_digest_rubrics({
        "digest_rubrics": {
            "min_similarity": 0.45,
            "items": [{"id": "city", "name": "Город", "fallback": True}],
        }
    })
    assert rubrics.min_similarity == 0.45
    assert len(rubrics.items) == 1
    assert rubrics.fallback.id == "city"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/config/test_config_parsers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.config.parsers'`

- [ ] **Step 3: Extract and implement `src/config/parsers/`**

Create `src/config/parsers/common.py`:
Move parsing logic from `config_loader.py` for storage, collection, database, mcp, prompts, embedding, filter specs, and dotted paths (`_validate_dotted_path`, `_parse_filter_specs`, `_parse_storage_config`, `_parse_collection_config`, `_parse_embedding_config`, `_parse_database_config`, `load_database_config`, `_parse_mcp_config`, `_parse_prompts_config`).

Create `src/config/parsers/telegram.py`:
Move parsing logic from `config_loader.py` for channels, topic validation, source type normalization, and channel groups (`_normalize_source_type`, `_validate_channel_lookback`, `_validate_channel_group`, `_validate_channel_source_type`, `_validate_channel_topics`, `_validate_channel_id_name`, `_parse_channel_entry`, `_validate_channel_groups`, `_parse_channels`, `_parse_telegram_config`).

Create `src/config/parsers/facebook.py`:
Move parsing logic from `config_loader.py` for facebook configs (`_parse_facebook_config`).

Create `src/config/parsers/publication.py`:
Move parsing logic from `config_loader.py` for AI settings resolution, rubrics, edition scopes, article options, event pipeline, and publication editorial parameters (`_resolve_ai_settings`, `_parse_digest_settings`, `_parse_digest_rubrics`, `_parse_edition_scopes`, `_parse_article_config`, `_parse_event_pipeline_config`, `_parse_publication_editorial_config`).

Create `src/config/parsers/__init__.py`:
Re-export all parsing functions so they can be consumed by `loader.py` and backwards-compatible callers.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/config/test_config_parsers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config/parsers tests/config/test_config_parsers.py
git commit -m "refactor(config): introduce modular src/config/parsers domain parsing routines"
```

---

### Task 3: Create `src/config/loader.py` and `src/config/__init__.py`

**Files:**
- Create: `src/config/loader.py`
- Create: `src/config/__init__.py`
- Test: `tests/config/test_config_package.py`

- [ ] **Step 1: Write the failing test for `src/config` package interface**

Create `tests/config/test_config_package.py`:

```python
"""Tests for the primary src.config package entry points."""

from pathlib import Path
from src.config import Config, load_config, load_database_config


def test_load_config_via_package(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot_token")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
channels:
  - id: "@test"
    name: "Test"
settings:
  schedule_time: "20:00"
  timezone: "UTC"
  lookback_hours: 24
  openai_model: "gpt-5-nano"
  openai_temperature: 0.7
""")

    cfg = load_config(str(config_file))
    assert isinstance(cfg, Config)
    assert cfg.settings.schedule_time == "20:00"
    assert len(cfg.channels) == 1
    assert cfg.channels[0].name == "Test"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/config/test_config_package.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_config' from 'src.config'`

- [ ] **Step 3: Implement `src/config/loader.py` and `src/config/__init__.py`**

Create `src/config/loader.py`:
Move `_load_and_validate_env_vars` and `load_config` into `src/config/loader.py`, tying together YAML parsing with the modular parsers from `src/config/parsers/`.

Create `src/config/__init__.py`:
Export all schemas from `src.config.schemas`, all parsers from `src.config.parsers`, and `load_config`, `load_database_config` from `src.config.loader`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/config/test_config_package.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/config/loader.py src/config/__init__.py tests/config/test_config_package.py
git commit -m "feat(config): provide unified src/config package entry points"
```

---

### Task 4: Replace `src/config_loader.py` with Facade & Regression Test

**Files:**
- Modify: `src/config_loader.py`
- Test: `tests/config/test_config_loader.py`
- Test: full test suite across the repository

- [ ] **Step 1: Replace `src/config_loader.py` content with backward-compatibility facade**

In `src/config_loader.py`, replace the 2,010 lines with:

```python
"""Backward-compatibility facade for src.config.

This module preserves all existing imports from `src.config_loader` while
delegating the actual schemas, parsers, and loading orchestration to the
modular `src.config` package.
"""

from __future__ import annotations

from src.config import *
from src.config.parsers.common import (
    _parse_collection_config,
    _parse_database_config,
    _parse_embedding_config,
    _parse_filter_specs,
    _parse_mcp_config,
    _parse_prompts_config,
    _parse_storage_config,
    _validate_dotted_path,
)
from src.config.parsers.facebook import _parse_facebook_config
from src.config.parsers.publication import (
    _parse_article_config,
    _parse_digest_rubrics,
    _parse_digest_settings,
    _parse_edition_scopes,
    _parse_event_pipeline_config,
    _parse_publication_editorial_config,
    _resolve_ai_settings,
)
from src.config.parsers.telegram import (
    _normalize_source_type,
    _parse_channel_entry,
    _parse_channels,
    _parse_telegram_config,
    _validate_channel_group,
    _validate_channel_groups,
    _validate_channel_id_name,
    _validate_channel_lookback,
    _validate_channel_source_type,
    _validate_channel_topics,
)
from src.config.loader import _load_and_validate_env_vars
```

- [ ] **Step 2: Run all 206 config tests**

Run: `.venv/bin/pytest tests/config/ -v`
Expected: All 206 tests PASS.

- [ ] **Step 3: Run the entire test suite**

Run: `.venv/bin/pytest -q`
Expected: All 2,030+ tests PASS with 0 regressions.

- [ ] **Step 4: Commit**

```bash
git add src/config_loader.py
git commit -m "refactor(config): convert config_loader.py into backward-compatible facade for src.config"
```

---

## Verification Plan

### Automated Tests
1. Config tests:
   ```bash
   .venv/bin/pytest tests/config/ -v
   ```
   Verifies that both `src.config` directly and legacy imports via `src.config_loader` behave identically across all 206 existing edge-case tests.
2. Full suite run:
   ```bash
   .venv/bin/pytest -n auto
   ```
   Ensures zero regressions across the entire application (2,030+ tests passing).

### Manual Verification
1. Verify line count reduction on `src/config_loader.py`:
   ```bash
   wc -l src/config_loader.py
   ```
   (Should shrink from 2,010 lines to ~40 lines).
2. Verify directory layout under `src/config/`:
   ```bash
   find src/config -type f
   ```
   (Verify clean modular organization).
