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
from src.editorial_writer import ArticleDraft

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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_regression_pipeline_recovers_from_string_elements_and_bad_refs(
    sample_config, mock_logger, mocker
):
    from src.editorial_writer import ArticleDraft, ArticleSection

    analysis_json = json.dumps(
        {
            "cards": [
                {
                    "id": "SC001",
                    "topic": "Электроснабжение",
                    "summary": "Отключения света на АКЗ и в Лисках",
                    "sources": ["S000001", "S000002"],
                    "hard_facts": [
                        "На АКЗ отключилось электричество",
                        "В Лисках также нет света",
                    ],
                    "uncertainties": [{"text": "Сроки подачи не сообщаются"}],
                },
                {
                    "id": "SC002",
                    "topic": "Связь",
                    "summary": "Перебои у операторов",
                    "sources": ["S000001", "S999999"],  # Contains 1 bad ref
                    "community_observations": ["Жители жалуются на мобильную связь"],
                },
            ]
        }
    )

    writer_draft = ArticleDraft(
        headline="Как Бердянск прожил сутки с перебоями света и связи",
        lead="Главными темами дня в городе стали перебои с электричеством и мобильной связью.",
        sections=[
            ArticleSection(
                heading="Перебои со светом и зарядка гаджетов: что происходило в районах",
                paragraphs=[
                    "В течение дня жители АКЗ и Лисок сообщали об отключениях электричества."
                ],
            ),
            ArticleSection(
                heading="Ситуация с мобильной связью",
                paragraphs=["В городских чатах жители также отмечали перебои со связью."],
            ),
        ],
    )

    generator = ArticleGenerator(sample_config, mock_logger)
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            analysis_json,  # Analyzer call
            json.dumps(
                {"status": "PASS", "systemic_problem": False, "issues": []}
            ),  # Fact-check call
        ]
    )

    analyzer_spy = mocker.spy(generator.analyzer, "analyze")
    generator.fallback_builder.build = mocker.MagicMock(wraps=generator.fallback_builder.build)
    generator.writer.write = AsyncMock(return_value=writer_draft)

    messages = {
        "channel_1": [
            Message(
                text="На АКЗ нет света",
                channel_name="channel_1",
                timestamp=datetime.now(timezone.utc),
                sender="user1",
                message_id=1,
                link="https://t.me/c1/1",
                has_media=False,
                media_type="",
            ),
            Message(
                text="В Лисках тоже выключили",
                channel_name="channel_1",
                timestamp=datetime.now(timezone.utc),
                sender="user2",
                message_id=2,
                link="https://t.me/c1/2",
                has_media=False,
                media_type="",
            ),
        ]
    }

    title, lead, body = await generator.generate_article(messages)

    assert title == "Как Бердянск прожил сутки с перебоями света и связи"
    assert "Перебои со светом" in body
    assert generator.fallback_builder.build.call_count == 0
    assert generator.writer.write.call_count == 1
    assert analyzer_spy.call_count == 1
    assert analyzer_spy.call_args.kwargs.get("compact", False) is False

    # Verify S999999 was sanitized out and never passed to writer bundle
    call_args = generator.writer.write.call_args
    passed_analysis, passed_bundle = call_args[0]
    assert "S999999" not in passed_analysis.all_source_refs()
    assert "S999999" not in passed_bundle.records


