"""Tests for article generator and news-style editorial article prompt."""

# pylint: disable=import-error

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.article_generator import ArticleGenerator
from src.collector import Message
from src.config_loader import Config, Settings

_VALID_REGISTRY = json.dumps(
    {
        "claims": [
            {
                "id": "C001",
                "claim": "В районе Косы слышны громкие звуки",
                "status": "attributed",
                "evidence": [{"source_id": "S0001", "quote": "В районе Косы слышны громкие звуки"}],
            }
        ]
    }
)

_VALID_DRAFT = json.dumps(
    {
        "headline": {
            "text": "В Бердянске ликвидируют последствия перебоев со светом и водой",
            "claim_ids": ["C001"],
        },
        "lead": {
            "text": (
                "За прошедшие сутки в Бердянске ключевые коммунальные службы "
                "работали в усиленном режиме из-за ночных инцидентов."
            ),
            "claim_ids": ["C001"],
        },
        "paragraphs": [],
        "sections": [
            {
                "heading": {
                    "text": "Обстановка в городе и происшествия",
                    "claim_ids": ["C001"],
                },
                "paragraphs": [
                    {
                        "text": (
                            "По сообщениям жителей, около полуночи в районе Косы "
                            "были слышны громкие звуки."
                        ),
                        "claim_ids": ["C001"],
                    }
                ],
            }
        ],
    }
)

_VALID_AUDIT = json.dumps(
    {
        "status": "PASS",
        "violations": [],
    }
)

_SIMPLE_REGISTRY = json.dumps(
    {
        "claims": [
            {
                "id": "C001",
                "claim": "Факт события",
                "status": "established",
                "evidence": [{"source_id": "S0001", "quote": "Факт события"}],
            }
        ]
    }
)

_SIMPLE_DRAFT = json.dumps(
    {
        "headline": {
            "text": "Заголовок",
            "claim_ids": ["C001"],
        },
        "lead": {
            "text": "Текст статьи.",
            "claim_ids": ["C001"],
        },
        "paragraphs": [
            {
                "text": "Дополнительный абзац.",
                "claim_ids": ["C001"],
            }
        ],
        "sections": [],
    }
)


def _make_generator() -> ArticleGenerator:
    """Build a generator with the smallest valid test configuration."""
    settings = Settings(
        schedule_time="09:00",
        timezone="Europe/Kiev",
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
    return ArticleGenerator(config, MagicMock())


def _message(text: str, link: str = "https://t.me/news/1") -> Message:
    """Build a minimal source message for article-generator tests."""
    return Message(
        text=text,
        sender="Admin",
        timestamp=datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc),
        link=link,
        channel_name="news",
        has_media=False,
        media_type="",
    )


@pytest.mark.unit
def test_article_skill_template_exists_and_is_loaded_by_generator():
    """Skill file must exist and ArticleGenerator should load it directly."""
    skill_path = Path(".agents/skills/news-style/SKILL.md")
    assert skill_path.exists()
    content = skill_path.read_text(encoding="utf-8")
    assert "news-style" in content
    assert "pro.berdyansk.biz" in content
    assert "attribution" in content.lower()

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
    system_prompt = generator._compose_system_prompt()
    assert "pro.berdyansk.biz" in system_prompt
    assert "Russian" in system_prompt


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

    generator = ArticleGenerator(config, logger)
    generator.provider.chat_completion = AsyncMock(
        side_effect=[_VALID_REGISTRY, _VALID_DRAFT, _VALID_AUDIT]
    )

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
    assert "## Обстановка в городе и происшествия" in body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_generator_retries_after_transient_model_failure():
    """A temporary provider failure is retried before the workflow gives up."""
    generator = _make_generator()
    generator.config.settings.article.generation_retries = 1
    generator.config.settings.article.generation_retry_delay = 0
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            RuntimeError("temporary provider failure"),
            _SIMPLE_REGISTRY,
            _SIMPLE_DRAFT,
            _VALID_AUDIT,
        ]
    )

    title, lead, body = await generator.generate_article({"news": [_message("Факт события")]})

    assert title == "Заголовок"
    assert lead == "Текст статьи."
    assert "Дополнительный абзац." in body
    assert generator.provider.chat_completion.await_count == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_generator_retries_after_empty_model_response():
    """An empty model response is treated as a recoverable generation error."""
    generator = _make_generator()
    generator.config.settings.article.generation_retries = 1
    generator.config.settings.article.generation_retry_delay = 0
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            "",
            _SIMPLE_REGISTRY,
            _SIMPLE_DRAFT,
            _VALID_AUDIT,
        ]
    )

    title, _, body = await generator.generate_article({"news": [_message("Факт события")]})

    assert title == "Заголовок"
    assert "Дополнительный абзац." in body
    assert generator.provider.chat_completion.await_count == 4


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
