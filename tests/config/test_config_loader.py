"""Tests for config_loader module."""

import logging
from unittest.mock import patch

import pytest
import yaml

from src.config_loader import (
    ArticleConfig,
    DatabaseConfig,
    DigestGroupConfig,
    EmbeddingConfig,
    FilterSpec,
    ForumTopicConfig,
    McpConfig,
    PromptsConfig,
    PublicationEditorialConfig,
    SourceRoleResolver,
    StorageConfig,
    effective_source_type,
    load_config,
    load_database_config,
)


@pytest.mark.unit
def test_article_config_defaults(temp_config_file, mock_env_vars):
    """Article settings default to enabled, 20:00 schedule, and 24 lookback hours."""
    config = load_config(temp_config_file)
    assert config.settings.article == ArticleConfig(
        enabled=True,
        schedule_time="20:00",
        lookback_hours=24,
        author_name="Бердянск Новости",
        fallback_save_dir="data/articles",
        telegraph_access_token=None,
    )


@pytest.mark.unit
def test_article_audit_output_budget_defaults_to_32768(temp_config_file, mock_env_vars):
    """Article audit output budget defaults to 32768 tokens independently of other stages."""
    config = load_config(temp_config_file)
    assert config.settings.article.editorial_audit_max_output_tokens == 32768


@pytest.mark.unit
def test_message_collection_default_limit_is_5000(temp_config_file, mock_env_vars):
    """A daily collection does not stop at the old 500-message cap."""
    config = load_config(temp_config_file)

    assert config.settings.max_messages_per_channel == 5000


@pytest.mark.unit
def test_article_config_custom_values(tmp_path, mock_env_vars):
    """Custom article settings are parsed and validated properly."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@test"
    name: "Test Channel"
settings:
  target_user_id: 123456789
  article:
    enabled: false
    schedule_time: "21:30"
    lookback_hours: 12
    author_name: "Редакция"
    fallback_save_dir: "custom/articles"
    telegraph_access_token: "tok_secret"
"""
    )
    config = load_config(str(config_file))
    assert config.settings.article == ArticleConfig(
        enabled=False,
        schedule_time="21:30",
        lookback_hours=12,
        author_name="Редакция",
        fallback_save_dir="custom/articles",
        telegraph_access_token="tok_secret",
    )


@pytest.mark.unit
def test_article_config_parses_generation_retry_settings(tmp_path, mock_env_vars):
    """Retry count and delay are configurable for resilient article generation."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@test"
    name: "Test Channel"
settings:
  target_user_id: 123456789
  article:
    generation_retries: 4
    generation_retry_delay: 0.25
"""
    )

    config = load_config(str(config_file))

    assert config.settings.article.generation_retries == 4
    assert config.settings.article.generation_retry_delay == 0.25


@pytest.mark.unit
def test_article_config_parses_editorial_output_budget(tmp_path, mock_env_vars):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@test"
    name: "Test Channel"
settings:
  target_user_id: 123456789
  article:
    editorial_max_output_tokens: 65536
    editorial_api_timeout: 300
"""
    )

    config = load_config(str(config_file))

    assert config.settings.article.editorial_max_output_tokens == 65536
    assert config.settings.article.editorial_api_timeout == 300


@pytest.mark.unit
def test_article_config_parses_separate_longform_budgets(tmp_path, mock_env_vars):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@test"
    name: "Test Channel"
settings:
  target_user_id: 123456789
  article:
    editorial_analysis_max_output_tokens: 65536
    editorial_analysis_compact_max_output_tokens: 16384
    editorial_writer_max_output_tokens: 65536
    editorial_audit_max_output_tokens: 16384
    editorial_repair_max_output_tokens: 8192
"""
    )

    config = load_config(str(config_file))

    article = config.settings.article
    assert article.editorial_analysis_max_output_tokens == 65536
    assert article.editorial_analysis_compact_max_output_tokens == 16384
    assert article.editorial_writer_max_output_tokens == 65536
    assert article.editorial_audit_max_output_tokens == 16384
    assert article.editorial_repair_max_output_tokens == 8192


@pytest.mark.unit
def test_load_config_parses_forum_topics(tmp_path, mock_env_vars):
    """A channel may restrict collection to named Telegram forum topics."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@Berdyansk_drb"
    name: "Бердянск Свежие Объявления"
    topics:
      - id: 235525
        name: "Проблемы ЖКХ"
      - id: 43339
        name: "Новости Бердянска"
settings:
  target_user_id: 123456789
"""
    )

    config = load_config(str(config_file))

    assert config.channels[0].topics == [
        ForumTopicConfig(id=235525, name="Проблемы ЖКХ"),
        ForumTopicConfig(id=43339, name="Новости Бердянска"),
    ]


@pytest.mark.unit
def test_source_type_precedence_and_mixed_default(tmp_path, mock_env_vars):
    """Topic roles override channel roles, while omitted roles default to mixed."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@source"
    name: "Source"
    source_type: community
    topics:
      - id: 123
        name: "News"
        source_type: news
  - id: "@unknown"
    name: "Unknown"
settings:
  target_user_id: 123456789
"""
    )

    config = load_config(str(config_file))

    assert config.channels[0].source_type == "community"
    assert config.channels[0].topics[0].source_type == "news"
    assert effective_source_type(config.channels[0], config.channels[0].topics[0]) == "news"
    assert config.channels[1].source_type == "mixed"
    assert effective_source_type(config.channels[1]) == "mixed"

    resolver = SourceRoleResolver(config.channels)
    assert resolver.resolve("Source", topic_id=123) == "news"
    assert resolver.resolve("Source", topic_id=999) == "community"
    assert resolver.resolve("Unknown") == "mixed"


@pytest.mark.unit
def test_source_type_rejects_unknown_value(tmp_path, mock_env_vars):
    """Unknown editorial roles fail configuration validation."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@source"
    name: "Source"
    source_type: officialish
settings:
  target_user_id: 123456789
"""
    )

    with pytest.raises(ValueError, match="source_type"):
        load_config(str(config_file))


@pytest.mark.unit
def test_load_config_defaults_topics_to_empty(temp_config_file, mock_env_vars):
    """Sources without a topics block retain whole-source collection behavior."""
    config = load_config(temp_config_file)

    assert config.channels[0].topics == []


@pytest.mark.unit
def test_load_config_success(temp_config_file, mock_env_vars):
    """Test successful configuration loading."""
    config = load_config(temp_config_file)

    assert config is not None
    assert len(config.channels) == 2
    assert config.channels[0].id == "@test_channel"
    assert config.channels[0].name == "Test Channel"
    assert config.settings.schedule_time == "08:00"
    assert config.settings.target_user_id == 123456789
    assert config.telegram_api_id == 12345678
    assert config.openai_api_key == "sk-test-key"
    assert config.settings.ai_provider == "openai"
    assert config.settings.ai_model == "gpt-5-nano"
    assert config.settings.output_language == "Russian"


@pytest.mark.unit
def test_load_config_reads_optional_openai_base_url(temp_config_file, mock_env_vars, monkeypatch):
    """The OpenAI-compatible endpoint can be overridden for DeepSeek."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")

    config = load_config(temp_config_file)

    assert config.openai_base_url == "https://api.deepseek.com/v1"


@pytest.mark.unit
def test_load_config_google_provider_uses_gemini_key(tmp_path, mock_env_vars, monkeypatch):
    """Google provider resolves its default model and separate API key."""
    monkeypatch.setenv("GEMINI_API_KEY", "google-test-key")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@test_channel"
    name: "Test Channel"
settings:
  target_user_id: 123456789
  ai_provider: "google"
"""
    )

    config = load_config(str(config_file))

    assert config.settings.ai_provider == "google"
    assert config.settings.ai_model == "gemini-3.6-flash"
    assert config.google_api_key == "google-test-key"


@pytest.mark.unit
def test_load_config_reads_google_and_openrouter_fallback_keys(
    tmp_path, mock_env_vars, monkeypatch
):
    """Optional backup Google keys and OpenRouter credentials are loaded separately."""
    monkeypatch.setenv("GEMINI_API_KEY", "google-test-key")
    monkeypatch.setenv("GEMINI_API_KEY_2", "google-backup-2")
    monkeypatch.setenv("GEMINI_API_KEY_3", "google-backup-3")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@test_channel"
    name: "Test Channel"
