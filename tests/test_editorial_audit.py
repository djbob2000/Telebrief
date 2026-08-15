"""Tests for non-blocking fact checks, targeted repair and deterministic preflight."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai_providers import ProviderCascadeError, TokenBudgetExhaustedError
from src.collector import Message
from src.editorial_audit import (
    AuditIssue,
    FactCheckResult,
    FactCheckUnavailableError,
    LightFactChecker,
    deterministic_preflight,
)
from src.editorial_models import (
    EditorialAnalysis,
    PreparedBundle,
    SourceRecord,
    StoryCard,
    StoryElement,
    Uncertainty,
)
from src.editorial_writer import ArticleDraft


def _draft() -> ArticleDraft:
    return ArticleDraft(
        headline="Перебои со светом стали заметной темой дня",
        lead="Жители нескольких районов рассказали о перебоях со светом.",
        paragraphs=[
            "В чатах обсуждали генераторы и места для зарядки телефонов.",
            "Продажи генераторов выросли вдвое.",
        ],
        sections=[],
    )


def _bundle() -> PreparedBundle:
    message = Message(
        text="Жители рассказали о перебоях со светом.",
        sender="Житель",
        timestamp=datetime(2026, 8, 14, tzinfo=timezone.utc),
        link="https://t.me/source/1",
        channel_name="Source",
        has_media=False,
        media_type="",
        message_id=1,
    )
    return PreparedBundle(
        records={"S000001": SourceRecord("S000001", message, "community")},
        prompt_text="[S000001] source_type=community\ntext: Жители рассказали о перебоях со светом.",
        total_messages=1,
        candidate_count=1,
    )


def test_fact_check_status_normalization_prefers_fix_over_warn():
    result = FactCheckResult.from_dict(
        {
            "status": "pass",
            "systemic_problem": False,
            "issues": [
                {
                    "unit_id": "P002",
                    "code": "unsupported_number",
                    "original_excerpt": "Продажи выросли вдвое.",
                    "reason": "No source supports the number.",
                    "suggested_direction": "Remove the comparison.",
                    "source_refs": [],
                    "severity": "fix",
                },
                {
                    "unit_id": "P001",
                    "code": "synthesis",
                    "original_excerpt": "Заметная тема дня.",
                    "reason": "Editorial synthesis.",
                    "suggested_direction": "Keep with context.",
                    "source_refs": ["S000001"],
                    "severity": "warn",
                },
            ],
        }
    )

    assert result.status == "FIX"
    assert result.issues[0].unit_id == "P002"


def test_deterministic_preflight_rejects_internal_markers_and_accepts_article():
    deterministic_preflight("# Заголовок\n\nЛид\n\nАбзац")

    with pytest.raises(ValueError, match="internal"):
        deterministic_preflight("# Заголовок\n\nS000001 факт")

    with pytest.raises(ValueError, match="JSON"):
        deterministic_preflight('{"headline": "Заголовок"}')


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_checker_marks_unsupported_concrete_detail_as_fix(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        return_value=json.dumps(
            {
                "status": "WARN",
                "systemic_problem": False,
                "issues": [
                    {
                        "unit_id": "P002",
                        "code": "unsupported_scale",
                        "original_excerpt": "Продажи генераторов выросли вдвое.",
                        "reason": "No supplied material contains sales data.",
                        "suggested_direction": "Delete the sentence.",
                        "source_refs": [],
                        "severity": "fix",
                    }
                ],
            }
        )
    )
    checker = LightFactChecker(provider, "model", mock_logger)

    result = await checker.check(_draft(), EditorialAnalysis([]), _bundle())

    assert result.status == "FIX"
    assert result.issues[0].unit_id == "P002"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_checker_uses_configured_longform_output_budget(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        return_value=json.dumps({"status": "PASS", "systemic_problem": False, "issues": []})
    )
    checker = LightFactChecker(provider, "model", mock_logger, max_output_tokens=65536)

    await checker.check(_draft(), EditorialAnalysis([]), _bundle())

    assert provider.chat_completion.call_args.kwargs["max_tokens"] == 65536


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_checker_uses_smaller_repair_budget(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        return_value=json.dumps({"replacements": {"P002": "Продажи не оценивались."}})
    )
    checker = LightFactChecker(
        provider, "model", mock_logger, max_output_tokens=16384, repair_max_output_tokens=8192
    )
    result = FactCheckResult(
        status="FIX",
        systemic_problem=False,
        issues=[
            AuditIssue(
                unit_id="P002",
                code="unsupported_scale",
                original_excerpt="Продажи генераторов выросли вдвое.",
                reason="No source supports it.",
                suggested_direction="Remove the claim.",
                source_refs=[],
            )
        ],
    )

    await checker.repair(_draft(), result, EditorialAnalysis([]), _bundle())

    assert provider.chat_completion.call_args.kwargs["max_tokens"] == 8192


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repair_replaces_only_flagged_unit(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        return_value=json.dumps({"replacements": {"P002": "Продажи не оценивались."}})
    )
    checker = LightFactChecker(provider, "model", mock_logger)
    result = FactCheckResult(
        status="FIX",
        systemic_problem=False,
        issues=[
            AuditIssue(
                unit_id="P002",
                code="unsupported_scale",
                original_excerpt="Продажи генераторов выросли вдвое.",
                reason="No source supports it.",
                suggested_direction="Remove the claim.",
                source_refs=[],
            )
        ],
    )

    repaired = await checker.repair(_draft(), result, EditorialAnalysis([]), _bundle())

    assert repaired.paragraphs[0] == _draft().paragraphs[0]
    assert repaired.paragraphs[1] == "Продажи не оценивались."


def test_light_fact_checker_prompt_distinguishes_synthesis_from_unverified_facts():
    import logging

    checker = LightFactChecker(
        provider=None,
        model="test-model",
        logger=logging.getLogger("test"),
    )
    prompt = checker._build_system_prompt()
    assert "synthesis" in prompt.lower()
    assert "FIX" in prompt
    assert "WARN" in prompt
    assert "verifiable" in prompt.lower() or "unverified" in prompt.lower()


def test_fact_checker_prompt_enforces_scale_denominator_and_absence_rules():
    import logging

    checker = LightFactChecker(
        provider=None,
        model="test-model",
        logger=logging.getLogger("test"),
    )
    prompt = checker._build_system_prompt()
    assert "denominator" in prompt.lower()
    assert "majority" in prompt.lower()
    assert "absence" in prompt.lower()
    assert "corpus" in prompt.lower()
    assert "FIX" in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_checker_exposes_json_parse_failure_diagnostics(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(return_value="{invalid json")
    checker = LightFactChecker(provider, "model", mock_logger)

    with pytest.raises(FactCheckUnavailableError):
        await checker.check(_draft(), EditorialAnalysis([]), _bundle())

    assert checker.last_stage == "json_parse"
    assert checker.last_raw_response == "{invalid json"
    assert checker.last_response_chars == len("{invalid json")
    assert (
        "json" in (checker.last_reason or "").lower()
        or "expecting" in (checker.last_reason or "").lower()
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_checker_audit_payload_contains_no_duplicate_draft_text(mock_logger):
    """Audit payload sends article text strictly once via audit_units, without duplicate draft."""
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        return_value=json.dumps({"status": "PASS", "systemic_problem": False, "issues": []})
    )
    checker = LightFactChecker(provider, "model", mock_logger)
    draft = _draft()
    await checker.check(draft, EditorialAnalysis([]), _bundle())

    assert provider.chat_completion.call_count == 1
    call_kwargs = provider.chat_completion.call_args.kwargs
    user_content = call_kwargs["messages"][1]["content"]
    payload = json.loads(user_content)

    assert "draft" not in payload
    assert "article_outline" not in payload
    assert "audit_units" in payload
    assert set(payload.keys()) == {"audit_units", "story_cards", "source_records"}


@pytest.mark.unit
def test_compact_story_cards_preserve_epistemic_status_and_attribution():
    """Compact story cards retain element status, attribution, refs, and basis while omitting debug metadata."""
    card = StoryCard(
        id="SC001",
        topic="Disruptions in power and water",
        importance="high",
        summary="Power grid disruptions affected central districts.",
        hard_facts=[
            StoryElement(
                text="Substation damaged near market.",
                source_refs=["S000001", "S000002"],
                status="established",
                attribution="",
                areas=["Center"],
            )
        ],
        community_observations=[
            StoryElement(
                text="Residents report evening outages.",
                source_refs=["S000003"],
                status="attributed",
                attribution="Residents in chats",
            )
        ],
        useful_details=[
            StoryElement(
                text="Local cafe provides charging.",
                source_refs=["S000004"],
                status="attributed",
            )
        ],
        uncertainties=[
            Uncertainty(
                text="Conflicting estimates of repair time from 2 days to 2 weeks.",
                basis="chat rumors",
                related_source_refs=["S000005"],
            )
        ],
    )
    analysis = EditorialAnalysis(
        cards=[card], labels={"S000001": "power"}, excluded_refs=["S000099"]
    )
    compact_cards = LightFactChecker._compact_story_cards(analysis, minimal=False)

    assert len(compact_cards) == 1
    c = compact_cards[0]
    assert c["id"] == "SC001"
    assert c["topic"] == "Disruptions in power and water"
    assert c["summary"] == "Power grid disruptions affected central districts."
    assert c["source_refs"] == ["S000001", "S000002", "S000003", "S000004", "S000005"]

    assert c["hard_facts"] == [
        {
            "text": "Substation damaged near market.",
            "status": "established",
            "attribution": "",
            "source_refs": ["S000001", "S000002"],
        }
    ]
    assert c["community_observations"] == [
        {
            "text": "Residents report evening outages.",
            "status": "attributed",
            "attribution": "Residents in chats",
            "source_refs": ["S000003"],
        }
    ]
    assert c["useful_details"] == [
        {
            "text": "Local cafe provides charging.",
            "status": "attributed",
            "attribution": "",
            "source_refs": ["S000004"],
        }
    ]
    assert c["uncertainties"] == [
        {
            "text": "Conflicting estimates of repair time from 2 days to 2 weeks.",
            "basis": "chat rumors",
            "related_source_refs": ["S000005"],
        }
    ]
    assert "labels" not in c
    assert "excluded_refs" not in c


@pytest.mark.unit
def test_compact_story_cards_minimal_mode_retains_summaries_only():
    """Minimal mode on compact story cards retains only id, topic, summary, and sorted refs."""
    card = StoryCard(
        id="SC001",
        topic="Disruptions",
        importance="high",
        summary="Power grid disruptions.",
        hard_facts=[StoryElement(text="Fact", source_refs=["S000002"])],
        uncertainties=[Uncertainty(text="Unc", related_source_refs=["S000001"])],
    )
    analysis = EditorialAnalysis(cards=[card])
    compact_cards = LightFactChecker._compact_story_cards(analysis, minimal=True)

    assert len(compact_cards) == 1
    c = compact_cards[0]
    assert set(c.keys()) == {"id", "topic", "summary", "source_refs"}
    assert c["id"] == "SC001"
    assert c["source_refs"] == ["S000001", "S000002"]
    assert "hard_facts" not in c
    assert "uncertainties" not in c


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_checker_compact_retry_triggers_only_on_token_budget_exhaustion(mock_logger):
    """Token budget exhaustion triggers exactly one compact retry with minimal card summaries."""
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        side_effect=[
            TokenBudgetExhaustedError("token budget exhausted"),
            json.dumps({"status": "PASS", "systemic_problem": False, "issues": []}),
        ]
    )
    checker = LightFactChecker(provider, "model", mock_logger)
    card = StoryCard(
        id="SC001",
        topic="Power disruption",
        importance="high",
        summary="Power outage in central city.",
        hard_facts=[StoryElement(text="Substation damaged", source_refs=["S000001"])],
    )
    analysis = EditorialAnalysis(cards=[card])
    result = await checker.check(_draft(), analysis, _bundle())

    assert result.status == "PASS"
    assert provider.chat_completion.call_count == 2
    assert checker.last_stage is None
    assert checker.last_reason is None

    second_payload = json.loads(
        provider.chat_completion.await_args_list[1].kwargs["messages"][1]["content"]
    )
    assert set(second_payload) == {"audit_units", "story_cards", "source_records"}
    for c in second_payload["story_cards"]:
        assert set(c) == {"id", "topic", "summary", "source_refs"}
        assert "hard_facts" not in c
        assert "community_observations" not in c
        assert "useful_details" not in c
        assert "uncertainties" not in c


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ProviderCascadeError("quota error", failure_kinds=("quota",)),
        ProviderCascadeError("auth error", failure_kinds=("auth",)),
    ],
)
async def test_fact_checker_does_not_retry_on_non_token_budget_errors(error, mock_logger):
    """Non-token-budget errors fail immediately without triggering a compact retry."""
    provider = MagicMock()
    provider.chat_completion = AsyncMock(side_effect=error)
    checker = LightFactChecker(provider, "model", mock_logger)

    with pytest.raises(FactCheckUnavailableError):
        await checker.check(_draft(), EditorialAnalysis([]), _bundle())

    assert provider.chat_completion.call_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fact_checker_second_token_budget_failure_raises_unavailable(mock_logger):
    """A second consecutive token budget failure raises FactCheckUnavailableError without looping."""
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        side_effect=[
            TokenBudgetExhaustedError("first exhaustion"),
            TokenBudgetExhaustedError("second exhaustion"),
        ]
    )
    checker = LightFactChecker(provider, "model", mock_logger)

    with pytest.raises(FactCheckUnavailableError):
        await checker.check(_draft(), EditorialAnalysis([]), _bundle())

    assert provider.chat_completion.call_count == 2
    assert checker.last_stage == "provider_call"
    assert checker.last_reason is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compact_retry_json_parse_failure_preserves_structured_reason(mock_logger):
    provider = MagicMock()
    provider.chat_completion = AsyncMock(
        side_effect=[
            TokenBudgetExhaustedError("output budget exceeded"),
            "invalid json format response",
        ]
    )
    checker = LightFactChecker(provider, "model", mock_logger)
    draft = ArticleDraft(headline="Заголовок", lead="Лид", paragraphs=["Текст"])

    with pytest.raises(FactCheckUnavailableError):
        await checker.check(draft, EditorialAnalysis([]), _bundle())

    assert checker.last_stage == "json_parse"
    assert "position" in checker.last_reason or "Expecting value" in checker.last_reason
