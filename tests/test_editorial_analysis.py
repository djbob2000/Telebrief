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
                            "basis": "Ни один источник прямо не указывает причину.",
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
async def test_editorial_analyzer_mixed_token_budget_and_context_size_does_not_raise_context_size_rejected(
    mock_logger,
):
    """Aggregate failure {token_budget, context_size} must NOT raise ContextSizeRejectedError so token_budget priority applies."""
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        side_effect=ProviderCascadeError(
            "all slots failed",
            failure_kinds=("token_budget", "context_size"),
            failure_labels=("primary", "fallback"),
        )
    )
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(EditorialAnalysisError) as exc_info:
        await analyzer.analyze(_bundle())

    assert not isinstance(exc_info.value, ContextSizeRejectedError)
    assert "token_budget" in exc_info.value.failure_kinds
    assert "context_size" in exc_info.value.failure_kinds


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_rejects_single_english_card_among_russian_cards(mock_logger):
    """Per-card language check: one English card among Russian cards fails analysis language validation."""
    russian_card = {
        "id": "SC001",
        "topic": "Отключения света",
        "importance": "high",
        "summary": "В городе продолжаются перебои с электроснабжением.",
        "hard_facts": [
            {
                "text": "Электроснабжение отключено в нескольких районах.",
                "source_refs": ["S000001"],
                "status": "established",
            }
        ],
    }
    english_card = {
        "id": "SC002",
        "topic": "Water supply",
        "importance": "medium",
        "summary": "Residents report severe water supply outages across the city.",
        "hard_facts": [
            {
                "text": "Water pressure dropped significantly yesterday evening.",
                "source_refs": ["S000001"],
                "status": "attributed",
            }
        ],
    }
    # 7 Russian cards + 1 English card
    payload = json.dumps({"cards": [russian_card] * 7 + [english_card]})
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=payload)
    analyzer = EditorialAnalyzer(provider, "model", mock_logger, output_language="Russian")

    with pytest.raises(EditorialAnalysisError) as exc_info:
        await analyzer.analyze(_bundle())

    assert exc_info.value.stage == "response_shape"
    assert exc_info.value.reason == "wrong_output_language"


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
    assert "LOCAL CONTEXT" in system_prompt


@pytest.mark.unit
def test_analyzer_prompt_preserves_materially_different_concrete_values(mock_logger):
    analyzer = EditorialAnalyzer(MagicMock(), "model", mock_logger)
    system, _ = analyzer.build_prompt(_bundle())
    lower = system.lower()

    assert "concrete values" in lower or "prices" in lower
    assert "representative" in lower
    assert "average" in lower or "midpoint" in lower
    assert "source refs" in lower


@pytest.mark.unit
def test_analyzer_prompt_keeps_corpus_boundary_internal(mock_logger):
    analyzer = EditorialAnalyzer(MagicMock(), "model", mock_logger)
    system, _ = analyzer.build_prompt(_bundle())

    assert "do not encode corpus absence as an established hard fact" in system.lower()
    assert "no official schedule appears in the supplied records" not in system.lower()


@pytest.mark.unit
def test_split_bundle_preserves_local_context(mock_logger):
    from src.city_context_models import AreaCandidate, CityContextAnnotation, ResolvedEntity

    analyzer = EditorialAnalyzer(MagicMock(), "model", mock_logger)

    msg1 = Message(
        text="На ул. Шевченко нет света",
        sender="u1",
        timestamp=datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
        link="",
        channel_name="ch1",
        has_media=False,
        media_type="",
        message_id=1,
    )
    cand = AreaCandidate(
        area_set="municipal_neighborhood_committees_2021",
        area_id="center",
        area_name="Центр",
        confidence="high",
        coverage_kind="whole_object",
        source_ref="gazetteer",
    )
    entity = ResolvedEntity(
        kind="place",
        entity_id="street:Шевченка",
        matched_text="ул. Шевченко",
        canonical_name="вулиця Шевченка",
        object_type="street",
        municipal_areas=(cand,),
    )
    rec1 = SourceRecord(
        ref="S000001",
        message=msg1,
        source_type="community",
        city_context=CityContextAnnotation(entities=(entity,)),
    )

    msg2 = Message(
        text="На Лисках воды нет",
        sender="u2",
        timestamp=datetime(2026, 8, 14, 13, tzinfo=timezone.utc),
        link="",
        channel_name="ch2",
        has_media=False,
        media_type="",
        message_id=2,
    )
    rec2 = SourceRecord(
        ref="S000002",
        message=msg2,
        source_type="community",
    )

    bundle = PreparedBundle(
        records={"S000001": rec1, "S000002": rec2},
        prompt_text="",
        total_messages=2,
        candidate_count=2,
    )

    batches = analyzer._split_bundle(bundle)
    assert len(batches) == 2
    b1 = next(b for b in batches if "S000001" in b.records)
    assert "local_context: street:Шевченка" in b1.prompt_text


