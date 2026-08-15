"""Tests for Story Card analysis and explicit context-size batching."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai_providers import ProviderCascadeError, TokenBudgetExhaustedError
from src.collector import Message
from src.editorial_analysis import (
    ContextSizeRejectedError,
    EditorialAnalysisError,
    EditorialAnalyzer,
)
from src.editorial_models import PreparedBundle, SourceRecord


def _bundle() -> PreparedBundle:
    message = Message(
        text="Коммунальное предприятие сообщило об отключении воды.",
        sender="КП",
        timestamp=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        link="https://t.me/source/1",
        channel_name="Official",
        has_media=False,
        media_type="",
        message_id=1,
    )
    records = {
        "S000001": SourceRecord(
            ref="S000001", message=message, source_type="official", context_text=""
        )
    }
    return PreparedBundle(
        records=records,
        prompt_text="[S000001] source_type=official\ntext: Коммунальное предприятие сообщило об отключении воды.",
        total_messages=1,
        candidate_count=1,
    )


def _analysis_json() -> str:
    return json.dumps(
        {
            "cards": [
                {
                    "id": "SC001",
                    "topic": "Вода",
                    "importance": "high",
                    "story_kind": "infrastructure",
                    "summary": "Воду отключили.",
                    "editorial_angle": {
                        "text": "Коммунальная тема заметна жителям.",
                        "basis_refs": ["S000001"],
                        "type": "editorial_synthesis",
                    },
                    "hard_facts": [
                        {
                            "text": "Предприятие сообщило об отключении.",
                            "source_refs": ["S000001"],
                            "status": "established",
                        }
                    ],
                    "community_observations": [],
                    "useful_details": [],
                    "uncertainties": [
                        {
                            "text": "Причина не установлена.",
                            "basis": "No supplied source directly establishes the cause",
                            "related_source_refs": ["S000001"],
                        }
                    ],
                }
            ],
            "labels": {"S000001": {"label": "news_item", "flags": []}},
        }
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_parses_cards_and_keeps_source_roles(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=_analysis_json())
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    analysis = await analyzer.analyze(_bundle())

    assert analysis.cards[0].hard_facts[0].source_refs == ["S000001"]
    assert analysis.cards[0].editorial_angle["type"] == "editorial_synthesis"
    system_prompt, user_prompt = analyzer.build_prompt(_bundle())
    assert "untrusted data" in system_prompt.lower()
    assert "4–8" in system_prompt
    assert "Classify each message" not in user_prompt
    assert "source_type=official" in user_prompt
    assert "S000001" in user_prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_exposes_context_rejection_for_caller(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        side_effect=ProviderCascadeError(
            "all slots failed", failure_kinds=("context_size",), failure_labels=("primary",)
        )
    )
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(ContextSizeRejectedError):
        await analyzer.analyze(_bundle())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_reports_provider_failure_kind(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        side_effect=ProviderCascadeError("all slots failed", failure_kinds=("quota", "timeout"))
    )
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(EditorialAnalysisError) as error:
        await analyzer.analyze(_bundle())

    assert error.value.stage == "provider_call"
    assert error.value.reason == "quota,timeout"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_maps_token_budget_failure(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(side_effect=TokenBudgetExhaustedError("provider details"))
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(EditorialAnalysisError) as error:
        await analyzer.analyze(_bundle())

    assert error.value.stage == "provider_call"
    assert error.value.reason == "token_budget"


@pytest.mark.unit
def test_editorial_analyzer_compact_prompt_requests_only_significant_stories(mock_logger):
    analyzer = EditorialAnalyzer(MagicMock(), "model", mock_logger)

    system_prompt, user_prompt = analyzer.build_prompt(_bundle(), compact=True)

    assert "3–6" in system_prompt
    assert "Do not classify or label every supplied message" in system_prompt
    assert "S000001" in user_prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batched_analysis_reports_provider_failure_kind(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        side_effect=ProviderCascadeError("all slots failed", failure_kinds=("quota",))
    )
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(EditorialAnalysisError) as error:
        await analyzer.analyze_batched(_bundle())

    assert error.value.stage == "provider_call"
    assert error.value.reason == "quota"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_rejects_story_card_refs_outside_bundle(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        return_value=json.dumps(
            {
                "cards": [
                    {
                        "id": "SC001",
                        "topic": "Вода",
                        "importance": "medium",
                        "summary": "Тема",
                        "hard_facts": [
                            {
                                "text": "Факт",
                                "source_refs": ["S999999"],
                                "status": "attributed",
                            }
                        ],
                    }
                ]
            }
        )
    )
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(EditorialAnalysisError, match="S999999") as error:
        await analyzer.analyze(_bundle())

    assert error.value.stage == "invalid_source_ref"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_reports_json_parse_stage(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value='{"cards": [')
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(EditorialAnalysisError) as error:
        await analyzer.analyze(_bundle())

    assert error.value.stage == "json_parse"
    assert error.value.response_chars == len('{"cards": [')


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["", "[]", '{"cards": {}}'])
async def test_editorial_analyzer_reports_empty_or_invalid_shape(mock_logger, response):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=response)
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(EditorialAnalysisError) as error:
        await analyzer.analyze(_bundle())

    assert error.value.stage in {"empty_response", "response_shape"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_reports_story_card_parse_stage(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        return_value=json.dumps(
            {
                "cards": [
                    {
                        "id": "SC001",
                        "topic": "",
                        "importance": "high",
                        "summary": "",
                    }
                ]
            }
        )
    )
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(EditorialAnalysisError) as error:
        await analyzer.analyze(_bundle())

    assert error.value.stage == "story_card_parse"
    assert error.value.response_chars is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_keeps_valid_cards_when_one_card_is_malformed(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        return_value=json.dumps(
            {
                "cards": [
                    {
                        "id": "SC001",
                        "topic": "Вода",
                        "importance": "high",
                        "summary": "Воду отключили.",
                        "hard_facts": [],
                    },
                    {"id": "SC002", "topic": "", "importance": "medium", "summary": ""},
                ]
            }
        )
    )
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    analysis = await analyzer.analyze(_bundle())

    assert [card.id for card in analysis.cards] == ["SC001"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_normalizes_common_card_aliases(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        return_value=json.dumps(
            {
                "cards": [
                    {
                        "title": "Вода",
                        "description": "Воду временно отключили.",
                        "importance": "urgent",
                        "sources": ["S000001"],
                    }
                ]
            }
        )
    )
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    analysis = await analyzer.analyze(_bundle())

    card = analysis.cards[0]
    assert card.id == "SC001"
    assert card.topic == "Вода"
    assert card.summary == "Воду временно отключили."
    assert card.importance == "medium"
    assert card.hard_facts[0].source_refs == ["S000001"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_keeps_last_raw_response_for_debug_artifact(mock_logger):
    raw = '{"cards": ['
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=raw)
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(EditorialAnalysisError):
        await analyzer.analyze(_bundle())

    assert analyzer.last_raw_response == raw


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_clears_raw_response_before_next_provider_call(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        side_effect=[
            '{"cards": [',
            ProviderCascadeError("provider unavailable", failure_kinds=("timeout",)),
        ]
    )
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(EditorialAnalysisError):
        await analyzer.analyze(_bundle())
    assert analyzer.last_raw_response == '{"cards": ['

    with pytest.raises(EditorialAnalysisError):
        await analyzer.analyze(_bundle())
    assert analyzer.last_raw_response == ""