@pytest.mark.unit
@pytest.mark.asyncio
async def test_writer_diagnostics_capture_exact_bundle_passed_to_writer(
    sample_config, mock_logger, tmp_path
):
    sample_config.settings.article.save_debug_artifacts = True
    sample_config.settings.article.debug_artifact_dir = str(tmp_path)

    analysis_json = json.dumps(
        {
            "cards": [
                {
                    "id": "SC001",
                    "topic": "Электроснабжение",
                    "importance": "high",
                    "summary": "Отключения света",
                    "sources": ["S000001"],
                    "hard_facts": ["На АКЗ отключилось электричество"],
                }
            ]
        }
    )
    generator = ArticleGenerator(sample_config, mock_logger)
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            analysis_json,
            json.dumps({"status": "PASS", "systemic_problem": False, "issues": []}),
        ]
    )
    mock_writer = AsyncMock()
    mock_writer.write.return_value = ArticleDraft(
        headline="Заголовок", lead="Лид", paragraphs=["Абзац"], sections=[]
    )
    generator.writer = mock_writer

    messages = {
        "ch1": [
            Message(
                text="На АКЗ нет света",
                channel_name="ch1",
                timestamp=datetime.now(timezone.utc),
                sender="user1",
                message_id=1,
                link="https://t.me/c1/1",
                has_media=False,
                media_type="",
            ),
            Message(
                text="Другое сообщение без ссылки",
                channel_name="ch1",
                timestamp=datetime.now(timezone.utc),
                sender="user2",
                message_id=2,
                link="https://t.me/c1/2",
                has_media=False,
                media_type="",
            ),
        ]
    }

    await generator.generate_article(messages)

    call_args = mock_writer.write.call_args
    _, passed_bundle = call_args[0]
    assert list(passed_bundle.records.keys()) == ["S000001"]

    writer_bundle_file = tmp_path / "writer_bundle.txt"
    assert writer_bundle_file.exists()
    assert "[S000001]" in writer_bundle_file.read_text(encoding="utf-8")
    assert "[S000002]" not in writer_bundle_file.read_text(encoding="utf-8")

    mock_logger.info.assert_any_call("Editorial analysis selected %d stories:", 1)
    mock_logger.info.assert_any_call("Selected %d source records for writer", 1)
    mock_logger.info.assert_any_call(
        "Drafting article from %d Story Cards / %d source records", 1, 1
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_check_failure_saves_raw_response_and_structured_reason(
    sample_config, mock_logger, tmp_path
):
    sample_config.settings.article.save_debug_artifacts = True
    sample_config.settings.article.debug_artifact_dir = str(tmp_path)

    analysis_json = json.dumps(
        {
            "cards": [
                {
                    "id": "SC001",
                    "topic": "Свет",
                    "importance": "high",
                    "summary": "Отключения света",
                    "sources": ["S000001"],
                    "hard_facts": ["На АКЗ отключилось электричество"],
                }
            ]
        }
    )
    writer_json = json.dumps(
        {
            "headline": "Заголовок",
            "lead": "Лид",
            "paragraphs": ["Абзац"],
            "sections": [],
        }
    )
    generator = ArticleGenerator(sample_config, mock_logger)
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            analysis_json,
            writer_json,
            "not a valid json fact check response",
        ]
    )

    messages = {
        "ch1": [
            Message(
                text="На АКЗ нет света",
                channel_name="ch1",
                timestamp=datetime.now(timezone.utc),
                sender="user1",
                message_id=1,
                link="https://t.me/c1/1",
                has_media=False,
                media_type="",
            )
        ]
    }

    title, _, _ = await generator.generate_article(messages)
    assert title == "Заголовок"

    raw_file = tmp_path / "fact_check_raw.txt"
    failure_file = tmp_path / "fact_check_failure.json"
    assert raw_file.exists()
    assert raw_file.read_text(encoding="utf-8") == "not a valid json fact check response"
    assert failure_file.exists()
    failure_data = json.loads(failure_file.read_text(encoding="utf-8"))
    assert failure_data["stage"] == "json_parse"
    assert failure_data["response_chars"] == len("not a valid json fact check response")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("card_count", [1, 2])
async def test_pipeline_accepts_small_local_story_set_without_inflation(
    card_count, sample_config, mock_logger
):
    """Pipeline smoothly accepts 1 or 2 local story cards without forcing extra stories or falling back."""
    cards = [
        {
            "id": f"SC00{i + 1}",
            "topic": f"Local Topic {i + 1}",
            "importance": "high",
            "summary": f"Summary {i + 1}",
            "hard_facts": [{"text": f"Fact {i + 1}", "source_refs": ["S000001"]}],
        }
        for i in range(card_count)
    ]
    analysis_json = json.dumps({"cards": cards})
    writer_json = json.dumps(
        {
            "headline": "Городской заголовок",
            "lead": "Городской лид.",
            "paragraphs": ["Параграф."],
            "sections": [],
        }
    )
    generator = ArticleGenerator(sample_config, mock_logger)
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            analysis_json,
            writer_json,
            json.dumps({"status": "PASS", "systemic_problem": False, "issues": []}),
        ]
    )

    messages = {
        "ch1": [
            Message(
                text="Сообщение из Бердянска",
                channel_name="ch1",
                timestamp=datetime.now(timezone.utc),
                sender="user1",
                message_id=1,
                link="https://t.me/c1/1",
                has_media=False,
                media_type="",
            )
        ]
    }

    title, lead, body = await generator.generate_article(messages)
    assert title == "Городской заголовок"
    assert lead == "Городской лид."
    assert "Параграф" in body
    assert generator.provider.chat_completion.call_count == 3
