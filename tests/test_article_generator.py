"""Tests for article generator and news-style editorial article prompt."""

# pylint: disable=import-error

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.article_generator import ArticleGenerator, UnsafeDraftError
from src.collector import Message
from src.config_loader import Config, Settings
from src.editorial_analysis import ContextSizeRejectedError, EditorialAnalysisError
from src.editorial_audit import (
    AuditIssue,
    FactCheckResult,
    FactCheckUnavailableError,
)
from src.editorial_models import EditorialAnalysis, PreparedBundle, SourceRecord, StoryCard
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
    """A primary provider failure is recovered via the provider cascade."""
    from src.ai_providers import ProviderCascade

    generator = _make_generator()
    primary = MagicMock()
    primary.chat_completion = AsyncMock(side_effect=RuntimeError("temporary provider failure"))
    backup = MagicMock()
    backup.chat_completion = AsyncMock(
        side_effect=[
            _SIMPLE_REGISTRY,
            _SIMPLE_DRAFT,
            _VALID_AUDIT,
        ]
    )
    generator.provider = ProviderCascade(
        [("primary", primary), ("backup", backup)], generator.logger
    )
    generator.analyzer.provider = generator.provider
    generator.writer.provider = generator.provider
    generator.fact_checker.provider = generator.provider

    title, lead, body = await generator.generate_article({"news": [_message("Факт события")]})

    assert title == "Заголовок"
    assert lead == "Текст статьи."
    assert "Дополнительный абзац." in body


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
            "topic": f"Городская тема {i + 1}",
            "importance": "high",
            "summary": f"Сводка по теме {i + 1}",
            "hard_facts": [{"text": f"Фактическое описание {i + 1}", "source_refs": ["S000001"]}],
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_handles_valid_zero_story_cards_as_no_substantive_outcome(
    sample_config, mock_logger
):
    generator = ArticleGenerator(sample_config, mock_logger)
    generator.provider.chat_completion = AsyncMock(return_value=json.dumps({"cards": []}))
    generator.fallback_builder.build = MagicMock()
    generator.writer.write = AsyncMock()

    messages = {
        "ch1": [
            Message(
                text="Сообщение",
                channel_name="ch1",
                timestamp=datetime.now(timezone.utc),
                sender="user",
                message_id=1,
                link="https://t.me/c/1",
                has_media=False,
                media_type="",
            )
        ]
    }

    from src.article_generator import NoSubstantiveEditorialError

    with pytest.raises(NoSubstantiveEditorialError):
        await generator.generate_article(messages)

    assert generator.provider.chat_completion.call_count == 1
    generator.writer.write.assert_not_called()
    generator.fallback_builder.build.assert_not_called()
    for call in mock_logger.warning.call_args_list:
        assert "Editorial analysis unavailable" not in str(call)
        assert "attempt 1 failed" not in str(call)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_check_lifecycle_artifacts_initial_pass(sample_config, mock_logger, tmp_path):
    sample_config.settings.article.save_debug_artifacts = True
    sample_config.settings.article.debug_artifact_dir = str(tmp_path)
    generator = ArticleGenerator(sample_config, mock_logger)
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "cards": [
                        {
                            "id": "SC001",
                            "topic": "Тема",
                            "importance": "high",
                            "summary": "Сводка",
                            "hard_facts": [{"text": "Факты", "source_refs": ["S000001"]}],
                        }
                    ]
                }
            ),
            json.dumps(
                {"headline": "Заголовок", "lead": "Лид", "paragraphs": ["Параграф"], "sections": []}
            ),
            json.dumps({"status": "PASS", "systemic_problem": False, "issues": []}),
        ]
    )
    messages = {
        "ch1": [
            Message(
                text="Сообщение",
                channel_name="ch1",
                timestamp=datetime.now(timezone.utc),
                sender="u",
                message_id=1,
                link="l",
                has_media=False,
                media_type="",
            )
        ]
    }

    await generator.generate_article(messages)

    initial_data = json.loads((tmp_path / "fact_check_initial.json").read_text(encoding="utf-8"))
    final_data = json.loads((tmp_path / "fact_check_final.json").read_text(encoding="utf-8"))
    fact_check_data = json.loads((tmp_path / "fact_check.json").read_text(encoding="utf-8"))
    assert initial_data["status"] == "PASS"
    assert final_data["status"] == "PASS"
    assert fact_check_data == final_data


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_check_lifecycle_artifacts_fix_repaired_to_pass(
    sample_config, mock_logger, tmp_path
):
    sample_config.settings.article.save_debug_artifacts = True
    sample_config.settings.article.debug_artifact_dir = str(tmp_path)
    generator = ArticleGenerator(sample_config, mock_logger)
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "cards": [
                        {
                            "id": "SC001",
                            "topic": "Тема",
                            "importance": "high",
                            "summary": "Сводка",
                            "hard_facts": [{"text": "Факты", "source_refs": ["S000001"]}],
                        }
                    ]
                }
            ),
            json.dumps(
                {"headline": "Заголовок", "lead": "Лид", "paragraphs": ["Параграф"], "sections": []}
            ),
            json.dumps(
                {
                    "status": "FIX",
                    "systemic_problem": False,
                    "issues": [
                        {
                            "unit_id": "P001",
                            "severity": "fix",
                            "code": "unverified",
                            "original_excerpt": "Параграф",
                            "reason": "Неподтвержденная формулировка",
                            "suggested_direction": "Использовать атрибуцию",
                            "source_refs": ["S000001"],
                        }
                    ],
                }
            ),
            json.dumps({"replacements": {"P001": "Отремонтированный параграф"}}),
            json.dumps({"status": "PASS", "systemic_problem": False, "issues": []}),
        ]
    )
    messages = {
        "ch1": [
            Message(
                text="Сообщение",
                channel_name="ch1",
                timestamp=datetime.now(timezone.utc),
                sender="u",
                message_id=1,
                link="l",
                has_media=False,
                media_type="",
            )
        ]
    }

    _, _, body = await generator.generate_article(messages)

    assert "Отремонтированный параграф" in body
    initial_data = json.loads((tmp_path / "fact_check_initial.json").read_text(encoding="utf-8"))
    final_data = json.loads((tmp_path / "fact_check_final.json").read_text(encoding="utf-8"))
    fact_check_data = json.loads((tmp_path / "fact_check.json").read_text(encoding="utf-8"))
    assert initial_data["status"] == "FIX"
    assert final_data["status"] == "PASS"
    assert fact_check_data == final_data


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_check_lifecycle_artifacts_systemic_regeneration(
    sample_config, mock_logger, tmp_path
):
    sample_config.settings.article.save_debug_artifacts = True
    sample_config.settings.article.debug_artifact_dir = str(tmp_path)
    generator = ArticleGenerator(sample_config, mock_logger)
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "cards": [
                        {
                            "id": "SC001",
                            "topic": "Тема",
                            "importance": "high",
                            "summary": "Сводка",
                            "hard_facts": [{"text": "Факты", "source_refs": ["S000001"]}],
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "headline": "Заголовок1",
                    "lead": "Лид1",
                    "paragraphs": ["Параграф1"],
                    "sections": [],
                }
            ),
            json.dumps(
                {
                    "status": "FIX",
                    "systemic_problem": True,
                    "issues": [
                        {
                            "unit_id": "P001",
                            "severity": "fix",
                            "code": "systemic",
                            "original_excerpt": "Параграф1",
                            "reason": "Системная структурная ошибка",
                            "suggested_direction": "Сгенерировать заново",
                            "source_refs": ["S000001"],
                        }
                    ],
                }
            ),
            json.dumps({"replacements": {"P001": "Параграф1"}}),
            json.dumps(
                {
                    "status": "FIX",
                    "systemic_problem": True,
                    "issues": [
                        {
                            "unit_id": "P001",
                            "severity": "fix",
                            "code": "systemic",
                            "original_excerpt": "Параграф1",
                            "reason": "Системная структурная ошибка",
                            "suggested_direction": "Сгенерировать заново",
                            "source_refs": ["S000001"],
                        }
                    ],
                }
            ),
            json.dumps({"replacements": {"P001": "Параграф1"}}),
            json.dumps(
                {
                    "status": "FIX",
                    "systemic_problem": True,
                    "issues": [
                        {
                            "unit_id": "P001",
                            "severity": "fix",
                            "code": "systemic",
                            "original_excerpt": "Параграф1",
                            "reason": "Системная структурная ошибка",
                            "suggested_direction": "Сгенерировать заново",
                            "source_refs": ["S000001"],
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "headline": "Заголовок2",
                    "lead": "Лид2",
                    "paragraphs": ["Параграф2"],
                    "sections": [],
                }
            ),
            json.dumps({"status": "PASS", "systemic_problem": False, "issues": []}),
        ]
    )
    messages = {
        "ch1": [
            Message(
                text="Сообщение",
                channel_name="ch1",
                timestamp=datetime.now(timezone.utc),
                sender="u",
                message_id=1,
                link="l",
                has_media=False,
                media_type="",
            )
        ]
    }

    await generator.generate_article(messages)

    initial_data = json.loads((tmp_path / "fact_check_initial.json").read_text(encoding="utf-8"))
    final_data = json.loads((tmp_path / "fact_check_final.json").read_text(encoding="utf-8"))
    assert initial_data["status"] == "FIX"
    assert initial_data["systemic_problem"] is True
    assert final_data["status"] == "PASS"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_check_lifecycle_keeps_last_parsed_result_when_recheck_unavailable(
    sample_config, mock_logger, tmp_path
):
    sample_config.settings.article.save_debug_artifacts = True
    sample_config.settings.article.debug_artifact_dir = str(tmp_path)
    generator = ArticleGenerator(sample_config, mock_logger)
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "cards": [
                        {
                            "id": "SC001",
                            "topic": "Тема",
                            "importance": "high",
                            "summary": "Сводка",
                            "hard_facts": [{"text": "Факты", "source_refs": ["S000001"]}],
                        }
                    ]
                }
            ),
            json.dumps(
                {"headline": "Заголовок", "lead": "Лид", "paragraphs": ["Параграф"], "sections": []}
            ),
            json.dumps(
                {
                    "status": "FIX",
                    "systemic_problem": False,
                    "issues": [
                        {
                            "unit_id": "P001",
                            "severity": "fix",
                            "code": "unverified",
                            "original_excerpt": "Параграф",
                            "reason": "Неподтвержденная формулировка",
                            "suggested_direction": "Использовать атрибуцию",
                            "source_refs": ["S000001"],
                        }
                    ],
                }
            ),
            json.dumps({"replacements": {"P001": "Отремонтированный параграф"}}),
            "invalid json on recheck",
        ]
    )
    messages = {
        "ch1": [
            Message(
                text="Сообщение",
                channel_name="ch1",
                timestamp=datetime.now(timezone.utc),
                sender="u",
                message_id=1,
                link="l",
                has_media=False,
                media_type="",
            )
        ]
    }

    await generator.generate_article(messages)

    initial_data = json.loads((tmp_path / "fact_check_initial.json").read_text(encoding="utf-8"))
    final_data = json.loads((tmp_path / "fact_check_final.json").read_text(encoding="utf-8"))
    failure_data = json.loads((tmp_path / "fact_check_failure.json").read_text(encoding="utf-8"))
    assert initial_data["status"] == "FIX"
    assert final_data["status"] == "FIX"
    assert failure_data["stage"] == "json_parse"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_article_clears_stale_failure_on_successful_pass(
    sample_config, mock_logger, tmp_path
):
    sample_config.settings.article.save_debug_artifacts = True
    sample_config.settings.article.debug_artifact_dir = str(tmp_path)

    # Pre-create stale failure artifact
    stale_failure = tmp_path / "fact_check_failure.json"
    stale_failure.write_text(json.dumps({"stage": "old", "error": "old"}), encoding="utf-8")

    generator = ArticleGenerator(sample_config, mock_logger)
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "cards": [
                        {
                            "id": "SC001",
                            "topic": "Тема",
                            "importance": "high",
                            "summary": "Сводка",
                            "hard_facts": [{"text": "Факты", "source_refs": ["S000001"]}],
                        }
                    ]
                }
            ),
            json.dumps(
                {"headline": "Заголовок", "lead": "Лид", "paragraphs": ["Параграф"], "sections": []}
            ),
            json.dumps({"status": "PASS", "systemic_problem": False, "issues": []}),
        ]
    )
    messages = {
        "ch1": [
            Message(
                text="Сообщение",
                channel_name="ch1",
                timestamp=datetime.now(timezone.utc),
                sender="u",
                message_id=1,
                link="l",
                has_media=False,
                media_type="",
            )
        ]
    }

    await generator.generate_article(messages)

    assert not stale_failure.exists()
    assert (tmp_path / "fact_check_initial.json").exists()
    assert (tmp_path / "fact_check_final.json").exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_article_clears_stale_pass_on_audit_failure(
    sample_config, mock_logger, tmp_path
):
    sample_config.settings.article.save_debug_artifacts = True
    sample_config.settings.article.debug_artifact_dir = str(tmp_path)

    # Pre-create stale initial and final artifacts
    stale_initial = tmp_path / "fact_check_initial.json"
    stale_final = tmp_path / "fact_check_final.json"
    stale_initial.write_text(json.dumps({"status": "PASS", "issues": []}), encoding="utf-8")
    stale_final.write_text(json.dumps({"status": "PASS", "issues": []}), encoding="utf-8")

    generator = ArticleGenerator(sample_config, mock_logger)
    generator.provider.chat_completion = AsyncMock(
        side_effect=[
            json.dumps(
                {
                    "cards": [
                        {
                            "id": "SC001",
                            "topic": "Тема",
                            "importance": "high",
                            "summary": "Сводка",
                            "hard_facts": [{"text": "Факты", "source_refs": ["S000001"]}],
                        }
                    ]
                }
            ),
            json.dumps(
                {"headline": "Заголовок", "lead": "Лид", "paragraphs": ["Параграф"], "sections": []}
            ),
            "invalid json on initial audit",
        ]
    )
    messages = {
        "ch1": [
            Message(
                text="В городе отключили свет",
                channel_name="ch1",
                timestamp=datetime.now(timezone.utc),
                sender="u",
                message_id=1,
                link="l",
                has_media=False,
                media_type="",
            )
        ]
    }

    await generator.generate_article(messages)

    assert not stale_initial.exists()
    assert not stale_final.exists()
    assert (tmp_path / "fact_check_failure.json").exists()