@pytest.mark.unit
def test_editorial_analyzer_enforces_output_language_prompt_contract(mock_logger):
    provider = MagicMock()
    analyzer = EditorialAnalyzer(provider, "model", mock_logger, output_language="Russian")
    system_prompt, _ = analyzer.build_prompt(_bundle())
    assert (
        "exclusively in Russian" in system_prompt
        or "strictly and exclusively in Russian" in system_prompt
    )
    assert "importance values ('high'|'medium'|'low')" in system_prompt
    assert "status values ('established'|'attributed'|'disputed')" in system_prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analyzer_rejects_wrong_output_language(mock_logger):
    english_analysis = json.dumps(
        {
            "cards": [
                {
                    "id": "SC001",
                    "topic": "Citywide power outage enters third week",
                    "importance": "high",
                    "story_kind": "infrastructure outage",
                    "summary": "Berdyansk residents report a prolonged electricity blackout across multiple districts.",
                    "hard_facts": [
                        {
                            "text": "Residents in multiple districts reported being without electricity for about two weeks.",
                            "source_refs": ["S000001"],
                            "status": "attributed",
                            "attribution": "multiple residents in community chat",
                        }
                    ],
                    "community_observations": [],
                    "useful_details": [],
                    "uncertainties": [],
                }
            ]
        }
    )
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value=english_analysis)
    analyzer = EditorialAnalyzer(provider, "model", mock_logger, output_language="Russian")

    with pytest.raises(EditorialAnalysisError) as exc_info:
        await analyzer.analyze(_bundle())
    assert exc_info.value.stage == "response_shape"
    assert exc_info.value.reason == "wrong_output_language"


@pytest.mark.unit
def test_is_large_bundle_for_rescue_boundary_conditions():
    from src.editorial_analysis import is_large_bundle_for_rescue

    # 99 candidates, 49,999 chars -> False
    bundle_small = PreparedBundle(
        records={}, prompt_text="x" * 49_999, total_messages=99, candidate_count=99
    )
    assert is_large_bundle_for_rescue(bundle_small) is False

    # 100 candidates, 10 chars -> True
    bundle_100_candidates = PreparedBundle(
        records={}, prompt_text="x" * 10, total_messages=100, candidate_count=100
    )
    assert is_large_bundle_for_rescue(bundle_100_candidates) is True

    # 10 candidates, 50,000 chars -> True
    bundle_50k_chars = PreparedBundle(
        records={}, prompt_text="x" * 50_000, total_messages=10, candidate_count=10
    )
    assert is_large_bundle_for_rescue(bundle_50k_chars) is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_editorial_analysis_propagates_slot_failures_and_kinds(mock_logger):
    from src.ai_providers import ProviderSlotFailure

    slot1 = ProviderSlotFailure(slot="slot1", kind="timeout", exception_type="TimeoutError")
    slot2 = ProviderSlotFailure(slot="slot2", kind="context_size", exception_type="BadRequestError")
    provider_exc = ProviderCascadeError(
        "cascade failed",
        failure_kinds=("timeout", "context_size"),
        failure_labels=("slot1", "slot2"),
        slot_failures=(slot1, slot2),
    )
    provider = MagicMock()
    provider.chat_completion = AsyncMock(side_effect=provider_exc)
    analyzer = EditorialAnalyzer(provider, "model", mock_logger)

    with pytest.raises(ContextSizeRejectedError) as exc_info:
        await analyzer.analyze(_bundle())
    err = exc_info.value
    assert err.stage == "provider_call"
    assert err.reason == "context_size"
    assert err.failure_kinds == ("timeout", "context_size")
    assert len(err.slot_failures) == 2
    assert err.slot_failures[0] == slot1
    assert err.slot_failures[1] == slot2
