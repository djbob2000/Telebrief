"""Tests for non-blocking fact checks, targeted repair and deterministic preflight."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.collector import Message
from src.editorial_audit import (
    AuditIssue,
    FactCheckResult,
    LightFactChecker,
    deterministic_preflight,
)
from src.editorial_models import EditorialAnalysis, PreparedBundle, SourceRecord
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