settings:
  target_user_id: 123456789
  ai_provider: "google"
"""
    )

    config = load_config(str(config_file))

    assert config.google_api_key_2 == "google-backup-2"
    assert config.google_api_key_3 == "google-backup-3"
    assert config.openrouter_api_key == "openrouter-test-key"


@pytest.mark.unit
def test_load_config_reads_gemini_keys_1_to_5(tmp_path, mock_env_vars, monkeypatch):
    """Dynamic loading supports up to N GEMINI_API_KEY_N variables."""
    monkeypatch.setenv("GEMINI_API_KEY", "key-1")
    monkeypatch.setenv("GEMINI_API_KEY_2", "key-2")
    monkeypatch.setenv("GEMINI_API_KEY_3", "key-3")
    monkeypatch.setenv("GEMINI_API_KEY_4", "key-4")
    monkeypatch.setenv("GEMINI_API_KEY_5", "key-5")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@test_channel"
    name: "Test Channel"
settings:
  target_user_id: 123456789
  ai_provider: "google"
"""
    )

    config = load_config(str(config_file))
    assert config.gemini_api_key == "key-1"
    assert config.gemini_api_key_2 == "key-2"
    assert config.gemini_api_key_3 == "key-3"
    assert config.gemini_api_key_4 == "key-4"
    assert config.gemini_api_key_5 == "key-5"
    assert config.google_api_keys == ["key-1", "key-2", "key-3", "key-4", "key-5"]
    assert config.google_api_backup_keys == ["key-2", "key-3", "key-4", "key-5"]


@pytest.mark.unit
def test_load_config_google_provider_requires_gemini_key(tmp_path, mock_env_vars, monkeypatch):
    """Google provider reports a missing Gemini API key clearly."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@test_channel"
    name: "Test Channel"
settings:
  target_user_id: 123456789
  ai_provider: "google"
"""
    )

    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            load_config(str(config_file))


@pytest.mark.unit
def test_load_config_reads_target_chat_id(tmp_path, mock_env_vars):
    """The digest destination can be a public Telegram channel username."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@source"
    name: "Source"
settings:
  target_user_id: 123456789
  target_chat_id: "@berdiansk_news"
"""
    )

    config = load_config(str(config_file))

    assert config.settings.target_chat_id == "@berdiansk_news"


@pytest.mark.unit
def test_load_config_custom_output_language(tmp_path, mock_env_vars):
    """Test config loading with custom output_language."""
    config_content = """
channels:
  - id: "@test_channel"
    name: "Test Channel"

settings:
  target_user_id: 123456789
  output_language: "English"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    config = load_config(str(config_file))

    assert config.settings.output_language == "English"


@pytest.mark.unit
def test_load_config_missing_file():
    """Test error handling for missing config file."""
    with pytest.raises(FileNotFoundError):
        load_config("nonexistent.yaml")


@pytest.mark.unit
def test_load_config_missing_env_vars(tmp_path, monkeypatch):
    """Test error handling for missing environment variables."""
    # Create temp config file without using mock_env_vars
    config_content = """
channels:
  - id: "@test_channel"
    name: "Test Channel"

settings:
  target_user_id: 123456789
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    # Remove all required env vars
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # Mock load_dotenv to prevent loading from .env file
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="Missing required environment variables"):
            load_config(str(config_file))


@pytest.mark.unit
def test_load_config_invalid_target_user(tmp_path, mock_env_vars):
    """Test error handling for invalid target user ID."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 0
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with pytest.raises(ValueError, match="target_user_id not configured"):
        load_config(str(config_file))


@pytest.mark.unit
def test_load_config_no_channels(tmp_path, mock_env_vars):
    """Test error handling for no channels configured."""
    config_content = """
channels: []

settings:
  target_user_id: 123456789
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with pytest.raises(ValueError, match="No channels configured"):
        load_config(str(config_file))


@pytest.mark.unit
def test_load_config_ollama_provider(tmp_path, monkeypatch):
    """Test config loading with Ollama provider (no API keys needed)."""
    monkeypatch.setenv("TELEGRAM_API_ID", "12345678")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test_hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF")

    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  ai_provider: "ollama"
  ai_model: "llama3"
  ollama_base_url: "http://myserver:11434"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with patch("src.config_loader.load_dotenv"):
        config = load_config(str(config_file))

    assert config.settings.ai_provider == "ollama"
    assert config.settings.ai_model == "llama3"
    assert config.settings.ollama_base_url == "http://myserver:11434"


@pytest.mark.unit
def test_load_config_anthropic_provider_missing_key(tmp_path, monkeypatch):
    """Test Anthropic provider requires ANTHROPIC_API_KEY."""
    monkeypatch.setenv("TELEGRAM_API_ID", "12345678")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test_hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  ai_provider: "anthropic"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            load_config(str(config_file))


@pytest.mark.unit
def test_load_config_unsupported_provider(tmp_path, mock_env_vars):
    """Test error for unsupported ai_provider value."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  ai_provider: "gemini"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with pytest.raises(ValueError, match="Unsupported ai_provider"):
        load_config(str(config_file))


@pytest.mark.unit
def test_load_config_null_ai_provider(tmp_path, mock_env_vars):
    """Test that ai_provider: null gives a clear ValueError, not AttributeError."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  ai_provider: null
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with pytest.raises(ValueError, match="ai_provider must be a string"):
        load_config(str(config_file))


@pytest.mark.unit
def test_load_config_openrouter_model_env_override(tmp_path, monkeypatch):
    """OPENROUTER_MODEL env var overrides settings.ai_model and syncs config.openrouter_model."""
    monkeypatch.setenv("TELEGRAM_API_ID", "12345678")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test_hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "minimax/minimax-m3:free")

    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  ai_provider: "openrouter"
  ai_model: "google/gemini-3.7-flash"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with patch("src.config_loader.load_dotenv"):
        config = load_config(str(config_file))

    assert config.settings.ai_provider == "openrouter"
    assert config.settings.ai_model == "minimax/minimax-m3:free"
    assert config.openrouter_model == "minimax/minimax-m3:free"


@pytest.mark.unit
def test_load_config_ai_model_env_override(tmp_path, monkeypatch):
    """AI_MODEL env var overrides settings.ai_model across providers."""
    monkeypatch.setenv("TELEGRAM_API_ID", "12345678")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test_hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("AI_MODEL", "custom-model-from-env")

    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  ai_provider: "openai"
  ai_model: "gpt-5-nano"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with patch("src.config_loader.load_dotenv"):
        config = load_config(str(config_file))

    assert config.settings.ai_model == "custom-model-from-env"


@pytest.mark.unit
def test_load_config_openrouter_defaults_from_config_yaml(tmp_path, monkeypatch):
    """When OPENROUTER_MODEL is unset, config.yaml ai_model populates openrouter_model."""
    monkeypatch.setenv("TELEGRAM_API_ID", "12345678")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test_hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)

    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  ai_provider: "openrouter"
  ai_model: "minimax/minimax-m3:free"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with patch("src.config_loader.load_dotenv"):
        config = load_config(str(config_file))

    assert config.settings.ai_model == "minimax/minimax-m3:free"
    assert config.openrouter_model == "minimax/minimax-m3:free"


@pytest.mark.unit
def test_load_config_rejects_forbidden_model_in_openrouter_model(tmp_path, monkeypatch):
    """Setting OPENROUTER_MODEL to deepseek/deepseek-chat raises ValueError."""
    monkeypatch.setenv("TELEGRAM_API_ID", "12345678")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test_hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-chat")

    config_content = """
channels:
  - id: "@test"
    name: "Test"
settings:
  target_user_id: 123456789
  ai_provider: "openrouter"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="strictly forbidden"):
            load_config(str(config_file))


@pytest.mark.unit
def test_load_config_rejects_forbidden_model_in_openrouter_model_2(tmp_path, monkeypatch):
    """Setting OPENROUTER_MODEL_2 to deepseek/deepseek-chat raises ValueError."""
    monkeypatch.setenv("TELEGRAM_API_ID", "12345678")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test_hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "minimax/minimax-m3:free:floor")
    monkeypatch.setenv("OPENROUTER_MODEL_2", "deepseek/deepseek-chat")

    config_content = """
