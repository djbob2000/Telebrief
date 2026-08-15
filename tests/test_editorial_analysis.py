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
    assert "up to 8" in system_prompt
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
@pytest.mark.asyncio
async def test_editorial_analyzer_uses_separate_normal_and_compact_budgets(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=_analysis_json())
    analyzer = EditorialAnalyzer(
        provider,
        "model",
        mock_logger,
        max_output_tokens=65536,
        compact_max_output_tokens=16384,
    )

    await analyzer.analyze(_bundle())
    await analyzer.analyze(_bundle(), compact=True)

    assert provider.chat_completion.call_args_list[0].kwargs["max_tokens"] == 65536
    assert provider.chat_completion.call_args_list[1].kwargs["max_tokens"] == 16384


@pytest.mark.unit
def test_editorial_analyzer_compact_prompt_requests_only_significant_stories(mock_logger):
    analyzer = EditorialAnalyzer(MagicMock(), "model", mock_logger)

    system_prompt, user_prompt = analyzer.build_prompt(_bundle(), compact=True)

    assert "up to 6" in system_prompt
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyzer_analyze_sanitizes_refs_and_succeeds_with_partial_bad_refs(mock_logger):
    payload = json.dumps(
        {
            "cards": [
                {
                    "id": "SC001",
                    "topic": "Вода",
                    "importance": "high",
                    "summary": "Воду отключили.",
                    "sources": ["S000001", "S999999"],
                    "hard_facts": [
                        {
                            "text": "Предприятие сообщило об отключении.",
                            "source_refs": ["S000001", "S999999"],
                        }
                    ],
                },
                {
                    "id": "SC002",
                    "topic": "Фантом",
                    "importance": "low",
                    "summary": "Фантомная тема.",
                    "sources": ["S999999"],
                    "hard_facts": [{"text": "Фантомный факт", "source_refs": ["S999999"]}],
                },
            ]
        }
    )
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=payload)
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    analysis = await analyzer.analyze(_bundle())
    assert len(analysis.cards) == 1
    assert analysis.cards[0].id == "SC001"
    assert analysis.cards[0].representative_source_refs == ["S000001"]
    assert analysis.cards[0].hard_facts[0].source_refs == ["S000001"]


@pytest.mark.unit
@pytest.mark.parametrize("compact", [False, True])
def test_analyzer_prompts_contain_locality_and_editorial_value_tests(compact, mock_logger):
    """Analyzer prompts include locality gate, editorial-value test, and preserved canonical fields."""
    analyzer = EditorialAnalyzer(MagicMock(), "model", mock_logger)
    system_prompt, user_prompt = analyzer.build_prompt(_bundle(), compact=compact)

    assert "Locality Test" in system_prompt or "locality" in system_prompt.lower()
    assert "Berdyansk" in system_prompt
    assert "Editorial-Value Test" in system_prompt or "editorial-value" in system_prompt.lower()
    assert "editorial_angle" in system_prompt
    assert "representative" in system_prompt.lower()


@pytest.mark.unit
@pytest.mark.parametrize("compact", [False, True])
def test_analyzer_prompts_contain_evidence_position_and_commercial_rules(compact, mock_logger):
    """Analyzer prompts distinguish hard_facts vs resident observations and demote commercial ads."""
    analyzer = EditorialAnalyzer(MagicMock(), "model", mock_logger)
    system_prompt, user_prompt = analyzer.build_prompt(_bundle(), compact=compact)

    assert "Evidence-Position Test" in system_prompt or "evidence-position" in system_prompt.lower()
    assert "hard_facts" in system_prompt
    assert "Commercial Demarcation" in system_prompt or "commercial" in system_prompt.lower()
    assert (
        "trend" in system_prompt.lower()
        or "shortage" in system_prompt.lower()
        or "migration" in system_prompt.lower()
    )


@pytest.mark.unit
@pytest.mark.parametrize("compact", [False, True])
def test_analyzer_prompts_contain_cardinality_bounds_and_quota_independence(compact, mock_logger):
    """Analyzer prompts enforce upper bounds (8 normal / 6 compact) with no minimum quota and zero valid."""
    analyzer = EditorialAnalyzer(MagicMock(), "model", mock_logger)
    system_prompt, user_prompt = analyzer.build_prompt(_bundle(), compact=compact)

    expected_target = "up to 6" if compact else "up to 8"
    assert expected_target in system_prompt
    assert (
        "One or two" in system_prompt
        or "1–2" in system_prompt
        or "one or two" in system_prompt.lower()
    )
    assert "zero" in system_prompt.lower()
    assert "quota" in system_prompt.lower() or "minimum" in system_prompt.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sanitize_or_fail_accepts_empty_cards_as_valid_editorial_result(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=json.dumps({"cards": []}))
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    analysis = await analyzer.analyze(_bundle())

    assert analysis.cards == []
    assert provider.chat_completion.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sanitize_or_fail_fails_when_non_empty_cards_lose_all_refs(mock_logger):
    payload = json.dumps(
        {
            "cards": [
                {
                    "id": "SC001",
                    "topic": "Фантом",
                    "importance": "high",
                    "summary": "Фантомная новость",
                    "sources": ["S999999"],
                    "hard_facts": [{"text": "Факт", "source_refs": ["S999999"]}],
                }
            ]
        }
    )
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=payload)
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(EditorialAnalysisError) as exc_info:
        await analyzer.analyze(_bundle())

    assert exc_info.value.stage == "invalid_source_ref"


@pytest.mark.unit
@pytest.mark.parametrize("compact", [False, True])
def test_analyzer_prompts_contain_informative_uncertainty_and_canonical_schema(
    compact, mock_logger
):
    analyzer = EditorialAnalyzer(MagicMock(), "model", mock_logger)
    system_prompt, _ = analyzer.build_prompt(_bundle(), compact=compact)

    assert "Informative Uncertainty" in system_prompt or "unverified" in system_prompt.lower()
    assert "noise" in system_prompt.lower() or "rumor" in system_prompt.lower()
    assert "representative_source_refs" in system_prompt
    assert "timeframe" in system_prompt
    assert "current_status" in system_prompt
    assert "next_known_step" in system_prompt
    assert "editorial_angle" in system_prompt


@pytest.mark.unit
@pytest.mark.parametrize("compact", [False, True])
def test_analyzer_prompts_contain_scale_hierarchy_and_corpus_boundary(compact, mock_logger):
    analyzer = EditorialAnalyzer(MagicMock(), "model", mock_logger)
    system_prompt, _ = analyzer.build_prompt(_bundle(), compact=compact)

    assert "geographic spread" in system_prompt.lower()
    assert "majority" in system_prompt.lower()
    assert "denominator" in system_prompt.lower()
    assert "corpus" in system_prompt.lower()
    assert "supplied records" in system_prompt.lower() or "supplied corpus" in system_prompt.lower()
