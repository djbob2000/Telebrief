"""Integration tests for main and degraded editorial generation paths."""

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.article_generator import ArticleGenerator
from src.collector import Message
from src.config_loader import Config, Settings
from src.editorial_analysis import ContextSizeRejectedError, EditorialAnalysisError
from src.editorial_audit import AuditIssue, FactCheckResult
from src.editorial_models import (
    EditorialAnalysis,
    PreparedBundle,
    SourceRecord,
    StoryCard,
    StoryElement,
)
from src.editorial_writer import ArticleDraft


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
        timestamp=datetime(2026, 8, 15, 10, tzinfo=timezone.utc) + timedelta(minutes=message_id),
        link=f"https://t.me/source/{message_id}",
        channel_name="Source",
        has_media=False,
        media_type="",
        message_id=message_id,
    )


def _bundle() -> PreparedBundle:
    message = _message("Жители сообщили о перебоях со светом")
    return PreparedBundle(
        records={"S000001": SourceRecord("S000001", message, "community")},
        prompt_text="[S000001] Жители сообщили о перебоях со светом",
        total_messages=1,
        candidate_count=1,
    )


def _repair_issue(unit_id: str = "P001") -> AuditIssue:
    return AuditIssue(
        unit_id=unit_id,
        code="unsupported_detail",
        original_excerpt="Неподтвержденная деталь",
        reason="Нет подтверждения",
        suggested_direction="Удалить",
        source_refs=[],
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
async def test_token_budget_switches_to_compact_analysis_and_reaches_writer():
    generator = _generator()
    token_error = EditorialAnalysisError("token budget")
    token_error.stage = "provider_call"
    token_error.reason = "token_budget"
    compact_analysis = EditorialAnalysis(
        cards=[
            StoryCard(
                id="SC001",
                topic="Свет",
                importance="high",
                summary="Жители сообщили о перебоях со светом.",
                hard_facts=[
                    StoryElement(
                        text="Жители сообщили о перебоях со светом.",
                        source_refs=["S000001"],
                        status="attributed",
                    )
                ],
            )
        ]
    )
    generator.analyzer.analyze = AsyncMock(
        side_effect=[
            token_error,
            compact_analysis,
        ]
    )
    generator.writer.write = AsyncMock(
        return_value=ArticleDraft("Перебои со светом", "Лид", ["Абзац"], [])
    )
    generator.fact_checker.check = AsyncMock(return_value=FactCheckResult("PASS", False, []))

    title, lead, body = await generator.generate_article(
        {"Source": [_message("Жители сообщили о перебоях со светом")]}
    )

    assert title == "Перебои со светом"
    assert lead == "Лид"
    assert "Абзац" in body
    assert generator.analyzer.analyze.call_args_list[0].kwargs == {"compact": False}
    assert generator.analyzer.analyze.call_args_list[1].kwargs == {"compact": True}
    writer_bundle = generator.writer.write.call_args.args[1]
    assert set(writer_bundle.records) == {"S000001"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_context_rejection_uses_batching_without_compact_retry():
    generator = _generator()
    analysis = EditorialAnalysis(cards=[])
    context_error = ContextSizeRejectedError("too large")
    context_error.stage = "provider_call"
    context_error.reason = "context_size"
    generator.analyzer.analyze = AsyncMock(side_effect=context_error)
    generator.analyzer.analyze_batched = AsyncMock(return_value=analysis)

    result = await generator._analyze(_bundle())

    assert result is analysis
    generator.analyzer.analyze.assert_awaited_once_with(_bundle(), compact=False)
    generator.analyzer.analyze_batched.assert_awaited_once_with(_bundle(), compact=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analysis_without_resolvable_refs_uses_complete_bundle_fallback():
    generator = _generator()
    generator.analyzer.analyze = AsyncMock(
        return_value=EditorialAnalysis(
            cards=[
                StoryCard(
                    id="SC001",
                    topic="Свет",
                    importance="high",
                    summary="Сводка без исходных refs.",
                )
            ]
        )
    )
    generator.writer.write = AsyncMock()

    title, _, body = await generator.generate_article(
        {"Source": [_message("На Колонии нет света")]}
    )

    assert title == "Что происходило в городе за сутки"
    assert "Жители сообщали о перебоях с электроснабжением" in body
    generator.writer.write.assert_not_awaited()


@pytest.mark.unit
def test_writer_bundle_keeps_all_representative_refs_from_story_card():
    generator = _generator()
    records = {}
    refs = []
    for index in range(120):
        ref = f"S{index + 1:06d}"
        refs.append(ref)
        records[ref] = SourceRecord(ref, _message(f"Наблюдение {index}", index + 1), "community")
    bundle = PreparedBundle(records, "", 120, 120)
    analysis = EditorialAnalysis(
        cards=[
            StoryCard(
                id="SC001",
                topic="Город",
                importance="medium",
                summary="Большой сюжет.",
                community_observations=[StoryElement("Наблюдения жителей.", refs, "attributed")],
            )
        ]
    )

    selected = generator._select_writer_bundle(analysis, bundle)

    assert set(selected.records) == set(refs)


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
async def test_writer_failure_renders_validated_story_cards_before_full_fallback():
    generator = _generator()
    generator.analyzer.analyze = AsyncMock(
        return_value=EditorialAnalysis(
            cards=[
                StoryCard(
                    id="SC001",
                    topic="Вода",
                    importance="high",
                    summary="Воду временно отключали.",
                    hard_facts=[
                        StoryElement(
                            "Источник сообщил об отключении водоснабжения.",
                            ["S000001"],
                            "established",
                        )
                    ],
                )
            ]
        )
    )
    generator.writer.write = AsyncMock(side_effect=RuntimeError("writer down"))

    title, _, body = await generator.generate_article(
        {"Source": [_message("Источник сообщил об отключении воды")]}
    )

    assert title == "Что происходило в городе за сутки"
    assert "Источник сообщил об отключении водоснабжения." in body
    assert "Вода" in body
    generator.writer.write.assert_awaited_once()


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
async def test_analysis_raw_response_is_saved_only_as_opt_in_debug_artifact(tmp_path):
    generator = _generator()
    generator.config.settings.article.save_debug_artifacts = True
    generator.config.settings.article.debug_artifact_dir = str(tmp_path)
    generator.provider.chat_completion = AsyncMock(return_value='{"cards": [')

    await generator.generate_article({"Source": [_message("На Колонии нет света")]})

    assert (tmp_path / "editorial_analysis_raw.txt").read_text() == '{"cards": ['


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_second_repair_is_checked_before_removing_remaining_issues():
    generator = _generator()
    draft = ArticleDraft("Свет", "Лид", ["Первый текст", "Незатронутый абзац"], [])
    first_fix = FactCheckResult("FIX", False, [_repair_issue()])
    passed = FactCheckResult("PASS", False, [])
    repaired_once = ArticleDraft("Свет", "Лид", ["Промежуточный текст", "Незатронутый абзац"], [])
    repaired_twice = ArticleDraft("Свет", "Лид", ["Исправленный текст", "Незатронутый абзац"], [])
    generator.fact_checker.check = AsyncMock(side_effect=[first_fix, first_fix, passed])
    generator.fact_checker.repair = AsyncMock(side_effect=[repaired_once, repaired_twice])

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _bundle())

    assert result.paragraphs == ["Исправленный текст", "Незатронутый абзац"]
    assert generator.fact_checker.check.await_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_systemic_regeneration_cleans_local_fix_before_publish():
    generator = _generator()
    draft = ArticleDraft("Свет", "Лид", ["Старый текст"], [])
    regenerated = ArticleDraft("Свет", "Лид", ["Новая неподтвержденная деталь"], [])
    initial = FactCheckResult("FIX", True, [_repair_issue()])
    local_fix = FactCheckResult("FIX", False, [_repair_issue()])
    generator.fact_checker.check = AsyncMock(side_effect=[initial, local_fix])
    generator.writer.write = AsyncMock(return_value=regenerated)

    result = await generator._repair_and_check(draft, EditorialAnalysis([]), _bundle())

    assert result.to_markdown() == "# Свет\n\nЛид"
    assert generator.fact_checker.check.await_count == 2