channels:
  - id: "@test"
    name: "Test"
settings:
  target_user_id: 123456789
  ai_provider: "openrouter"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="strictly forbidden"):
            load_config(str(config_file))


@pytest.mark.unit
def test_load_config_temperature_fallback(tmp_path, mock_env_vars):
    """Test that temperature falls back to openai_temperature when not set."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  openai_temperature: 0.5
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    config = load_config(str(config_file))

    assert config.settings.temperature == 0.5
    assert config.settings.openai_temperature == 0.5


@pytest.mark.unit
def test_load_config_temperature_override(tmp_path, mock_env_vars):
    """Test that explicit temperature overrides openai_temperature."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  openai_temperature: 0.5
  temperature: 0.9
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    config = load_config(str(config_file))

    assert config.settings.temperature == 0.9


@pytest.mark.unit
def test_load_config_api_timeout_string_coercion(tmp_path, mock_env_vars):
    """Test that api_timeout is coerced to int even when YAML provides a string."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  api_timeout: "60"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    config = load_config(str(config_file))

    assert config.settings.api_timeout == 60
    assert isinstance(config.settings.api_timeout, int)


@pytest.mark.unit
def test_load_config_default_max_tokens_per_summary(tmp_path, mock_env_vars):
    """Test that max_tokens_per_summary defaults to 96000 when not specified."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    config = load_config(str(config_file))

    assert config.settings.max_tokens_per_summary == 96000


@pytest.mark.unit
def test_load_config_default_max_prompt_chars(tmp_path, mock_env_vars):
    """Test that max_prompt_chars defaults to 8000 when not specified."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    config = load_config(str(config_file))

    assert config.settings.max_prompt_chars == 8000


@pytest.mark.unit
def test_load_config_custom_max_prompt_chars(tmp_path, mock_env_vars):
    """Test that max_prompt_chars is loaded correctly from YAML."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  max_prompt_chars: 4000
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    config = load_config(str(config_file))

    assert config.settings.max_prompt_chars == 4000


@pytest.mark.unit
def test_load_config_digest_mode_with_groups(tmp_path, mock_env_vars):
    """Test valid digest config loads correctly."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  digest_mode: "digest"
  digest_groups:
    - name: "Events"
      description: "Conferences and meetups"
    - name: "News"
      description: "World affairs"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    config = load_config(str(config_file))

    assert config.settings.digest_mode == "digest"
    assert len(config.settings.digest_groups) == 2
    assert config.settings.digest_groups[0] == DigestGroupConfig(
        name="Events", description="Conferences and meetups"
    )
    assert config.settings.digest_groups[1] == DigestGroupConfig(
        name="News", description="World affairs"
    )


@pytest.mark.unit
def test_load_config_digest_defaults(tmp_path, mock_env_vars):
    """Test missing digest_groups defaults to empty list, digest_mode defaults to channel."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    config = load_config(str(config_file))

    assert config.settings.digest_mode == "channel"
    assert config.settings.digest_groups == []


@pytest.mark.unit
def test_load_config_invalid_digest_mode(tmp_path, mock_env_vars):
    """Test invalid digest_mode value raises ValueError."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  digest_mode: "invalid"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with pytest.raises(ValueError, match="Invalid digest_mode"):
        load_config(str(config_file))


@pytest.mark.unit
def test_load_config_digest_mode_empty_groups_warns(tmp_path, mock_env_vars, caplog):
    """Test digest_mode 'digest' with empty digest_groups logs warning."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  digest_mode: "digest"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with caplog.at_level(logging.WARNING, logger="telebrief"):
        config = load_config(str(config_file))

    assert config.settings.digest_mode == "digest"
    assert config.settings.digest_groups == []
    assert "digest mode enabled but no digest_groups configured" in caplog.text


@pytest.mark.unit
def test_load_config_digest_groups_null(tmp_path, mock_env_vars):
    """Test digest_groups: null (YAML key with no value) defaults to empty list."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  digest_groups:
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    config = load_config(str(config_file))
    assert config.settings.digest_groups == []


@pytest.mark.unit
def test_load_config_digest_groups_non_string_fields(tmp_path, mock_env_vars):
    """Test digest_groups with non-string name/description raises ValueError."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  digest_groups:
    - name: 123
      description: "A numeric name"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with pytest.raises(ValueError, match="must be strings"):
        load_config(str(config_file))


@pytest.mark.unit
def test_load_config_valid_output_languages(tmp_path, mock_env_vars):
    """Test that all supported languages are accepted."""
    for lang in ("English", "Russian", "Spanish", "German", "French"):
        config_content = f"""
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  output_language: "{lang}"
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content)

        config = load_config(str(config_file))
        assert config.settings.output_language == lang


@pytest.mark.unit
def test_load_config_invalid_output_language(tmp_path, mock_env_vars):
    """Test that invalid output_language raises ValueError with supported list."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789
  output_language: "Klingon"
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with pytest.raises(ValueError, match="Unsupported output_language") as exc_info:
        load_config(str(config_file))

    error_msg = str(exc_info.value)
    assert "Klingon" in error_msg
    for lang in ("English", "Russian", "Spanish", "German", "French"):
        assert lang in error_msg


@pytest.mark.unit
def test_load_config_per_channel_overrides(tmp_path, mock_env_vars):
    """Per-channel lookback_hours and prompt_extra are parsed into ChannelConfig."""
    config_content = """
channels:
  - id: "@jobs"
    name: "Jobs"
    lookback_hours: 72
    prompt_extra: "Focus on backend roles only."
  - id: "@news"
    name: "News"

settings:
  target_user_id: 123456789
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    config = load_config(str(config_file))

    jobs, news = config.channels
    assert jobs.lookback_hours == 72
    assert jobs.prompt_extra == "Focus on backend roles only."
    assert news.lookback_hours is None
    assert news.prompt_extra == ""


@pytest.mark.unit
def test_load_config_invalid_lookback_hours_type(tmp_path, mock_env_vars):
    """Non-int lookback_hours raises ValueError."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"
    lookback_hours: "72h"

settings:
  target_user_id: 123456789
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with pytest.raises(ValueError, match="lookback_hours must be an int"):
        load_config(str(config_file))


@pytest.mark.unit
def test_load_config_invalid_lookback_hours_value(tmp_path, mock_env_vars):
    """Non-positive lookback_hours raises ValueError."""
    config_content = """
channels:
  - id: "@test"
    name: "Test"
    lookback_hours: 0

settings:
  target_user_id: 123456789
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with pytest.raises(ValueError, match="lookback_hours must be positive"):
        load_config(str(config_file))


@pytest.mark.unit
def test_load_config_rejects_removed_schedule_jobs(tmp_path, mock_env_vars):
    """The removed multi-schedule setting fails with an actionable error."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@news"
    name: "News"
settings:
  schedule_time: "09:00"
  timezone: "UTC"
  lookback_hours: 24
  target_user_id: 123456789
  schedule_jobs:
    - name: evening
      time: "21:00"
      lookback_hours: 12
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schedule_jobs.*no longer supported"):
        load_config(str(config_file))


@pytest.mark.unit
def test_load_config_duplicate_channel_names(tmp_path, mock_env_vars):
    """Duplicate channel names raise ValueError to prevent silent override loss."""
    config_content = """
channels:
  - id: "@first"
    name: "Same Name"
  - id: "@second"
    name: "Same Name"

settings:
  target_user_id: 123456789
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)

    with pytest.raises(ValueError, match="Duplicate channel names.*Same Name"):
        load_config(str(config_file))


# ---------------------------------------------------------------------------
# StorageConfig tests
# ---------------------------------------------------------------------------


def _storage_config_file(tmp_path, storage_block: str) -> str:
    content = f"""
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789

