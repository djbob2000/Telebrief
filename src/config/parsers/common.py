"""Common and infrastructure configuration parsers."""

from __future__ import annotations

import os

import yaml
from dotenv import load_dotenv

from src.config.schemas.common import (
    EMBEDDING_PROVIDERS,
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


def _validate_dotted_path(value: str, label: str) -> str:
    """Validate a YAML-string dotted path (e.g. 'pkg.module.ClassName')."""
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
    """Parse and validate a list of filter specs from YAML."""
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


def _parse_storage_config(yaml_config: dict) -> StorageConfig:
    """Parse and validate the optional top-level storage: block."""
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
    """Parse and validate the optional top-level collection: block."""
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


def _parse_embedding_config(
    yaml_config: dict,
    *,
    env_vars: dict | None = None,
    api_key: str = "",
) -> EmbeddingConfig:
    """Parse and validate the optional top-level embedding: block."""
    raw = yaml_config.get("embedding")
    if raw is None:
        default_key = api_key or (env_vars.get("google_api_key", "") if env_vars else "")
        return EmbeddingConfig(api_key=default_key)
    if not isinstance(raw, dict):
        raise ValueError(f"'embedding' must be a mapping, got {type(raw).__name__}")

    provider = raw.get("provider", "google")
    if provider not in EMBEDDING_PROVIDERS:
        raise ValueError(
            f"embedding.provider must be one of {', '.join(sorted(EMBEDDING_PROVIDERS))}, "
            f"got {provider!r}"
        )

    model = raw.get("model", "gemini-embedding-2" if provider == "google" else "")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("embedding.model must be a non-empty string")

    dimensions = raw.get("dimensions", 1536)
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise ValueError(f"embedding.dimensions must be an integer, got {dimensions!r}")
    if not MIN_EMBEDDING_DIMENSIONS <= dimensions <= MAX_EMBEDDING_DIMENSIONS:
        raise ValueError(
            "embedding.dimensions must be an integer between "
            f"{MIN_EMBEDDING_DIMENSIONS} and {MAX_EMBEDDING_DIMENSIONS}, got {dimensions}"
        )

    timeout = raw.get("timeout", 45)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError(f"embedding.timeout must be a positive integer, got {timeout!r}")

    resolved_api_key = api_key
    if not resolved_api_key and env_vars:
        if provider == "google":
            resolved_api_key = env_vars.get("google_api_key", "")
        elif provider == "openrouter":
            resolved_api_key = env_vars.get("openrouter_api_key", "")
        elif provider == "openai":
            resolved_api_key = env_vars.get("openai_api_key", "")

    return EmbeddingConfig(
        provider=provider,
        model=model.strip(),
        dimensions=int(dimensions),
        timeout=int(timeout),
        api_key=resolved_api_key,
    )


def _parse_database_config(yaml_config: dict, *, require_enabled: bool = False) -> DatabaseConfig:
    """Parse and validate the optional top-level database: block."""
    raw = yaml_config.get("database")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"'database' must be a mapping, got {type(raw).__name__}")

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"database.enabled must be a bool, got {type(enabled).__name__}")

    min_pool_size = raw.get("min_pool_size", 1)
    max_pool_size = raw.get("max_pool_size", 3)
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
    """Load only the PostgreSQL database configuration."""
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
    """Parse and validate the optional top-level mcp: block."""
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
    """Parse and validate the optional top-level prompts: block."""
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
