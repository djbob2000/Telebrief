"""Integration tests for main and degraded editorial generation paths."""

import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.article_generator import ArticleGenerator
from src.collector import Message
from src.config_loader import Config, Settings


def _generator() -> ArticleGenerator:
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
    generator = ArticleGenerator(config, MagicMock())
    generator.config.settings.article.generation_retries = 0
    return generator


def _message(text: str, message_id: int = 1) -> Message:
    return Message(
        text=text,
        sender="Источник",
        timestamp=datetime(2026, 8, 15, 10, message_id, tzinfo=timezone.utc),
        link=f"https://t.me/source/{message_id}",
        channel_name="Source",
        has_media=False,
        media_type="",
        message_id=message_id,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writer_article_survives_fact_checker_failure():
    generator = _generator()
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "cards": [
                        {
                            "id": "SC001",
                            "topic": "Вода",
                            "importance": "high",
                            "summary": "Воду отключили.",
                            "hard_facts": [
                                {
                                    "text": "Воду отключили.",
                                    "source_refs": ["S000001"],
                                    "status": "established",
                                }
                            ],
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "headline": "Воду отключили",
                    "lead": "В городе сообщили об отключении воды.",
                    "paragraphs": ["Подробности уточняются."],
                    "sections": [],
                }
            ),
            RuntimeError("audit timeout"),
        ]
    )

    title, lead, body = await generator.generate_article({"Source": [_message("Воду отключили.")]})

    assert title == "Воду отключили"
    assert lead.startswith("В городе")
    assert "Подробности" in body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analysis_failure_uses_thematic_fallback_not_raw_latest_messages():
    generator = _generator()
    generator.provider.chat_completion = AsyncMock(side_effect=RuntimeError("provider down"))
    messages = [
        _message("Курс доллара 41.20", 1),
        _message("На Колонии нет света", 2),
        _message("Реклама: куплю квартиру", 3),
    ]

    title, lead, body = await generator.generate_article({"Source": messages})

    assert title == "Что происходило в городе за сутки"
    assert lead
    assert "Жители сообщали о перебоях с электроснабжением" in body
    assert "На Колонии нет света" not in body
    assert "Курс доллара" not in body
    assert "Реклама" not in body
    assert "последние сообщения" not in body.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fallback_log_identifies_degraded_editorial_stage(caplog):
    generator = _generator()
    generator.logger = logging.getLogger("telebrief.test.editorial")
    generator.provider.chat_completion = AsyncMock(side_effect=RuntimeError("provider down"))

    with caplog.at_level(logging.WARNING, logger="telebrief.test.editorial"):
        await generator.generate_article({"Source": [_message("На Колонии нет света")]})

    assert "Editorial pipeline entered degraded path" in caplog.text
    assert "editorial analysis unavailable" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_systemic_audit_failure_falls_back_after_one_regeneration():
    generator = _generator()
    card_response = json.dumps(
        {
            "cards": [
                {
                    "id": "SC001",
                    "topic": "Вода",
                    "importance": "high",
                    "summary": "Воду отключили.",
                    "hard_facts": [
                        {
                            "text": "Воду отключили.",
                            "source_refs": ["S000001"],
                            "status": "established",
                        }
                    ],
                }
            ]
        }
    )
    draft_response = json.dumps(
        {
            "headline": "Воду отключили",
            "lead": "В городе сообщили об отключении воды.",
            "paragraphs": ["Подробности уточняются."],
            "sections": [],
        }
    )
    systemic_fix = json.dumps(
        {
            "status": "FIX",
            "systemic_problem": True,
            "issues": [
                {
                    "unit_id": "LEAD",
                    "code": "unsupported_cause",
                    "original_excerpt": "В городе сообщили об отключении воды.",
                    "reason": "Systemic unsupported framing.",
                    "suggested_direction": "Fallback.",
                    "source_refs": [],
                    "severity": "fix",
                }
            ],
        }
    )
    generator.provider.chat_completion = AsyncMock(
        side_effect=[card_response, draft_response, systemic_fix, draft_response, systemic_fix]
    )

    title, _, body = await generator.generate_article({"Source": [_message("Воду отключили.")]})

    assert title == "Что происходило в городе за сутки"
    assert "Жители сообщали о перебоях с водоснабжением" in body