{storage_block}
"""
    p = tmp_path / "config.yaml"
    p.write_text(content)
    return str(p)


@pytest.mark.unit
def test_storage_config_missing_block_defaults(tmp_path, mock_env_vars):
    config = load_config(_storage_config_file(tmp_path, ""))
    assert config.storage == StorageConfig()
    assert config.storage.enabled is False
    assert config.storage.backend == "sqlite"
    assert config.storage.path == "data/messages.db"
    assert config.storage.url == ""


@pytest.mark.unit
def test_storage_config_sqlite_explicit(tmp_path, mock_env_vars):
    block = """
storage:
  enabled: true
  backend: sqlite
  path: /tmp/test.db
"""
    config = load_config(_storage_config_file(tmp_path, block))
    assert config.storage.enabled is True
    assert config.storage.backend == "sqlite"
    assert config.storage.path == "/tmp/test.db"


@pytest.mark.unit
def test_storage_config_postgres_with_url(tmp_path, mock_env_vars):
    block = """
storage:
  enabled: true
  backend: postgres
  url: "postgresql://user:pass@localhost:5432/db"
"""
    config = load_config(_storage_config_file(tmp_path, block))
    assert config.storage.enabled is True
    assert config.storage.backend == "postgres"
    assert config.storage.url == "postgresql://user:pass@localhost:5432/db"


@pytest.mark.unit
def test_storage_config_postgres_enabled_empty_url_raises(tmp_path, mock_env_vars):
    block = """
storage:
  enabled: true
  backend: postgres
  url: ""
"""
    with pytest.raises(ValueError, match="storage.url must be set"):
        load_config(_storage_config_file(tmp_path, block))


@pytest.mark.unit
def test_storage_config_postgres_disabled_empty_url_ok(tmp_path, mock_env_vars):
    block = """
storage:
  enabled: false
  backend: postgres
  url: ""
"""
    config = load_config(_storage_config_file(tmp_path, block))
    assert config.storage.enabled is False


@pytest.mark.unit
def test_storage_config_invalid_backend_raises(tmp_path, mock_env_vars):
    block = """
storage:
  backend: mysql
"""
    with pytest.raises(ValueError, match="storage.backend must be"):
        load_config(_storage_config_file(tmp_path, block))


@pytest.mark.unit
def test_storage_config_enabled_not_bool_raises(tmp_path, mock_env_vars):
    block = """
storage:
  enabled: "yes"
"""
    with pytest.raises(ValueError, match="storage.enabled must be a bool"):
        load_config(_storage_config_file(tmp_path, block))


@pytest.mark.unit
def test_storage_config_empty_path_raises(tmp_path, mock_env_vars):
    block = """
storage:
  path: ""
"""
    with pytest.raises(
        ValueError, match="storage.path must be a non-empty string when backend is 'sqlite'"
    ):
        load_config(_storage_config_file(tmp_path, block))


@pytest.mark.unit
def test_storage_config_url_not_string_raises(tmp_path, mock_env_vars):
    block = """
storage:
  url: 12345
"""
    with pytest.raises(ValueError, match="storage.url must be a string"):
        load_config(_storage_config_file(tmp_path, block))


@pytest.mark.unit
def test_storage_config_not_mapping_raises(tmp_path, mock_env_vars):
    block = "storage: true"
    with pytest.raises(ValueError, match="'storage' must be a mapping"):
        load_config(_storage_config_file(tmp_path, block))


# ---------------------------------------------------------------------------
# FilterSpec / filter chain parsing tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filter_spec_defaults_to_empty_global(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\nsettings:\n  target_user_id: 123456789\n'
    )
    config = load_config(str(p))
    assert config.settings.filters == []


@pytest.mark.unit
def test_filter_spec_global_parsed(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "  filters:\n"
        "    - class_path: src.extensions.filters.KeywordFilter\n"
        "      config:\n"
        "        include:\n          - job\n"
        "        exclude:\n          - nsfw\n"
    )
    config = load_config(str(p))
    assert len(config.settings.filters) == 1
    spec = config.settings.filters[0]
    assert spec == FilterSpec(
        class_path="src.extensions.filters.KeywordFilter",
        config={"include": ["job"], "exclude": ["nsfw"]},
    )


@pytest.mark.unit
def test_filter_spec_config_defaults_to_empty_dict(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "  filters:\n"
        "    - class_path: src.extensions.filters.MinLengthFilter\n"
    )
    config = load_config(str(p))
    assert config.settings.filters[0].config == {}


@pytest.mark.unit
def test_filter_spec_channel_null_uses_global(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n    filters:\n'
        "settings:\n  target_user_id: 123456789\n"
    )
    config = load_config(str(p))
    assert config.channels[0].filters is None


@pytest.mark.unit
def test_filter_spec_channel_empty_list_explicit_noop(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n    filters: []\n'
        "settings:\n  target_user_id: 123456789\n"
    )
    config = load_config(str(p))
    assert config.channels[0].filters == []


@pytest.mark.unit
def test_filter_spec_channel_override(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "    filters:\n"
        "      - class_path: src.extensions.filters.KeywordFilter\n"
        "        config:\n          include:\n            - python\n"
        "settings:\n  target_user_id: 123456789\n"
        "  filters:\n"
        "    - class_path: src.extensions.filters.MinLengthFilter\n"
        "      config:\n        min_chars: 10\n"
    )
    config = load_config(str(p))
    assert len(config.settings.filters) == 1
    assert config.settings.filters[0].class_path == "src.extensions.filters.MinLengthFilter"
    assert len(config.channels[0].filters) == 1
    assert config.channels[0].filters[0].class_path == "src.extensions.filters.KeywordFilter"


@pytest.mark.unit
def test_filter_spec_missing_class_path_raises(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "  filters:\n    - config:\n        min_chars: 10\n"
    )
    with pytest.raises(ValueError, match="missing required field 'class_path'"):
        load_config(str(p))


@pytest.mark.unit
def test_filter_spec_non_string_class_path_raises(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "  filters:\n    - class_path: 42\n"
    )
    with pytest.raises(ValueError, match="class_path must be a non-empty string"):
        load_config(str(p))


@pytest.mark.unit
def test_filter_spec_non_dict_config_raises(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "  filters:\n"
        "    - class_path: src.extensions.filters.MinLengthFilter\n"
        "      config: not a dict\n"
    )
    with pytest.raises(ValueError, match="config must be a mapping"):
        load_config(str(p))


@pytest.mark.unit
def test_filter_spec_non_mapping_item_raises(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "  filters:\n    - src.extensions.filters.MinLengthFilter\n"
    )
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(str(p))


@pytest.mark.unit
def test_filter_spec_channel_invalid_class_path_raises(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "    filters:\n      - class_path: 99\n"
        "settings:\n  target_user_id: 123456789\n"
    )
    with pytest.raises(ValueError, match="class_path must be a non-empty string"):
        load_config(str(p))


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_path",
    ["NoDots", "trailing.", ".leading", "two..dots", "has space.Cls", "1bad.Cls"],
)
def test_filter_spec_invalid_dotted_path_raises(tmp_path, mock_env_vars, bad_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        f"  filters:\n    - class_path: {bad_path!r}\n"
    )
    with pytest.raises(ValueError, match="must be a dotted path"):
        load_config(str(p))


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_composer",
    ["NoDots", "trailing.", ".leading", "two..dots", "has space.Cls", "1bad.Cls"],
)
def test_prompts_composer_invalid_dotted_path_raises(tmp_path, mock_env_vars, bad_composer):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "prompts:\n"
        f"  composer: {bad_composer!r}\n"
    )
    with pytest.raises(ValueError, match="prompts.composer must be a dotted path"):
        load_config(str(p))


@pytest.mark.unit
def test_prompts_composer_empty_string_allowed(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "prompts:\n"
        "  composer: ''\n"
    )
    config = load_config(str(p))
    assert config.prompts.composer == ""


@pytest.mark.unit
def test_prompts_config_strips_whitespace(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "prompts:\n"
        "  base_template: '  src/prompts/base_summary.txt  '\n"
        "  composer: '  src.extensions.prompts.DefaultComposer  '\n"
    )
    config = load_config(str(p))
    assert config.prompts.base_template == "src/prompts/base_summary.txt"
    assert config.prompts.composer == "src.extensions.prompts.DefaultComposer"


@pytest.mark.unit
def test_sample_config_fixture_still_constructs(sample_config):
    assert sample_config.settings.filters == []
    assert sample_config.channels[0].filters is None


# ---------------------------------------------------------------------------
# Group binding and PromptsConfig tests (Task 8)
# ---------------------------------------------------------------------------


def _group_config_file(tmp_path, channels_block: str, settings_block: str, extra: str = "") -> str:
    content = f"""