@pytest.mark.unit
def test_enforce_publication_gate_raises_unsafe_on_blocking_fix(sample_config, mock_logger):
    from src.article_generator import UnsafeDraftError
    from src.editorial_audit import AuditIssue, FactCheckResult
    from src.editorial_writer import ArticleDraft, ArticleSection

    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft(
        headline="Заголовок",
        lead="Лид",
        paragraphs=[],
        sections=[ArticleSection("Глава", ["Текст"])],
    )
    result = FactCheckResult(
        status="FIX",
        systemic_problem=False,
        issues=[
            AuditIssue(
                unit_id="H001",
                severity="fix",
                code="unsupported_casualty",
                original_excerpt="Глава",
                reason="Unsupported casualty in heading",
                suggested_direction="Fix heading",
                source_refs=[],
                publication_blocking=True,
            )
        ],
    )

    with pytest.raises(UnsafeDraftError, match="unresolved publication-blocking FIX remains"):
        generator._enforce_publication_gate(draft, result)


@pytest.mark.unit
def test_enforce_publication_gate_allows_non_blocking_fix(sample_config, mock_logger):
    from src.editorial_audit import AuditIssue, FactCheckResult
    from src.editorial_writer import ArticleDraft, ArticleSection

    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft(
        headline="Заголовок",
        lead="Лид",
        paragraphs=[],
        sections=[ArticleSection("Глава", ["Текст"])],
    )
    result = FactCheckResult(
        status="FIX",
        systemic_problem=False,
        issues=[
            AuditIssue(
                unit_id="H001",
                severity="fix",
                code="unsupported_scale",
                original_excerpt="Глава",
                reason="Soft scale overstatement",
                suggested_direction="Fix heading",
                source_refs=[],
                publication_blocking=False,
            )
        ],
    )

    out = generator._enforce_publication_gate(draft, result)
    assert out == draft


