"""Tests for the primary src.config package entry points."""

from src.config import Config, load_config


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
  target_user_id: 123456789
  schedule_time: "20:00"
  timezone: "UTC"
  lookback_hours: 24
  openai_model: "gpt-5-nano"
  openai_temperature: 0.7
""")

    cfg = load_config(str(config_file))
    assert isinstance(cfg, Config)
    assert cfg.settings.schedule_time == "20:00"
    assert cfg.settings.target_user_id == 123456789
    assert len(cfg.channels) == 1
    assert cfg.channels[0].name == "Test"