channels:
{channels_block}

settings:
  target_user_id: 123456789
{settings_block}
{extra}
"""
    p = tmp_path / "config.yaml"
    p.write_text(content)
    return str(p)


@pytest.mark.unit
def test_digest_group_prompt_extra_parsed(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "  digest_groups:\n"
        "    - name: Jobs\n"
        "      description: Job offers\n"
        "      prompt_extra: Focus on remote roles.\n"
    )
    config = load_config(str(p))
    assert config.settings.digest_groups[0].prompt_extra == "Focus on remote roles."


@pytest.mark.unit
def test_digest_group_prompt_extra_defaults_empty(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "  digest_groups:\n"
        "    - name: Jobs\n"
        "      description: Job offers\n"
    )
    config = load_config(str(p))
    assert config.settings.digest_groups[0].prompt_extra == ""


@pytest.mark.unit
def test_digest_group_prompt_extra_non_string_raises(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "  digest_groups:\n"
        "    - name: Jobs\n"
        "      description: Job offers\n"
        "      prompt_extra: 42\n"
    )
    with pytest.raises(ValueError, match="prompt_extra must be a string"):
        load_config(str(p))


@pytest.mark.unit
def test_channel_group_field_parsed(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@jobs"\n    name: "Jobs"\n    group: MyGroup\n'
        "settings:\n  target_user_id: 123456789\n"
        "  digest_groups:\n"
        "    - name: MyGroup\n"
        "      description: My group\n"
    )
    config = load_config(str(p))
    assert config.channels[0].group == "MyGroup"


@pytest.mark.unit
def test_channel_group_none_by_default(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\nsettings:\n  target_user_id: 123456789\n'
    )
    config = load_config(str(p))
    assert config.channels[0].group is None


@pytest.mark.unit
def test_channel_group_other_accepted(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n    group: Other\n'
        "settings:\n  target_user_id: 123456789\n"
    )
    config = load_config(str(p))
    assert config.channels[0].group == "Other"


@pytest.mark.unit
def test_channel_group_localized_other_accepted(tmp_path, mock_env_vars):
    # Russian localized "Other" is "Другое"
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n    group: "Другое"\n'
        "settings:\n  target_user_id: 123456789\n  output_language: Russian\n"
    )
    config = load_config(str(p))
    assert config.channels[0].group == "Другое"


@pytest.mark.unit
def test_channel_unknown_group_raises(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n    group: NonExistent\n'
        "settings:\n  target_user_id: 123456789\n"
    )
    with pytest.raises(ValueError, match="Unknown group references"):
        load_config(str(p))


@pytest.mark.unit
def test_channel_unknown_group_error_lists_bad_channel(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "MyChan"\n    group: BadGroup\n'
        "settings:\n  target_user_id: 123456789\n"
    )
    with pytest.raises(ValueError) as exc_info:
        load_config(str(p))
    assert "MyChan" in str(exc_info.value)
    assert "BadGroup" in str(exc_info.value)


@pytest.mark.unit
def test_channel_group_null_yaml_is_none(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n    group:\n'
        "settings:\n  target_user_id: 123456789\n"
    )
    config = load_config(str(p))
    assert config.channels[0].group is None


# ---------------------------------------------------------------------------
# PromptsConfig tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prompts_config_missing_block_defaults(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\nsettings:\n  target_user_id: 123456789\n'
    )
    config = load_config(str(p))
    assert config.prompts == PromptsConfig()
    assert config.prompts.base_template == "src/prompts/base_summary.txt"
    assert config.prompts.composer == ""


@pytest.mark.unit
def test_prompts_config_explicit_values(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "prompts:\n"
        "  base_template: custom/my_template.txt\n"
        "  composer: myapp.prompts.CustomComposer\n"
    )
    config = load_config(str(p))
    assert config.prompts.base_template == "custom/my_template.txt"
    assert config.prompts.composer == "myapp.prompts.CustomComposer"


@pytest.mark.unit
def test_prompts_config_not_mapping_raises(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "prompts: true\n"
    )
    with pytest.raises(ValueError, match="'prompts' must be a mapping"):
        load_config(str(p))


@pytest.mark.unit
def test_prompts_config_empty_base_template_raises(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "prompts:\n  base_template: ''\n"
    )
    with pytest.raises(ValueError, match="base_template must be a non-empty string"):
        load_config(str(p))


@pytest.mark.unit
def test_prompts_config_composer_not_string_raises(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "prompts:\n  composer: 42\n"
    )
    with pytest.raises(ValueError, match="prompts.composer must be a string"):
        load_config(str(p))


@pytest.mark.unit
def test_digest_group_config_equality_with_prompt_extra(tmp_path, mock_env_vars):
    p = tmp_path / "config.yaml"
    p.write_text(
        'channels:\n  - id: "@test"\n    name: "Test"\n'
        "settings:\n  target_user_id: 123456789\n"
        "  digest_groups:\n"
        "    - name: Events\n"
        "      description: Conferences\n"
        "      prompt_extra: Be concise.\n"
    )
    config = load_config(str(p))
    assert config.settings.digest_groups[0] == DigestGroupConfig(
        name="Events", description="Conferences", prompt_extra="Be concise."
    )


# ---------------------------------------------------------------------------
# McpConfig tests
# ---------------------------------------------------------------------------


def _mcp_config_file(tmp_path, mcp_block: str) -> str:
    content = f"""
channels:
  - id: "@test"
    name: "Test"

settings:
  target_user_id: 123456789

{mcp_block}
"""
    p = tmp_path / "config.yaml"
    p.write_text(content)
    return str(p)


@pytest.mark.unit
def test_mcp_config_missing_block_defaults(tmp_path, mock_env_vars):
    """No mcp: block leaves the server disabled on loopback."""
    config = load_config(_mcp_config_file(tmp_path, ""))
    assert config.mcp == McpConfig()
    assert config.mcp.enabled is False
    assert config.mcp.host == "127.0.0.1"
    assert config.mcp.port == 8765
    assert config.mcp.path == "/mcp"


@pytest.mark.unit
def test_mcp_config_explicit(tmp_path, mock_env_vars):
    """An explicit block is parsed field by field."""
    block = """
mcp:
  enabled: true
  host: "0.0.0.0"
  port: 9000
  path: "/telebrief"
"""
    config = load_config(_mcp_config_file(tmp_path, block))
    assert config.mcp.enabled is True
    assert config.mcp.host == "0.0.0.0"
    assert config.mcp.port == 9000
    assert config.mcp.path == "/telebrief"


@pytest.mark.unit
@pytest.mark.parametrize(
    "block,match",
    [
        ('mcp:\n  enabled: "yes"\n', "mcp.enabled must be a bool"),
        ("mcp:\n  host: 123\n", "mcp.host must be a non-empty string"),
        ('mcp:\n  host: "  "\n', "mcp.host must be a non-empty string"),
        ("mcp:\n  port: 0\n", "mcp.port must be an int"),
        ("mcp:\n  port: 70000\n", "mcp.port must be an int"),
        ('mcp:\n  port: "8765"\n', "mcp.port must be an int"),
        ('mcp:\n  path: "mcp"\n', "mcp.path must be a string starting"),
        ("mcp: notamapping\n", "'mcp' must be a mapping"),
    ],
)
def test_mcp_config_rejects_invalid(tmp_path, mock_env_vars, block, match):
    """Malformed mcp blocks fail loudly at load time, not at request time."""
    with pytest.raises(ValueError, match=match):
        load_config(_mcp_config_file(tmp_path, block))


# ---------------------------------------------------------------------------
# DatabaseConfig tests (multisource foundation)
# ---------------------------------------------------------------------------


def _write_minimal_config(tmp_path) -> str:
    return _write_config(tmp_path)


def _write_config(tmp_path, extra_blocks: dict | None = None) -> str:
    doc = {
        "channels": [{"id": "@test", "name": "Test"}],
        "settings": {"target_user_id": 123456789},
    }
    if extra_blocks:
        doc.update(extra_blocks)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(doc))
    return str(p)


@pytest.mark.unit
def test_database_config_defaults(monkeypatch, tmp_path, mock_env_vars):
    monkeypatch.setenv("DATABASE_URL", "postgresql://telebrief:test@localhost/telebrief")
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=_write_minimal_config(tmp_path))
    assert config.database.min_pool_size == 1
    assert config.database.max_pool_size == 3
    assert config.database.domain_schema == "public"
    assert config.database.procrastinate_schema == "procrastinate"


@pytest.mark.unit
def test_database_pool_max_must_cover_min(monkeypatch, tmp_path, mock_env_vars):
    monkeypatch.setenv("DATABASE_URL", "postgresql://telebrief:test@localhost/telebrief")
    path = _write_config(tmp_path, {"database": {"min_pool_size": 4, "max_pool_size": 2}})
    with pytest.raises(ValueError, match="max_pool_size"):
        load_config(path=path)


@pytest.mark.unit
def test_database_disabled_by_default_without_block_or_env(monkeypatch, tmp_path, mock_env_vars):
    """Migration phase: no database block and no DATABASE_URL keeps Postgres off."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=_write_minimal_config(tmp_path))
    assert config.database == DatabaseConfig()
    assert config.database.enabled is False
    assert config.database.url == ""