@pytest.mark.unit
def test_article_generator_loads_city_context_and_handles_missing_file(sample_config, mock_logger):
    # Default initialization loads checked-in profile
    generator = ArticleGenerator(sample_config, mock_logger)
    assert generator.city_context_resolver is not None
    assert generator.story_context_enricher is not None

    # When profile path is invalid, fails open gracefully
    sample_config.settings.article.city_profile_path = "non_existent_profile.yaml"
    generator_fallback = ArticleGenerator(sample_config, mock_logger)
    assert generator_fallback.city_context_resolver is None
    assert generator_fallback.story_context_enricher is None


@pytest.mark.unit
def test_article_generator_propagates_output_language(sample_config, mock_logger):
    sample_config.settings.output_language = "Russian"
    generator = ArticleGenerator(sample_config, mock_logger)
    assert generator.analyzer.output_language == "Russian"
    assert generator.writer.output_language == "Russian"
    assert generator.fact_checker.output_language == "Russian"
    assert generator.fallback_renderer.output_language == "Russian"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_article_generator_wrong_language_writer_falls_back_to_story_cards(
    sample_config, mock_logger
):
    from unittest.mock import AsyncMock

    from src.editorial_models import EditorialAnalysis, StoryCard, StoryElement

    sample_config.settings.output_language = "Russian"
    generator = ArticleGenerator(sample_config, mock_logger)

    card = StoryCard(
        id="SC001",
        topic="Электроснабжение",
        importance="high",
        summary="В городе наблюдаются проблемы со светом.",
        hard_facts=[
            StoryElement(
                text="Жители сообщали об отключении электроснабжения на АКЗ.",
                source_refs=["S000001"],
                status="attributed",
                attribution="Жители",
            )
        ],
    )
    generator._analyze = AsyncMock(return_value=EditorialAnalysis(cards=[card]))
    english_draft_text = json.dumps(
        {
            "headline": "Blackout in Berdyansk",
            "lead": "Residents in several districts report power outages across the city.",
            "sections": [
                {
                    "heading": "Power outage",
                    "paragraphs": ["Residents reported no electricity."],
                }
            ],
        }
    )
    generator.writer.provider.chat_completion = AsyncMock(return_value=english_draft_text)

    msg = Message(
        text="На АКЗ нет света",
        sender="u1",
        timestamp=datetime.now(timezone.utc),
        link="",
        channel_name="ch1",
        has_media=False,
        media_type="",
        message_id=1,
    )
    title, lead, body = await generator.generate_article({"ch1": [msg]})

    assert "Blackout" not in title
    assert "Blackout" not in body
    assert "Что происходило в городе за сутки" in title or "Электроснабжение" in body
    assert "Жители сообщали об отключении электроснабжения на АКЗ." in body


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyze_token_budget_triggers_compact_full_bundle(sample_config, mock_logger):
    from src.ai_providers import ProviderSlotFailure
    from src.editorial_models import EditorialAnalysis, StoryCard

    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(id="SC001", topic="Тема", importance="high", summary="Сводка")
    success_analysis = EditorialAnalysis(cards=[card])

    slot1 = ProviderSlotFailure(
        slot="primary", kind="token_budget", exception_type="TokenBudgetExhaustedError"
    )
    tb_error = EditorialAnalysisError("token budget exhausted")
    tb_error.stage = "provider_call"
    tb_error.reason = "token_budget"
    tb_error.failure_kinds = ("token_budget",)
    tb_error.slot_failures = (slot1,)

    generator.analyzer.analyze = AsyncMock(side_effect=[tb_error, success_analysis])
    generator.analyzer.analyze_batched = AsyncMock()

    bundle = PreparedBundle(
        records={}, prompt_text="small prompt", total_messages=10, candidate_count=10
    )
    result = await generator._analyze(bundle)

    assert result == success_analysis
    assert generator.analyzer.analyze.await_count == 2
    # First call compact=False, second call compact=True
    assert generator.analyzer.analyze.await_args_list[0].kwargs == {"compact": False}
    assert generator.analyzer.analyze.await_args_list[1].kwargs == {"compact": True}
    generator.analyzer.analyze_batched.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyze_token_budget_then_context_size_triggers_batched_compact(
    sample_config, mock_logger
):
    from src.ai_providers import ProviderSlotFailure
    from src.editorial_models import EditorialAnalysis, StoryCard

    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(id="SC001", topic="Тема", importance="high", summary="Сводка")
    success_analysis = EditorialAnalysis(cards=[card])

    tb_error = EditorialAnalysisError("token budget")
    tb_error.stage = "provider_call"
    tb_error.reason = "token_budget"
    tb_error.failure_kinds = ("token_budget",)
    tb_error.slot_failures = (
        ProviderSlotFailure("primary", "token_budget", "TokenBudgetExhaustedError"),
    )

    cs_error = ContextSizeRejectedError("context size")
    cs_error.stage = "provider_call"
    cs_error.reason = "context_size"
    cs_error.failure_kinds = ("context_size",)
    cs_error.slot_failures = (ProviderSlotFailure("primary", "context_size", "BadRequestError"),)

    generator.analyzer.analyze = AsyncMock(side_effect=[tb_error, cs_error])
    generator.analyzer.analyze_batched = AsyncMock(return_value=success_analysis)

    bundle = PreparedBundle(
        records={}, prompt_text="small prompt", total_messages=10, candidate_count=10
    )
    result = await generator._analyze(bundle)

    assert result == success_analysis
    assert generator.analyzer.analyze.await_count == 2
    generator.analyzer.analyze_batched.assert_awaited_once_with(bundle, compact=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyze_context_size_or_mixed_triggers_batched_normal(sample_config, mock_logger):
    from src.ai_providers import ProviderSlotFailure
    from src.editorial_models import EditorialAnalysis, StoryCard

    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(id="SC001", topic="Тема", importance="high", summary="Сводка")
    success_analysis = EditorialAnalysis(cards=[card])

    # Mixed failure: primary timeout + backup context_size
    cs_error = ContextSizeRejectedError("context size")
    cs_error.stage = "provider_call"
    cs_error.reason = "context_size"
    cs_error.failure_kinds = ("timeout", "context_size")
    cs_error.slot_failures = (
        ProviderSlotFailure("primary", "timeout", "TimeoutError"),
        ProviderSlotFailure("backup", "context_size", "BadRequestError"),
    )

    generator.analyzer.analyze = AsyncMock(side_effect=cs_error)
    generator.analyzer.analyze_batched = AsyncMock(return_value=success_analysis)

    bundle = PreparedBundle(
        records={}, prompt_text="small prompt", total_messages=10, candidate_count=10
    )
    result = await generator._analyze(bundle)

    assert result == success_analysis
    generator.analyzer.analyze.assert_awaited_once_with(bundle, compact=False)
    generator.analyzer.analyze_batched.assert_awaited_once_with(bundle, compact=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyze_pure_outage_fails_fast_without_batching(sample_config, mock_logger):
    from src.ai_providers import ProviderSlotFailure

    generator = ArticleGenerator(sample_config, mock_logger)

    outage_error = EditorialAnalysisError("pure outage")
    outage_error.stage = "provider_call"
    outage_error.reason = "auth,server"
    outage_error.failure_kinds = ("auth", "server")
    outage_error.slot_failures = (
        ProviderSlotFailure("primary", "auth", "AuthenticationError"),
        ProviderSlotFailure("backup", "server", "InternalServerError"),
    )

    generator.analyzer.analyze = AsyncMock(side_effect=outage_error)
    generator.analyzer.analyze_batched = AsyncMock()

    bundle = PreparedBundle(records={}, prompt_text="prompt", total_messages=10, candidate_count=10)
    with pytest.raises(EditorialAnalysisError):
        await generator._analyze(bundle)

    generator.analyzer.analyze.assert_awaited_once_with(bundle, compact=False)
    generator.analyzer.analyze_batched.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyze_other_large_bundle_triggers_batched_rescue(sample_config, mock_logger):
    from src.ai_providers import ProviderSlotFailure
    from src.editorial_models import EditorialAnalysis, StoryCard

    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(id="SC001", topic="Тема", importance="high", summary="Сводка")
    success_analysis = EditorialAnalysis(cards=[card])

    other_error = EditorialAnalysisError("RuntimeError")
    other_error.stage = "provider_call"
    other_error.reason = "RuntimeError"
    other_error.failure_kinds = ("other",)
    other_error.slot_failures = (ProviderSlotFailure("primary", "other", "RuntimeError"),)

    generator.analyzer.analyze = AsyncMock(side_effect=other_error)
    generator.analyzer.analyze_batched = AsyncMock(return_value=success_analysis)

    # Large bundle: 100 candidate messages
    bundle = PreparedBundle(
        records={}, prompt_text="x" * 100, total_messages=100, candidate_count=100
    )
    result = await generator._analyze(bundle)

    assert result == success_analysis
    generator.analyzer.analyze.assert_awaited_once_with(bundle, compact=False)
    generator.analyzer.analyze_batched.assert_awaited_once_with(bundle, compact=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyze_other_small_bundle_fails_fast_without_batching(sample_config, mock_logger):
    from src.ai_providers import ProviderSlotFailure

    generator = ArticleGenerator(sample_config, mock_logger)

    other_error = EditorialAnalysisError("RuntimeError")
    other_error.stage = "provider_call"
    other_error.reason = "RuntimeError"
    other_error.failure_kinds = ("other",)
    other_error.slot_failures = (ProviderSlotFailure("primary", "other", "RuntimeError"),)

    generator.analyzer.analyze = AsyncMock(side_effect=other_error)
    generator.analyzer.analyze_batched = AsyncMock()

    # Small bundle: 10 candidate messages, 100 chars
    bundle = PreparedBundle(
        records={}, prompt_text="x" * 100, total_messages=10, candidate_count=10
    )
    with pytest.raises(EditorialAnalysisError):
        await generator._analyze(bundle)

    generator.analyzer.analyze.assert_awaited_once_with(bundle, compact=False)
    generator.analyzer.analyze_batched.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parse_error_on_large_bundle_uses_compact_not_batching(sample_config, mock_logger):
    """Substantive response with JSON/schema errors triggers compact retry, NEVER batching."""
    from src.editorial_models import EditorialAnalysis, StoryCard

    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(id="SC001", topic="Тема", importance="high", summary="Сводка")
    success_compact = EditorialAnalysis(cards=[card])

    parse_error = EditorialAnalysisError("malformed json")
    parse_error.stage = "json_parse"
    parse_error.reason = "Expecting value"

    generator.analyzer.analyze = AsyncMock(side_effect=[parse_error, success_compact])
    generator.analyzer.analyze_batched = AsyncMock()

    # Large bundle: 100 candidate messages
    bundle = PreparedBundle(
        records={}, prompt_text="x" * 60_000, total_messages=100, candidate_count=100
    )
    result = await generator._analyze(bundle)

    assert result == success_compact
    assert generator.analyzer.analyze.await_count == 2
    assert generator.analyzer.analyze.await_args_list[0].kwargs == {"compact": False}
    assert generator.analyzer.analyze.await_args_list[1].kwargs == {"compact": True}
    generator.analyzer.analyze_batched.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_state_machine_each_call_shape_attempted_at_most_once(sample_config, mock_logger):
    """Verify state machine invariant: every (shape, compact) call is made at most once."""
    from src.ai_providers import ProviderSlotFailure

    generator = ArticleGenerator(sample_config, mock_logger)

    # Sequence: Full Normal (token_budget) -> Full Compact (context_size) -> Batched Compact (fails) -> Fallback
    tb_error = EditorialAnalysisError("token budget")
    tb_error.stage = "provider_call"
    tb_error.reason = "token_budget"
    tb_error.failure_kinds = ("token_budget",)
    tb_error.slot_failures = (
        ProviderSlotFailure("primary", "token_budget", "TokenBudgetExhaustedError"),
    )

    cs_error = ContextSizeRejectedError("context size")
    cs_error.stage = "provider_call"
    cs_error.reason = "context_size"
    cs_error.failure_kinds = ("context_size",)
    cs_error.slot_failures = (ProviderSlotFailure("primary", "context_size", "BadRequestError"),)

    batched_error = ContextSizeRejectedError("batch also exceeded")
    batched_error.stage = "provider_call"
    batched_error.reason = "context_size"
    batched_error.failure_kinds = ("context_size",)

    calls: list[tuple[str, bool]] = []

    async def mock_analyze(b, *, compact=False):
        calls.append(("full", compact))
        if not compact:
            raise tb_error
        raise cs_error

    async def mock_analyze_batched(b, *, compact=False):
        calls.append(("batched", compact))
        raise batched_error

    generator.analyzer.analyze = AsyncMock(side_effect=mock_analyze)
    generator.analyzer.analyze_batched = AsyncMock(side_effect=mock_analyze_batched)

    bundle = PreparedBundle(records={}, prompt_text="prompt", total_messages=10, candidate_count=10)

    with pytest.raises(EditorialAnalysisError):
        await generator._analyze(bundle)

    # Invariant: No duplicate (shape, compact) calls
    assert len(calls) == len(set(calls))
    assert calls == [("full", False), ("full", True), ("batched", True)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyze_mixed_token_budget_and_context_size_executes_full_compact_first(
    sample_config, mock_logger
):
    """Mixed cascade failure with {token_budget, context_size} must execute Full Compact first."""
    from src.editorial_models import EditorialAnalysis, StoryCard

    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(id="SC001", topic="Тема", importance="high", summary="Сводка")
    success_compact = EditorialAnalysis(cards=[card])

    mixed_error = EditorialAnalysisError("mixed failure")
    mixed_error.stage = "provider_call"
    mixed_error.reason = "token_budget,context_size"
    mixed_error.failure_kinds = ("token_budget", "context_size")

    generator.analyzer.analyze = AsyncMock(side_effect=[mixed_error, success_compact])
    generator.analyzer.analyze_batched = AsyncMock()

    bundle = PreparedBundle(
        records={}, prompt_text="x" * 60_000, total_messages=100, candidate_count=100
    )
    result = await generator._analyze(bundle)

    assert result == success_compact
    assert generator.analyzer.analyze.await_count == 2
    assert generator.analyzer.analyze.await_args_list[0].kwargs == {"compact": False}
    assert generator.analyzer.analyze.await_args_list[1].kwargs == {"compact": True}
    generator.analyzer.analyze_batched.assert_not_called()


def _make_dummy_bundle() -> PreparedBundle:
    return PreparedBundle(
        records={
            "S000001": SourceRecord(
                "S000001",
                Message(
                    text="Жители сообщают о ситуации",
                    sender="u",
                    timestamp=datetime.now(timezone.utc),
                    link="l",
                    channel_name="ch",
                    has_media=False,
                    media_type="",
                    message_id=1,
                ),
                "community",
            )
        },
        prompt_text="[S000001] Жители сообщают о ситуации",
        total_messages=1,
        candidate_count=1,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_systemic_true_attempts_repair_before_regeneration(sample_config, mock_logger):
    """Criteria 1: systemic=True with local FIXes undergoes local repair before regeneration."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Свет", "Лид", ["Текст с ошибкой"], [])
    systemic_fix = FactCheckResult(
        "FIX",
        True,
        [
            AuditIssue(
                "P001",
                "unsupported_scale",
                "Текст с ошибкой",
                "Scale unsupported",
                "Narrow scale",
                [],
                "fix",
            )
        ],
    )
    pass_result = FactCheckResult("PASS", False, [])
    repaired_draft = ArticleDraft("Свет", "Лид", ["Исправленный текст"], [])

    generator.fact_checker.check = AsyncMock(side_effect=[systemic_fix, pass_result])
    generator.fact_checker.repair = AsyncMock(return_value=repaired_draft)
    generator.writer.write = AsyncMock()

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())

    assert result.paragraphs == ["Исправленный текст"]
    assert generator.fact_checker.repair.await_count == 1
    generator.writer.write.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_repair_pass_prevents_regeneration(sample_config, mock_logger):
    """Criteria 2: If first repair pass returns PASS, writer.write() is NOT called a second time."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Свет", "Лид", ["Текст с ошибкой"], [])
    initial_fix = FactCheckResult(
        "FIX",
        True,
        [
            AuditIssue(
                "P001",
                "unsupported_scale",
                "Текст с ошибкой",
                "Scale unsupported",
                "Narrow",
                [],
                "fix",
            )
        ],
    )
    pass_result = FactCheckResult("PASS", False, [])
    repaired_draft = ArticleDraft("Свет", "Лид", ["Исправленный текст"], [])

    generator.fact_checker.check = AsyncMock(side_effect=[initial_fix, pass_result])
    generator.fact_checker.repair = AsyncMock(return_value=repaired_draft)
    generator.writer.write = AsyncMock()

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())

    assert result == repaired_draft
    generator.writer.write.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_still_systemic_fix_after_two_repairs_triggers_exactly_one_regeneration(
    sample_config, mock_logger
):
    """Criteria 3 & 4: If still systemic FIX after 2 repairs, exactly ONE regeneration with feedback is triggered."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Свет", "Лид", ["Текст"], [])
    systemic_fix = FactCheckResult(
        "FIX",
        True,
        [
            AuditIssue(
                "P001",
                "unsupported_scale",
                "Текст",
                "Scale unsupported",
                "Narrow",
                [],
                "fix",
            )
        ],
    )
    pass_result = FactCheckResult("PASS", False, [])
    regenerated_draft = ArticleDraft("Свет новый", "Лид новый", ["Регенерированный текст"], [])

    # Initial check -> repair 1 -> check 2 -> repair 2 -> check 3 -> regeneration -> regenerated check
    generator.fact_checker.check = AsyncMock(
        side_effect=[systemic_fix, systemic_fix, systemic_fix, pass_result]
    )
    generator.fact_checker.repair = AsyncMock(return_value=draft)
    generator.writer.write = AsyncMock(return_value=regenerated_draft)

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())

    assert result.headline == "Свет новый"
    assert generator.writer.write.await_count == 1
    # Verify feedback was passed to write
    call_kwargs = generator.writer.write.await_args_list[0].kwargs
    assert call_kwargs.get("revision_feedback") == systemic_fix


@pytest.mark.unit
@pytest.mark.asyncio
async def test_regenerated_draft_with_local_fix_undergoes_repair(sample_config, mock_logger):
    """Criteria 5: Regenerated draft with local FIX goes through repair loop, not immediate failure."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Свет", "Лид", ["Текст"], [])
    systemic_fix = FactCheckResult(
        "FIX",
        True,
        [
            AuditIssue(
                "P001",
                "unsupported_scale",
                "Текст",
                "Scale unsupported",
                "Narrow",
                [],
                "fix",
            )
        ],
    )
    regenerated_draft = ArticleDraft(
        "Свет 2", "Лид 2", ["Регенерированный текст с локальной ошибкой"], []
    )
    local_fix = FactCheckResult(
        "FIX",
        False,
        [
            AuditIssue(
                "P001",
                "unsupported_scale",
                "Регенерированный текст",
                "Local issue",
                "Fix it",
                [],
                "fix",
            )
        ],
    )
    pass_result = FactCheckResult("PASS", False, [])
    repaired_regenerated = ArticleDraft(
        "Свет 2", "Лид 2", ["Исправленный регенерированный текст"], []
    )

    # Initial check -> repair 1 -> check 2 -> repair 2 -> check 3
    # -> regeneration -> regenerated check (local_fix)
    # -> regenerated repair 1 -> regenerated check 2 (pass)
    generator.fact_checker.check = AsyncMock(
        side_effect=[systemic_fix, systemic_fix, systemic_fix, local_fix, pass_result]
    )
    generator.fact_checker.repair = AsyncMock(side_effect=[draft, draft, repaired_regenerated])
    generator.writer.write = AsyncMock(return_value=regenerated_draft)

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())

    assert result.paragraphs == ["Исправленный регенерированный текст"]
    assert generator.writer.write.await_count == 1
    assert generator.fact_checker.repair.await_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_second_regeneration_on_persistent_systemic_issue(sample_config, mock_logger):
    """Criteria 6 & regression: Persistent blocking issue after regeneration triggers UnsafeDraftError, no 2nd write."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Свет", "Лид", ["Текст"], [])
    systemic_fix = FactCheckResult(
        "FIX",
        True,
        [
            AuditIssue(
                "P001",
                "unsupported_casualty",
                "Текст",
                "Casualty unsupported",
                "Remove",
                [],
                "fix",
                publication_blocking=True,
            )
        ],
    )
    regenerated_draft = ArticleDraft("Свет 2", "Лид 2", ["Регенерированный"], [])

    # Initial check -> repair 1 -> check 2 -> repair 2 -> check 3
    # -> regeneration -> regenerated check (systemic)
    # -> regenerated repair 1 -> regenerated check 2 (systemic)
    # -> regenerated repair 2 -> regenerated check 3 (systemic)
    generator.fact_checker.check = AsyncMock(
        side_effect=[
            systemic_fix,
            systemic_fix,
            systemic_fix,
            systemic_fix,
            systemic_fix,
            systemic_fix,
        ]
    )
    generator.fact_checker.repair = AsyncMock(return_value=draft)
    generator.writer.write = AsyncMock(return_value=regenerated_draft)

    with pytest.raises(UnsafeDraftError, match="unresolved publication-blocking FIX remains"):
        await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())

    assert generator.writer.write.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unsafe_draft_falls_back_to_story_cards_not_deterministic_regex(
    sample_config, mock_logger
):
    """Criteria 7 & 8: UnsafeDraftError uses _render_story_card_fallback without calling fallback_builder."""
    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(
        id="SC001",
        topic="Тема света",
        importance="high",
        summary="Сводка по свету",
        representative_source_refs=["S000001"],
    )
    analysis = EditorialAnalysis(cards=[card])
    draft = ArticleDraft("Свет", "Лид", ["Текст"], [])

    generator._analyze = AsyncMock(return_value=analysis)
    generator.writer.write = AsyncMock(return_value=draft)
    generator._repair_and_check = AsyncMock(
        side_effect=UnsafeDraftError("unresolved publication-blocking FIX remains")
    )
    generator.fallback_renderer.render = MagicMock(
        return_value=ArticleDraft(
            "Что происходило в городе за сутки", "Лид", ["Сводка по свету"], []
        )
    )
    generator.fallback_builder.build = MagicMock()

    bundle_messages = {
        "ch": [
            Message(
                text="Жители сообщают",
                sender="u",
                timestamp=datetime.now(timezone.utc),
                link="l",
                channel_name="ch",
                has_media=False,
                media_type="",
                message_id=1,
            )
        ]
    }

    title, lead, body = await generator.generate_article(bundle_messages)

    assert title == "Что происходило в городе за сутки"
    assert "Сводка по свету" in body
    generator.fallback_renderer.render.assert_called_once_with(analysis.cards)
    generator.fallback_builder.build.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deterministic_fallback_used_only_if_story_card_renderer_fails(
    sample_config, mock_logger
):
    """Criteria 9: If StoryCard renderer fails, fallback_builder is called as the ultimate fallback."""
    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(
        id="SC001",
        topic="Свет",
        importance="high",
        summary="Сводка",
        representative_source_refs=["S000001"],
    )
    analysis = EditorialAnalysis(cards=[card])
    draft = ArticleDraft("Свет", "Лид", ["Текст"], [])

    generator._analyze = AsyncMock(return_value=analysis)
    generator.writer.write = AsyncMock(return_value=draft)
    generator._repair_and_check = AsyncMock(
        side_effect=UnsafeDraftError("unresolved publication-blocking FIX remains")
    )
    generator.fallback_renderer.render = MagicMock(side_effect=RuntimeError("renderer crash"))
    generator._fallback = AsyncMock(
        return_value=("Фоллбэк заголовок", "Фоллбэк лид", "Фоллбэк тело")
    )

    bundle_messages = {
        "ch": [
            Message(
                text="Жители сообщают про свет",
                sender="u",
                timestamp=datetime.now(timezone.utc),
                link="l",
                channel_name="ch",
                has_media=False,
                media_type="",
                message_id=1,
            )
        ]
    }

    title, _, body = await generator.generate_article(bundle_messages)

    assert title == "Фоллбэк заголовок"
    generator._fallback.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unresolved_title_lead_heading_fix_raises_unsafe_draft_error(
    sample_config, mock_logger
):
    """Criteria 10: Unresolved publication-blocking FIX on TITLE/LEAD/heading raises UnsafeDraftError."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Свет с ошибкой", "Лид", ["Текст"], [])
    title_fix = FactCheckResult(
        "FIX",
        False,
        [
            AuditIssue(
                "TITLE",
                "unsupported_evacuation",
                "Свет с ошибкой",
                "Fake evacuation order in headline",
                "Fix",
                [],
                "fix",
                publication_blocking=True,
            )
        ],
    )

    generator.fact_checker.check = AsyncMock(return_value=title_fix)
    generator.fact_checker.repair = AsyncMock(return_value=draft)
    generator.writer.write = AsyncMock(return_value=draft)

    with pytest.raises(UnsafeDraftError, match="unresolved publication-blocking FIX remains"):
        await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unresolved_non_blocking_title_fix_publishes_writer_draft(sample_config, mock_logger):
    """Soft publication gate: Non-blocking FIX in headline is published with warning."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Бердянск без света: официальных сроков нет", "Лид", ["Текст"], [])
    title_fix = FactCheckResult(
        "FIX",
        False,
        [
            AuditIssue(
                "TITLE",
                "absolute_absence",
                "Бердянск без света: официальных сроков нет",
                "Corpus boundary note",
                "Fix",
                [],
                "fix",
                publication_blocking=False,
            )
        ],
    )

    generator.fact_checker.check = AsyncMock(return_value=title_fix)
    generator.fact_checker.repair = AsyncMock(return_value=draft)

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())
    assert result == draft


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_check_unavailable_publishes_valid_draft_non_blocking(
    sample_config, mock_logger
):
    """Criteria 11: FactCheckUnavailableError remains non-blocking and publishes current valid draft."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Свет", "Лид", ["Текст"], [])

    generator.fact_checker.check = AsyncMock(
        side_effect=FactCheckUnavailableError("Fact check timed out")
    )

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())

    assert result == draft


