"""Common and infrastructure configuration schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
    """Generic ingestion collection scheduling.

    ``telegram_interval_minutes`` is the polling cadence bootstrap applies to
    every managed Telegram source: at bootstrap time it is mirrored into each
    source's ``collector_options.schedule.interval_minutes``. Valid range is
    5..360 minutes; enforced when loading YAML, not by the dataclass itself.
    """

    telegram_interval_minutes: int = 45


@dataclass
class EmbeddingConfig:
    """Semantic embedding settings (Plan 3 Task 5).

    ``model`` and ``dimensions`` name one immutable vector space: both values
    are copied into every embedding job so retries keep writing into exactly
    the space they were queued for. Changing them schedules a new backfill;
    old embedding rows are never mutated or reinterpreted.

    The API key reuses the shared Gemini key resolution (GEMINI_API_KEY /
    GOOGLE_API_KEY env vars — no separate embedding credentials exist).
    ``repr=False`` makes the credential structurally unloggable.
    """

    provider: str = "google"
    model: str = "gemini-embedding-2"
    dimensions: int = 1536
    timeout: int = 45
    api_key: str = field(default="", repr=False)


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
    max_pool_size: int = 3
    domain_schema: str = "public"
    procrastinate_schema: str = "procrastinate"


@dataclass
class McpConfig:
    """Configuration for the built-in MCP server."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/mcp"