@pytest.mark.unit
def test_database_enabled_block_parsed_from_env_url(monkeypatch, tmp_path, mock_env_vars):
    """The connection URL comes from DATABASE_URL only and never leaks via repr."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://telebrief:test@localhost/telebrief")
    path = _write_config(tmp_path, {"database": {"enabled": True}})
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=path)
    assert config.database.enabled is True
    assert config.database.url == "postgresql://telebrief:test@localhost/telebrief"
    assert "telebrief:test" not in repr(config.database)


@pytest.mark.unit
def test_database_custom_values_parsed(monkeypatch, tmp_path, mock_env_vars):
    monkeypatch.setenv("DATABASE_URL", "postgresql://telebrief:test@localhost/telebrief")
    path = _write_config(
        tmp_path,
        {
            "database": {
                "enabled": True,
                "min_pool_size": 2,
                "max_pool_size": 8,
                "domain_schema": "telebrief",
                "procrastinate_schema": "jobs",
            }
        },
    )
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=path)
    assert config.database == DatabaseConfig(
        enabled=True,
        url="postgresql://telebrief:test@localhost/telebrief",
        min_pool_size=2,
        max_pool_size=8,
        domain_schema="telebrief",
        procrastinate_schema="jobs",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "block",
    [
        {"min_pool_size": 0, "max_pool_size": 4},
        {"min_pool_size": -1, "max_pool_size": 4},
        {"min_pool_size": 11, "max_pool_size": 12},
        {"min_pool_size": 4, "max_pool_size": 2},
        {"min_pool_size": 1, "max_pool_size": 11},
    ],
)
def test_database_invalid_pool_sizes_raise(monkeypatch, tmp_path, mock_env_vars, block):
    """Pool sizes must satisfy 1 <= min_pool_size <= max_pool_size <= 10."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://telebrief:test@localhost/telebrief")
    path = _write_config(tmp_path, {"database": block})
    with pytest.raises(ValueError, match="pool"):
        load_config(path=path)


@pytest.mark.unit
def test_database_enabled_requires_database_url(monkeypatch, tmp_path, mock_env_vars):
    """An enabled database block without DATABASE_URL fails clearly at load time."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = _write_config(tmp_path, {"database": {"enabled": True}})
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="DATABASE_URL"):
            load_config(path=path)


@pytest.mark.unit
def test_load_database_config_reads_block_without_telegram_creds(monkeypatch, tmp_path):
    """load_database_config needs only the database block + DATABASE_URL env var."""
    for var in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://telebrief:test@localhost/telebrief")
    path = _write_config(tmp_path, {"database": {"enabled": True}})
    with patch("src.config_loader.load_dotenv"):
        db = load_database_config(path, require_enabled=True)
    assert db == DatabaseConfig(
        enabled=True,
        url="postgresql://telebrief:test@localhost/telebrief",
    )
    assert "telebrief:test" not in repr(db)


@pytest.mark.unit
def test_load_database_config_require_enabled_rejects_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with (
        patch("src.config_loader.load_dotenv"),
        pytest.raises(ValueError, match="database must be enabled"),
    ):
        load_database_config(_write_minimal_config(tmp_path), require_enabled=True)


@pytest.mark.unit
def test_load_database_config_require_enabled_rejects_missing_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = _write_config(tmp_path, {"database": {"enabled": True}})
    with (
        patch("src.config_loader.load_dotenv"),
        pytest.raises(ValueError, match="DATABASE_URL"),
    ):
        load_database_config(path, require_enabled=True)


# --- settings.persistent_ingestion (transitional cutover flag) ---


@pytest.mark.unit
def test_persistent_ingestion_defaults_to_false(monkeypatch, tmp_path, mock_env_vars):
    """Legacy live-Telegram collection remains the default until operator cutover."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=_write_minimal_config(tmp_path))
    assert config.settings.persistent_ingestion is False


@pytest.mark.unit
@pytest.mark.parametrize("value", ["yes", 1, None])
def test_persistent_ingestion_must_be_bool(monkeypatch, tmp_path, mock_env_vars, value):
    path = _write_config(
        tmp_path, {"settings": {"target_user_id": 123456789, "persistent_ingestion": value}}
    )
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="persistent_ingestion must be a bool"):
            load_config(path=path)


@pytest.mark.unit
def test_persistent_ingestion_requires_enabled_database(monkeypatch, tmp_path, mock_env_vars):
    """Enabling the flag without database.enabled=true fails with a clear error."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = _write_config(
        tmp_path, {"settings": {"target_user_id": 123456789, "persistent_ingestion": True}}
    )
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="persistent_ingestion.*database.enabled"):
            load_config(path=path)


@pytest.mark.unit
def test_persistent_ingestion_accepted_with_database_enabled(monkeypatch, tmp_path, mock_env_vars):
    monkeypatch.setenv("DATABASE_URL", "postgresql://telebrief:test@localhost/telebrief")
    path = _write_config(
        tmp_path,
        {
            "database": {"enabled": True},
            "settings": {"target_user_id": 123456789, "persistent_ingestion": True},
        },
    )
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=path)
    assert config.settings.persistent_ingestion is True


# --- settings.vision_mode (Plan 3 Task 3: bounded Vision analysis) ---


@pytest.mark.unit
def test_vision_mode_defaults_to_relevance_only(monkeypatch, tmp_path, mock_env_vars):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=_write_minimal_config(tmp_path))
    assert config.settings.vision_mode == "relevance_only"


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["off", "relevance_only", "full"])
def test_vision_mode_accepts_all_supported_modes(monkeypatch, tmp_path, mock_env_vars, mode):
    path = _write_config(tmp_path, {"settings": {"target_user_id": 123456789, "vision_mode": mode}})
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=path)
    assert config.settings.vision_mode == mode


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["sometimes", "", 42, None])
def test_vision_mode_rejects_unknown_values(monkeypatch, tmp_path, mock_env_vars, mode):
    path = _write_config(tmp_path, {"settings": {"target_user_id": 123456789, "vision_mode": mode}})
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="vision_mode"):
            load_config(path=path)


# --- config.embedding (Plan 3 Task 5: semantic embeddings) ---


@pytest.mark.unit
def test_embedding_config_defaults(monkeypatch, tmp_path, mock_env_vars):
    """Embeddings default to Google gemini-embedding-2 at 1536 dimensions."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=_write_minimal_config(tmp_path))
    assert config.embedding == EmbeddingConfig(
        provider="google", model="gemini-embedding-2", dimensions=1536, timeout=45
    )