@pytest.mark.unit
@pytest.mark.asyncio
async def test_systemic_to_non_systemic_repair_proceeds_to_local_removal_without_regeneration(
    sample_config, mock_logger
):
    """Criteria 12: Initial systemic FIX repaired to non-systemic FIX does not regenerate, retains non-blocking prose."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Свет", "Лид", ["Текст", "Удаляемый абзац"], [])
    initial_systemic = FactCheckResult(
        "FIX",
        True,
        [
            AuditIssue(
                "P001",
                "unsupported_scale",
                "Текст",
                "Scale unsupported",
                "Fix",
                [],
                "fix",
                publication_blocking=False,
            )
        ],
    )
    non_systemic_fix = FactCheckResult(
        "FIX",
        False,
        [
            AuditIssue(
                "P002",
                "unsupported_fact",
                "Удаляемый абзац",
                "Local detail unsupported",
                "Remove",
                [],
                "fix",
                publication_blocking=False,
            )
        ],
    )

    # Initial check (systemic) -> repair 1 -> check (non-systemic) -> repair 2 -> check (non-systemic)
    generator.fact_checker.check = AsyncMock(
        side_effect=[initial_systemic, non_systemic_fix, non_systemic_fix]
    )
    generator.fact_checker.repair = AsyncMock(return_value=draft)
    generator.writer.write = AsyncMock()

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())

    assert result.paragraphs == ["Текст", "Удаляемый абзац"]
    generator.writer.write.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_regenerated_audit_unavailable_publishes_regenerated_prose(
    sample_config, mock_logger
):
    """Criteria 13: When audit of regenerated draft fails with FactCheckUnavailableError, publish regenerated draft."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Свет", "Лид", ["Текст"], [])
    systemic_fix = FactCheckResult(
        "FIX",
        True,
        [
            AuditIssue(
                "P001",
                "unsupported_scale",
                "Текст",
                "Scale unsupported",
                "Fix",
                [],
                "fix",
                publication_blocking=False,
            )
        ],
    )
    regenerated = ArticleDraft("Свет новый", "Лид новый", ["Регенерированный текст"], [])

    # Initial check (systemic) -> repair 1 -> check (systemic) -> repair 2 -> check (systemic)
    # -> regeneration -> regenerated check (FactCheckUnavailableError)
    generator.fact_checker.check = AsyncMock(
        side_effect=[
            systemic_fix,
            systemic_fix,
            systemic_fix,
            FactCheckUnavailableError("Fact check timed out"),
        ]
    )
    generator.fact_checker.repair = AsyncMock(return_value=draft)
    generator.writer.write = AsyncMock(return_value=regenerated)

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())

    assert result == regenerated


