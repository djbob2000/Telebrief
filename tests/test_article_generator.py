"""Tests for article generator and news-style editorial article prompt."""

# pylint: disable=import-error

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.article_generator import ArticleGenerator
from src.collector import Message
from src.config_loader import Config, Settings


@pytest.mark.unit
def test_article_prompt_template_exists_and_contains_rules():
    """System prompt file must exist and contain core editorial rules."""
    prompt_path = Path("src/prompts/article_news_style.txt")
    assert prompt_path.exists()
    content = prompt_path.read_text(encoding="utf-8")
    assert "{language}" in content
    assert "Бердянск" in content
    assert "Напомним" in content
    assert "##" in content
    assert "pro.berdyansk.biz" in content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_generator_creates_valid_article():
    """ArticleGenerator formats messages, calls AI provider, and parses title/lead/body."""
    settings = Settings(
        schedule_time="09:00",
        timezone="Europe/Kyiv",
        lookback_hours=24,
        openai_model="gpt-5-nano",
        openai_temperature=0.7,
        output_language="Russian",
        target_user_id=123,
    )
    config = Config(
        channels=[],
        settings=settings,
        telegram_api_id=123,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
    )
    logger = MagicMock()

    mock_ai_response = """# В Бердянске ликвидируют последствия перебоев со светом и водой

За прошедшие сутки в Бердянске ключевые коммунальные службы работали в усиленном режиме из-за ночных инцидентов.

## Обстановка в городе и происшествия
По сообщениям жителей, около полуночи в районе Косы были слышны громкие звуки.

## Коммунальная инфраструктура
Как сообщили в аварийной службе, ремонтные бригады восстановили подачу электричества в 12 домах на улице Победы.
"""

    generator = ArticleGenerator(config, logger)
    generator.provider.chat_completion = AsyncMock(return_value=mock_ai_response)

    messages_by_channel = {
        "Бердянск": [
            Message(
                text="В районе Косы слышны громкие звуки",
                sender="Admin",
                timestamp=datetime.now(timezone.utc),
                link="https://t.me/berdiansk_me/100",
                channel_name="Бердянск",
                has_media=False,
                media_type="text",
            )
        ]
    }

    title, lead, body = await generator.generate_article(messages_by_channel)
    assert "В Бердянске ликвидируют последствия" in title
    assert "За прошедшие сутки в Бердянске" in lead
    assert "## Обстановка в городе" in body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_generator_empty_messages_raises():
    """ArticleGenerator raises ValueError when passed an empty message dictionary."""
    settings = Settings(
        schedule_time="09:00",
        timezone="Europe/Kyiv",
        lookback_hours=24,
        openai_model="gpt-5-nano",
        openai_temperature=0.7,
        output_language="Russian",
        target_user_id=123,
    )
    config = Config(
        channels=[],
        settings=settings,
        telegram_api_id=123,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
    )
    logger = MagicMock()
    generator = ArticleGenerator(config, logger)

    with pytest.raises(ValueError, match="No messages provided for article generation"):
        await generator.generate_article({"Бердянск": []})