@pytest.mark.unit
def test_embedding_config_always_present(monkeypatch, tmp_path, mock_env_vars):
    """config.embedding exists even when the YAML block is absent entirely."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=_write_minimal_config(tmp_path))
    assert config.embedding.provider == "google"
    assert config.embedding.dimensions == 1536


@pytest.mark.unit
def test_embedding_config_custom_block(monkeypatch, tmp_path, mock_env_vars):
    path = _write_config(
        tmp_path,
        {
            "embedding": {
                "provider": "google",
                "model": "gemini-embedding-2",
                "dimensions": 768,
                "timeout": 20,
            }
        },
    )
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=path)
    assert config.embedding.model == "gemini-embedding-2"
    assert config.embedding.dimensions == 768
    assert config.embedding.timeout == 20


@pytest.mark.unit
def test_embedding_config_accepts_dimension_bounds(monkeypatch, tmp_path, mock_env_vars):
    """Both inclusive bounds of the supported dimensionality range parse."""
    for dims in (128, 8192):
        path = _write_config(tmp_path, {"embedding": {"dimensions": dims}})
        with patch("src.config_loader.load_dotenv"):
            config = load_config(path=path)
        assert config.embedding.dimensions == dims


@pytest.mark.unit
@pytest.mark.parametrize("dims", [64, 127, 8193, 10000])
def test_embedding_config_rejects_dimensions_out_of_bounds(
    monkeypatch, tmp_path, mock_env_vars, dims
):
    path = _write_config(tmp_path, {"embedding": {"dimensions": dims}})
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="dimensions must be an integer between 128 and 8192"):
            load_config(path=path)


@pytest.mark.unit
@pytest.mark.parametrize("dims", ["1536", 1536.0, True, None])
def test_embedding_config_rejects_non_integer_dimensions(
    monkeypatch, tmp_path, mock_env_vars, dims
):
    path = _write_config(tmp_path, {"embedding": {"dimensions": dims}})
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="embedding.dimensions must be an integer"):
            load_config(path=path)


@pytest.mark.unit
def test_embedding_config_rejects_unknown_provider(monkeypatch, tmp_path, mock_env_vars):
    path = _write_config(tmp_path, {"embedding": {"provider": "unsupported_provider"}})
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="embedding.provider"):
            load_config(path=path)


@pytest.mark.unit
def test_embedding_config_openrouter_provider(monkeypatch, tmp_path, mock_env_vars):
    """OpenRouter provider correctly parses model and resolves OPENROUTER_API_KEY."""
    secret = "openrouter-secret-key-xyz"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    path = _write_config(
        tmp_path,
        {
            "embedding": {
                "provider": "openrouter",
                "model": "qwen/qwen3-embedding-8b",
                "dimensions": 1536,
            }
        },
    )
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=path)
    assert config.embedding.provider == "openrouter"
    assert config.embedding.model == "qwen/qwen3-embedding-8b"
    assert config.embedding.dimensions == 1536
    assert config.embedding.api_key == secret
    assert secret not in repr(config.embedding)


@pytest.mark.unit
def test_embedding_config_openai_provider(monkeypatch, tmp_path, mock_env_vars):
    """OpenAI provider correctly parses model and resolves OPENAI_API_KEY."""
    secret = "openai-secret-key-123"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    path = _write_config(
        tmp_path,
        {
            "embedding": {
                "provider": "openai",
                "model": "text-embedding-3-small",
                "dimensions": 1536,
            }
        },
    )
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=path)
    assert config.embedding.provider == "openai"
    assert config.embedding.model == "text-embedding-3-small"
    assert config.embedding.api_key == secret


@pytest.mark.unit
@pytest.mark.parametrize("timeout", [0, -5])
def test_embedding_config_rejects_non_positive_timeout(
    monkeypatch, tmp_path, mock_env_vars, timeout
):
    path = _write_config(tmp_path, {"embedding": {"timeout": timeout}})
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="embedding.timeout must be a positive integer"):
            load_config(path=path)


@pytest.mark.unit
def test_embedding_config_rejects_empty_model(monkeypatch, tmp_path, mock_env_vars):
    path = _write_config(tmp_path, {"embedding": {"model": "  "}})
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="embedding.model must be a non-empty string"):
            load_config(path=path)


@pytest.mark.unit
def test_embedding_api_key_never_appears_in_repr(monkeypatch, tmp_path, mock_env_vars):
    """The Gemini key rides on EmbeddingConfig but is structurally unloggable."""
    secret = "gemini-secret-key-abc123"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    path = _write_config(tmp_path, {"settings": {"target_user_id": 123456789}})
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=path)
    assert config.embedding.api_key == secret
    assert secret not in repr(config.embedding)
    assert secret not in repr(config)


@pytest.mark.unit
def test_facebook_config_editorial_enabled_defaults_and_override(tmp_path, mock_env_vars):
    """facebook.editorial_enabled defaults to True and can be set to False."""
    path1 = _write_config(tmp_path, {"facebook": {"enabled": True}})
    with patch("src.config_loader.load_dotenv"):
        cfg1 = load_config(path=path1)
    assert cfg1.facebook.enabled is True
    assert cfg1.facebook.editorial_enabled is True

    path2 = _write_config(tmp_path, {"facebook": {"enabled": True, "editorial_enabled": False}})
    with patch("src.config_loader.load_dotenv"):
        cfg2 = load_config(path=path2)
    assert cfg2.facebook.enabled is True
    assert cfg2.facebook.editorial_enabled is False


@pytest.mark.unit
def test_facebook_config_rejects_invalid_editorial_enabled(tmp_path, mock_env_vars):
    path = _write_config(tmp_path, {"facebook": {"editorial_enabled": "invalid"}})
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="facebook.editorial_enabled must be a bool"):
            load_config(path=path)


@pytest.mark.unit
def test_event_pipeline_config_defaults(tmp_path, mock_env_vars):
    """settings.event_pipeline defaults to legacy_claims mode with sensible bounds."""
    path = _write_config(tmp_path, {"settings": {"target_user_id": 123456789}})
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=path)

    ep = config.settings.event_pipeline
    assert ep.mode == "legacy_claims"
    assert ep.fragment_max_chars == 1200
    assert ep.active_window_hours == 72
    assert ep.join_similarity == 0.84
    assert ep.max_cluster_candidates == 20
    assert ep.embedding_batch_size == 128
    assert ep.direct_analysis_min_fragments == 3
    assert ep.direct_analysis_min_unique_sources == 2
    assert ep.triage_batch_size == 30
    assert ep.triage_excerpt_chars == 320
    assert ep.triage_min_ignore_confidence == 0.95
    assert ep.analysis_quiet_seconds == 120
    assert ep.analysis_min_interval_seconds == 600
    assert ep.analysis_min_new_fragments == 3
    assert ep.analysis_max_calls_per_story_per_hour == 4
    assert ep.provider_retry_backoff_seconds == 300
    assert ep.analysis_max_input_chars == 24000
    assert ep.representative_fragment_limit == 16
    assert ep.live_batch_size == 100
    assert ep.backfill_batch_size == 500


@pytest.mark.unit
def test_event_pipeline_config_custom(tmp_path, mock_env_vars):
    """Custom settings.event_pipeline values are loaded correctly."""
    custom_ep = {
        "mode": "event_first",
        "fragment_max_chars": 1500,
        "active_window_hours": 48,
        "join_similarity": 0.88,
        "max_cluster_candidates": 30,
        "embedding_batch_size": 64,
        "direct_analysis_min_fragments": 2,
        "direct_analysis_min_unique_sources": 1,
        "triage_batch_size": 50,
        "triage_excerpt_chars": 200,
        "triage_min_ignore_confidence": 0.90,
        "analysis_quiet_seconds": 60,
        "analysis_min_interval_seconds": 300,
        "analysis_min_new_fragments": 2,
        "analysis_max_calls_per_story_per_hour": 6,
        "provider_retry_backoff_seconds": 120,
        "analysis_max_input_chars": 16000,
        "representative_fragment_limit": 12,
        "live_batch_size": 50,
        "backfill_batch_size": 200,
    }
    path = _write_config(
        tmp_path, {"settings": {"target_user_id": 123456789, "event_pipeline": custom_ep}}
    )
    with patch("src.config_loader.load_dotenv"):
        config = load_config(path=path)

    ep = config.settings.event_pipeline
    assert ep.mode == "event_first"
    assert ep.join_similarity == 0.88
    assert ep.fragment_max_chars == 1500
    assert ep.embedding_batch_size == 64


@pytest.mark.unit
@pytest.mark.parametrize("bad_mode", ["invalid", "claim_first", "", None, 123])
def test_event_pipeline_config_rejects_invalid_mode(tmp_path, mock_env_vars, bad_mode):
    path = _write_config(
        tmp_path,
        {"settings": {"target_user_id": 123456789, "event_pipeline": {"mode": bad_mode}}},
    )
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match="event_pipeline.mode must be one of"):
            load_config(path=path)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "bad_value", "err_pattern"),
    [
        ("join_similarity", 0.0, "join_similarity must be between 0.0 and 1.0"),
        ("join_similarity", 1.5, "join_similarity must be between 0.0 and 1.0"),
        ("join_similarity", -0.1, "join_similarity must be between 0.0 and 1.0"),
        (
            "triage_min_ignore_confidence",
            0.0,
            "triage_min_ignore_confidence must be between 0.0 and 1.0",
        ),
        (
            "triage_min_ignore_confidence",
            1.1,
            "triage_min_ignore_confidence must be between 0.0 and 1.0",
        ),
        ("fragment_max_chars", 0, "fragment_max_chars must be a positive integer"),
        ("fragment_max_chars", -10, "fragment_max_chars must be a positive integer"),
        ("fragment_max_chars", True, "fragment_max_chars must be a positive integer"),
        ("active_window_hours", 0, "active_window_hours must be a positive integer"),
        ("embedding_batch_size", 0, "embedding_batch_size must be a positive integer"),
        ("analysis_quiet_seconds", -1, "analysis_quiet_seconds must be a non-negative integer"),
        (
            "analysis_min_interval_seconds",
            -1,
            "analysis_min_interval_seconds must be a non-negative integer",
        ),
        ("analysis_max_input_chars", 0, "analysis_max_input_chars must be a positive integer"),
    ],
)
def test_event_pipeline_config_rejects_invalid_ranges(
    tmp_path, mock_env_vars, field, bad_value, err_pattern
):
    path = _write_config(
        tmp_path,
        {"settings": {"target_user_id": 123456789, "event_pipeline": {field: bad_value}}},
    )
    with patch("src.config_loader.load_dotenv"):
        with pytest.raises(ValueError, match=err_pattern):
            load_config(path=path)


@pytest.mark.unit
def test_digest_rubrics_accept_arbitrary_ids_and_names(tmp_path, mock_env_vars):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
channels:
  - id: "@test"
    name: "Test Channel"
settings:
  target_user_id: 123456789
  digest_rubrics:
    min_similarity: 0.41
    items:
      - id: sea_and_resort
        name: "Море и курорт"
        description: "Пляжи, море, курортная инфраструктура"
        emoji: "🌊"
      - id: catch_all
        name: "Остальное"
        description: "Другие важные события"
        fallback: true
"""
    )

    cfg = load_config(str(config_file))

    assert cfg.settings.digest_rubrics.min_similarity == 0.41
    assert [r.id for r in cfg.settings.digest_rubrics.items] == ["sea_and_resort", "catch_all"]
    assert cfg.settings.digest_rubrics.fallback.id == "catch_all"