@pytest.mark.unit
@pytest.mark.asyncio
async def test_regenerated_draft_with_audit_timeout_publishes_regenerated_prose_even_with_prior_blocking_fix(
    sample_config, mock_logger
):
    """Full regeneration produced new valid draft; if post-regeneration fact-check fails, publish regenerated draft with warning."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Свет", "Лид", ["Текст с ложной эвакуацией"], [])
    blocking_fix = FactCheckResult(
        "FIX",
        False,
        [
            AuditIssue(
                "P001",
                "unsupported_evacuation",
                "Текст с ложной эвакуацией",
                "Fake evacuation order",
                "Fix",
                [],
                "fix",
                publication_blocking=True,
            )
        ],
    )
    regenerated = ArticleDraft(
        "Свет новый", "Лид новый", ["Регенерированный текст без эвакуации"], []
    )

    generator.fact_checker.check = AsyncMock(
        side_effect=[
            blocking_fix,
            blocking_fix,
            blocking_fix,
            FactCheckUnavailableError("Fact check timed out"),
        ]
    )
    generator.fact_checker.repair = AsyncMock(return_value=draft)
    generator.writer.write = AsyncMock(return_value=regenerated)

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())
    assert result == regenerated


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocking_unit_tracking_is_not_carried_across_full_regeneration(
    sample_config, mock_logger
):
    """Blocking unit tracking from pre-regeneration draft is not carried over to regenerated draft."""
    generator = ArticleGenerator(sample_config, mock_logger)
    draft = ArticleDraft("Свет", "Лид", ["Текст с ложной эвакуацией"], [])
    blocking_fix = FactCheckResult(
        "FIX",
        False,
        [
            AuditIssue(
                "P001",
                "unsupported_evacuation",
                "Текст с ложной эвакуацией",
                "Fake evacuation order",
                "Fix",
                [],
                "fix",
                publication_blocking=True,
            )
        ],
    )
    # Regenerated draft has different unit structure where P001 is a normal non-blocking paragraph
    regenerated = ArticleDraft("Свет новый", "Лид новый", ["Нормальный абзац"], [])
    regenerated_check = FactCheckResult(
        "FIX",
        False,
        [
            AuditIssue(
                "P001",
                "unsupported_scale",
                "Нормальный абзац",
                "Soft scale",
                "Fix",
                [],
                "fix",
                publication_blocking=False,
            )
        ],
    )

    generator.fact_checker.check = AsyncMock(
        side_effect=[
            blocking_fix,
            blocking_fix,
            blocking_fix,
            regenerated_check,
            FactCheckUnavailableError("Fact check timed out"),
        ]
    )
    generator.fact_checker.repair = AsyncMock(side_effect=[draft, draft, regenerated])
    generator.writer.write = AsyncMock(return_value=regenerated)

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _make_dummy_bundle())
    assert result == regenerated


@pytest.mark.unit
@pytest.mark.asyncio
async def test_feedback_guided_regeneration_failure_triggers_story_card_fallback(
    sample_config, mock_logger
):
    """Regeneration failure with prior blocking fix raises UnsafeDraftError and triggers Story Card fallback."""
    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(
        id="SC001",
        topic="Свет",
        importance="high",
        summary="Сводка",
        representative_source_refs=["S000001"],
    )
    analysis = EditorialAnalysis(cards=[card])
    draft = ArticleDraft("Свет", "Лид", ["Текст"], [])
    systemic_blocking_fix = FactCheckResult(
        "FIX",
        True,
        [
            AuditIssue(
                "P001",
                "unsupported_casualty",
                "Текст",
                "Casualty unsupported",
                "Fix",
                [],
                "fix",
                publication_blocking=True,
            )
        ],
    )

    generator._analyze = AsyncMock(return_value=analysis)
    generator.writer.write = AsyncMock(
        side_effect=[draft, RuntimeError("Regeneration provider timeout")]
    )
    generator.fact_checker.check = AsyncMock(return_value=systemic_blocking_fix)
    generator.fact_checker.repair = AsyncMock(return_value=draft)
    generator.fallback_renderer.render = MagicMock(
        return_value=ArticleDraft("Что происходило в городе за сутки", "Лид", ["Сводка"], [])
    )

    bundle_messages = {
        "ch": [
            Message(
                text="Жители сообщают про свет",
                sender="u",
                timestamp=datetime.now(timezone.utc),
                link="l",
                channel_name="ch",
                has_media=False,
                media_type="",
                message_id=1,
            )
        ]
    }

    title, _, body = await generator.generate_article(bundle_messages)

    assert title == "Что происходило в городе за сутки"
    assert "Сводка" in body
    generator.fallback_renderer.render.assert_called_once_with(analysis.cards)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repair_structural_failure_falls_back_to_story_cards_not_rejected_draft(
    sample_config, mock_logger
):
    """Regression: When initial audit returns blocking FIX and local repair produces a structurally malformed draft,
    the rejected original draft is NOT published, Story Card fallback is used, and fallback_builder is NOT called.
    """
    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(
        id="SC001",
        topic="Свет",
        importance="high",
        summary="Сводка по отключениям",
        representative_source_refs=["S000001"],
    )
    analysis = EditorialAnalysis(cards=[card])
    rejected_initial_draft = ArticleDraft(
        "Бракованный заголовок", "Бракованный лид", ["Бракованный текст абзаца"], []
    )
    # Malformed draft produced by repair (empty title/lead or invalid markdown that fails deterministic_preflight)
    malformed_repaired_draft = ArticleDraft("", "", [], [])

    fix_result = FactCheckResult(
        "FIX",
        systemic_problem=False,
        issues=[
            AuditIssue(
                "P001",
                "unsupported_casualty",
                "Бракованный текст",
                "Casualty unsupported",
                "Fix",
                [],
                "fix",
                publication_blocking=True,
            )
        ],
    )

    generator._analyze = AsyncMock(return_value=analysis)
    generator.writer.write = AsyncMock(
        side_effect=[rejected_initial_draft, RuntimeError("Regeneration failed")]
    )
    generator.fact_checker.check = AsyncMock(return_value=fix_result)
    generator.fact_checker.repair = AsyncMock(return_value=malformed_repaired_draft)
    generator.fallback_renderer.render = MagicMock(
        return_value=ArticleDraft(
            "Что происходило в городе за сутки", "Лид сводки", ["Сводка по отключениям"], []
        )
    )
    generator.fallback_builder.build = MagicMock()

    bundle_messages = {
        "ch": [
            Message(
                text="Жители сообщают про свет",
                sender="u",
                timestamp=datetime.now(timezone.utc),
                link="l",
                channel_name="ch",
                has_media=False,
                media_type="",
                message_id=1,
            )
        ]
    }

    title, lead, body = await generator.generate_article(bundle_messages)

    # 1. Story Card fallback was used
    assert title == "Что происходило в городе за сутки"
    assert "Сводка по отключениям" in body
    generator.fallback_renderer.render.assert_called_once_with(analysis.cards)

    # 2. Original rejected writer draft was NOT published
    assert title != "Бракованный заголовок"
    assert "Бракованный текст" not in body

    # 3. Deterministic fallback builder was NOT called while AI Story Cards were valid
    generator.fallback_builder.build.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repair_loop_recomputes_blocking_units_on_new_blocking_issue(
    sample_config, mock_logger
):
    """Regression: When initial audit has non-blocking FIX, but repair introduces a new blocking FIX,

    subsequent audit timeout while the new blocking unit remains unmodified raises UnsafeDraftError.
    """
    generator = ArticleGenerator(sample_config, mock_logger)
    initial_draft = ArticleDraft("Свет", "Лид", ["Текст 1", "Текст 2"], [])
    initial_non_blocking = FactCheckResult(
        "FIX",
        False,
        [
            AuditIssue(
                "P001",
                "unsupported_scale",
                "Текст 1",
                "Soft scale overstatement",
                "Narrow scale",
                [],
                "fix",
                publication_blocking=False,
            )
        ],
    )
    # Repair #1 produces intermediate draft where P002 has fake evacuation
    intermediate_draft = ArticleDraft(
        "Свет", "Лид", ["Исправленный текст 1", "Ложная эвакуация на Пушкина"], []
    )
    second_check_new_blocking = FactCheckResult(
        "FIX",
        False,
        [
            AuditIssue(
                "P002",
                "unsupported_evacuation",
                "Ложная эвакуация на Пушкина",
                "Fake evacuation order",
                "Remove",
                [],
                "fix",
                publication_blocking=True,
            )
        ],
    )

    # Initial check -> repair 1 (returns intermediate_draft) -> check 2 (new blocking FIX on P002)
    # -> repair 2 (returns intermediate_draft unchanged) -> check 3 (FactCheckUnavailableError)
    generator.fact_checker.check = AsyncMock(
        side_effect=[
            initial_non_blocking,
            second_check_new_blocking,
            FactCheckUnavailableError("Fact check timed out"),
        ]
    )
    generator.fact_checker.repair = AsyncMock(side_effect=[intermediate_draft, intermediate_draft])

    with pytest.raises(
        UnsafeDraftError,
        match="fact check unavailable during repair with unmodified publication-blocking unit",
    ):
        await generator._repair_and_check(
            initial_draft, EditorialAnalysis([]), _make_dummy_bundle()
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generator_leaked_source_opacity_keeps_writer_prose(sample_config, mock_logger):
    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(
        id="SC001",
        topic="Вода",
        importance="high",
        summary="Воду отключили до вечера.",
        representative_source_refs=["S000001"],
    )
    analysis = EditorialAnalysis(cards=[card])
    leaked_draft = ArticleDraft(
        headline="Отключение воды в городе",
        lead="В нескольких районах пропала вода.",
        paragraphs=["В доступных сообщениях сроки восстановления не названы."],
        sections=[],
    )

    generator._analyze = AsyncMock(return_value=analysis)
    generator.writer.write = AsyncMock(return_value=leaked_draft)
    generator._repair_and_check = AsyncMock(return_value=leaked_draft)
    generator.fallback_renderer.render = MagicMock()

    bundle_messages = {
        "ch": [
            Message(
                text="Воды нет.",
                sender="u",
                timestamp=datetime.now(timezone.utc),
                link="l",
                channel_name="ch",
                has_media=False,
                media_type="",
                message_id=1,
            )
        ]
    }

    title, lead, body = await generator.generate_article(bundle_messages)

    assert title == "Отключение воды в городе"
    assert lead == "В нескольких районах пропала вода."
    assert "сроки восстановления" in body
    generator.fallback_renderer.render.assert_not_called()
    assert any(
        "publication-copy" in str(call).lower() or "source mechanics" in str(call).lower()
        for call in mock_logger.warning.call_args_list
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generator_structural_preflight_remains_hard_gate(sample_config, mock_logger):
    generator = ArticleGenerator(sample_config, mock_logger)
    card = StoryCard(
        id="SC001",
        topic="Вода",
        importance="high",
        summary="Воду отключили до вечера.",
        representative_source_refs=["S000001"],
    )
    analysis = EditorialAnalysis(cards=[card])
    malformed_draft = ArticleDraft(
        headline="Отключение воды в городе",
        lead="В нескольких районах пропала вода.",
        paragraphs=["Текст с маркером [S000001], который нарушает структуру."],
        sections=[],
    )

    generator._analyze = AsyncMock(return_value=analysis)
    generator.writer.write = AsyncMock(return_value=malformed_draft)
    generator._repair_and_check = AsyncMock(return_value=malformed_draft)
    generator.fallback_renderer.render = MagicMock(
        return_value=ArticleDraft(
            "Что происходило в городе за сутки",
            "Лид сводки.",
            ["Воду отключили до вечера."],
            [],
        )
    )

    bundle_messages = {
        "ch": [
            Message(
                text="Воды нет.",
                sender="u",
                timestamp=datetime.now(timezone.utc),
                link="l",
                channel_name="ch",
                has_media=False,
                media_type="",
                message_id=1,
            )
        ]
    }

    title, lead, body = await generator.generate_article(bundle_messages)

    generator.fallback_renderer.render.assert_called_once()
    assert title == "Что происходило в городе за сутки"
    assert "[S000001]" not in body
