import os
import urllib.parse
from datetime import datetime, timezone
from typing import Iterator
from unittest.mock import MagicMock

DEFAULT_TEST_ENV = {
    "DATABASE_URL": os.environ.get(
        "TELEBRIEF_TEST_DATABASE_URL",
        "postgresql://telebrief:telebrief@localhost:5432/telebrief_test",
    ),
    "TELEGRAM_API_ID": "12345678",
    "TELEGRAM_API_HASH": "test_hash",
    "TELEGRAM_BOT_TOKEN": "123456789:ABC-DEF",
    "OPENAI_API_KEY": "sk-test-key",
    "GEMINI_API_KEY": "test-gemini-key",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "LOG_LEVEL": "INFO",
}

for _key, _val in DEFAULT_TEST_ENV.items():
    if _key not in os.environ:
        os.environ[_key] = _val

import psycopg  # noqa: E402
import pytest  # noqa: E402

from src.config_loader import ChannelConfig, Config, DatabaseConfig, Settings  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def configure_worker_database(request) -> Iterator[str]:
    """Provide an isolated test database URL per xdist worker created from the template."""
    worker_id = "master"
    if hasattr(request.config, "workerinput"):
        worker_id = request.config.workerinput["workerid"]

    base_url = os.environ.get(
        "TELEBRIEF_TEST_DATABASE_URL",
        os.environ.get(
            "DATABASE_URL", "postgresql://telebrief:telebrief@localhost:5432/telebrief_test"
        ),
    )

    if worker_id == "master":
        yield base_url
        return

    parsed = urllib.parse.urlparse(base_url)
    base_db_name = parsed.path.lstrip("/") or "telebrief_test"
    worker_db_name = f"{base_db_name}_{worker_id}"
    admin_url = urllib.parse.urlunparse(parsed._replace(path="/postgres"))
    worker_url = urllib.parse.urlunparse(parsed._replace(path=f"/{worker_db_name}"))

    # Provision worker database from the template
    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {worker_db_name} (FORCE)")
            conn.execute(f"CREATE DATABASE {worker_db_name} TEMPLATE {base_db_name}")
    except Exception as exc:
        import logging

        logging.getLogger("pytest").warning(
            "Failed to provision worker database %s: %s", worker_db_name, exc
        )
        yield base_url
        return

    # Point environment to the worker database
    orig_test_url = os.environ.get("TELEBRIEF_TEST_DATABASE_URL")
    orig_db_url = os.environ.get("DATABASE_URL")
    os.environ["TELEBRIEF_TEST_DATABASE_URL"] = worker_url
    os.environ["DATABASE_URL"] = worker_url

    yield worker_url

    # Teardown
    if orig_test_url is not None:
        os.environ["TELEBRIEF_TEST_DATABASE_URL"] = orig_test_url
    if orig_db_url is not None:
        os.environ["DATABASE_URL"] = orig_db_url

    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {worker_db_name} (FORCE)")
    except Exception:
        pass


@pytest.fixture
def database_config(configure_worker_database: str) -> DatabaseConfig:
    """DatabaseConfig pointing at the persistent PostgreSQL test database."""
    return DatabaseConfig(
        enabled=True,
        url=configure_worker_database,
        min_pool_size=1,
        max_pool_size=4,
        domain_schema="public",
        procrastinate_schema="procrastinate",
    )


@pytest.fixture(autouse=True)
def reset_provider_cascade_state():
    """Reset ProviderCascade global cooldowns and round-robin pointer for each test."""
    from src.ai_providers import ProviderCascade

    ProviderCascade.reset_global_state()
    yield
    ProviderCascade.reset_global_state()


@pytest.fixture(autouse=True)
def reset_telebrief_runtime():
    """Clear the process-local Telebrief runtime around each test.

    Tests that install a runtime (or leak one from a failed initialize) must
    not poison other tests; the registry has no unguarded public reset, so
    this fixture resets the module reference directly on both sides.
    """
    import logging

    from src import runtime

    runtime._runtime = None
    logging.getLogger("telebrief").propagate = True
    yield
    runtime._runtime = None
    logging.getLogger("telebrief").propagate = True


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set up mock environment variables."""
    monkeypatch.setenv("TELEGRAM_API_ID", "12345678")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test_hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:ABC-DEF")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("LOG_LEVEL", "INFO")


@pytest.fixture
def sample_config():
    """Create a sample configuration for testing."""
    channels = [
        ChannelConfig(id="@test_channel", name="Test Channel"),
        ChannelConfig(id="-1001234567890", name="Private Group"),
    ]

    settings = Settings(
        schedule_time="08:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-5-nano",
        openai_temperature=0.7,
        temperature=0.7,
        max_tokens_per_summary=1500,
        use_emojis=True,
        include_statistics=True,
        target_user_id=123456789,
        auto_cleanup_old_digests=True,
        max_messages_per_channel=5000,
        max_prompt_chars=8000,
        api_timeout=30,
        ai_provider="openai",
        ai_model="gpt-5-nano",
        ollama_base_url="http://localhost:11434",
        output_language="Russian",
    )

    config = Config(
        channels=channels,
        settings=settings,
        telegram_api_id=12345678,
        telegram_api_hash="test_hash",
        telegram_bot_token="123456789:ABC-DEF",
        openai_api_key="sk-test-key",
        log_level="INFO",
        anthropic_api_key="",
    )

    return config


@pytest.fixture
def sample_messages():
    """Create sample messages for testing."""
    from src.collector import Message

    return [
        Message(
            text="Test message 1",
            sender="User1",
            timestamp=datetime(2025, 12, 14, 10, 0, 0, tzinfo=timezone.utc),
            link="https://t.me/test/1",
            channel_name="Test Channel",
            has_media=False,
            media_type="",
        ),
        Message(
            text="Test message 2",
            sender="User2",
            timestamp=datetime(2025, 12, 14, 11, 0, 0, tzinfo=timezone.utc),
            link="https://t.me/test/2",
            channel_name="Test Channel",
            has_media=True,
            media_type="Фото",
        ),
        Message(
            text="Test message 3",
            sender="User3",
            timestamp=datetime(2025, 12, 14, 12, 0, 0, tzinfo=timezone.utc),
            link="https://t.me/test/3",
            channel_name="Test Channel",
            has_media=False,
            media_type="",
        ),
    ]


@pytest.fixture
def mock_logger():
    """Create a mock logger."""
    return MagicMock()


@pytest.fixture
def temp_config_file(tmp_path, mock_env_vars):
    """Create a temporary config file."""
    config_content = """
channels:
  - id: "@test_channel"
    name: "Test Channel"
  - id: -1001234567890
    name: "Private Group"

settings:
  schedule_time: "08:00"
  timezone: "UTC"
  lookback_hours: 24
  openai_model: "gpt-5-nano"
  openai_temperature: 0.7
  max_tokens_per_summary: 1500
  use_emojis: true
  include_statistics: true
  target_user_id: 123456789
  auto_cleanup_old_digests: true
  max_messages_per_channel: 5000
  api_timeout: 30
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)
    return str(config_file)