@pytest.mark.unit
@pytest.mark.parametrize(
    "digest_rubrics,error",
    [
        ({"items": []}, "at least one rubric"),
        (
            {
                "items": [
                    {"id": "a", "name": "A", "description": "A", "fallback": False},
                ]
            },
            "exactly one fallback",
        ),
        (
            {
                "items": [
                    {"id": "a", "name": "A", "description": "A", "fallback": True},
                    {"id": "b", "name": "B", "description": "B", "fallback": True},
                ]
            },
            "exactly one fallback",
        ),
        (
            {
                "items": [
                    {"id": "Bad ID", "name": "Bad", "description": "Bad", "fallback": True},
                ]
            },
            "invalid digest rubric id",
        ),
    ],
)
def test_digest_rubrics_validate_contract(tmp_path, mock_env_vars, digest_rubrics, error):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "channels": [{"id": "@test", "name": "Test Channel"}],
                "settings": {
                    "target_user_id": 123456789,
                    "digest_rubrics": digest_rubrics,
                },
            },
            allow_unicode=True,
        )
    )

    with pytest.raises(ValueError, match=error):
        load_config(str(config_file))


@pytest.mark.unit
def test_publication_editorial_config_defaults(temp_config_file, mock_env_vars):
    """Publication editorial settings default to expected limits."""
    config = load_config(temp_config_file)
    pub_edit = config.settings.publication_editorial
    assert pub_edit.conflict_window_minutes == 90
    assert pub_edit.article_min_words == 800
    assert pub_edit.article_max_words == 2200
    assert pub_edit.article_min_sections == 3
    assert pub_edit.article_max_sections == 8
    assert pub_edit.article_max_direct_quotes == 4

    assert pub_edit.digest_narrative_mode == "deterministic"
    assert pub_edit.digest_narrative_max_cards_per_block == 6
    assert pub_edit.digest_narrative_max_output_tokens == 4096


@pytest.mark.unit
def test_publication_editorial_config_single_call_mode(tmp_path, mock_env_vars):
    """Explicit single_call mode is accepted from YAML."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "channels": [{"id": "@test", "name": "Test Channel"}],
                "settings": {
                    "target_user_id": 123456789,
                    "publication_editorial": {
                        "digest_narrative_mode": "single_call",
                        "digest_narrative_max_cards_per_block": 8,
                        "digest_narrative_max_output_tokens": 2048,
                    },
                },
            },
            allow_unicode=True,
        )
    )
    config = load_config(str(config_file))
    pub_edit = config.settings.publication_editorial
    assert pub_edit.digest_narrative_mode == "single_call"
    assert pub_edit.digest_narrative_max_cards_per_block == 8
    assert pub_edit.digest_narrative_max_output_tokens == 2048


@pytest.mark.unit
@pytest.mark.parametrize(
    "pub_editorial,error",
    [
        ({"conflict_window_minutes": -10}, "must be a positive integer"),
        (
            {"article_min_words": 1500, "article_max_words": 1000},
            "article_min_words cannot be greater",
        ),
        (
            {"article_min_sections": 5, "article_max_sections": 3},
            "article_min_sections cannot be greater",
        ),
        ({"article_max_direct_quotes": -1}, "must be a non-negative integer"),
        (
            {"digest_narrative_mode": "invalid_mode"},
            "digest_narrative_mode must be 'deterministic', 'single_call', or 'journalistic'",
        ),
        ({"digest_narrative_max_cards_per_block": 0}, "must be a positive integer"),
        ({"digest_narrative_max_output_tokens": -100}, "must be a positive integer"),
    ],
)
def test_publication_editorial_config_validation(tmp_path, mock_env_vars, pub_editorial, error):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "channels": [{"id": "@test", "name": "Test Channel"}],
                "settings": {
                    "target_user_id": 123456789,
                    "publication_editorial": pub_editorial,
                },
            },
            allow_unicode=True,
        )
    )

    with pytest.raises(ValueError, match=error):
        load_config(str(config_file))


@pytest.mark.unit
def test_publication_editorial_city_situation_defaults() -> None:
    cfg = PublicationEditorialConfig()
    assert cfg.digest_city_situation_max_items == 7
    assert cfg.digest_city_situation_max_details_per_item == 2
    assert cfg.digest_city_situation_max_positive_items == 2


@pytest.mark.unit
@pytest.mark.parametrize("value", [0, 13])
def test_city_situation_max_items_bounds(value: int) -> None:
    with pytest.raises(ValueError):
        PublicationEditorialConfig(digest_city_situation_max_items=value)


@pytest.mark.unit
@pytest.mark.parametrize("value", [0, 5])
def test_city_situation_max_details_bounds(value: int) -> None:
    with pytest.raises(ValueError):
        PublicationEditorialConfig(digest_city_situation_max_details_per_item=value)


@pytest.mark.unit
@pytest.mark.parametrize("value", [-1, 5])
def test_city_situation_max_positive_items_bounds(value: int) -> None:
    with pytest.raises(ValueError):
        PublicationEditorialConfig(digest_city_situation_max_positive_items=value)
