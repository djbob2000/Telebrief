# ruff: noqa: S101
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.article_generator import ArticleGenerator
from src.openrouter_balance import (
    OpenRouterBalance,
    balance_warnings,
    check_openrouter_balance,
    parse_balance,
)
from src.publication.article_context import ArticleEditorialContext


def test_parse_balance_reads_credits_and_workspace_budget() -> None:
    balance = parse_balance(
        {"total_credits": 70, "total_usage": 66.5},
        {"effective_budget": {"remaining_usd": 1.5, "resets_at": "2026-11-08T00:00:00Z"}},
    )
    assert balance.credits_remaining_usd == pytest.approx(3.5)
    assert balance.daily_budget_remaining_usd == pytest.approx(1.5)
    assert balance.daily_budget_resets_at == "2026-11-08T00:00:00Z"


def test_parse_balance_handles_missing_budget() -> None:
    balance = parse_balance({"total_credits": 100, "total_usage": 10}, {"effective_budget": None})
    assert balance.credits_remaining_usd == pytest.approx(90)
    assert balance.daily_budget_remaining_usd is None


def test_warnings_flag_low_credits_and_exhausted_daily_budget() -> None:
    low = OpenRouterBalance(3.84, 1.9, "2026-11-08T00:00:00Z")
    warnings = balance_warnings(low)
    assert len(warnings) == 1
    assert "$3.84" in warnings[0]

    daily = OpenRouterBalance(50.0, 0.1, None)
    assert len(balance_warnings(daily)) == 1


def test_no_warning_when_balance_is_healthy() -> None:
    assert balance_warnings(OpenRouterBalance(50.0, 1.9, None)) == []
    assert balance_warnings(OpenRouterBalance(None, None, None)) == []


def test_check_alerts_each_recipient_only_when_low(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    async def low_balance(api_key: str, base_url: str) -> OpenRouterBalance:
        return OpenRouterBalance(3.0, 1.9, None)

    monkeypatch.setattr("src.openrouter_balance.fetch_openrouter_balance", low_balance)
    warnings = asyncio.run(
        check_openrouter_balance(
            api_key="k", base_url="u", recipient_ids=[1, 2], send_message=fake_send
        )
    )
    assert len(warnings) == 1
    assert [chat_id for chat_id, _ in sent] == [1, 2]


def test_check_is_silent_when_healthy_or_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    async def healthy(api_key: str, base_url: str) -> OpenRouterBalance:
        return OpenRouterBalance(80.0, 1.9, None)

    monkeypatch.setattr("src.openrouter_balance.fetch_openrouter_balance", healthy)
    assert (
        asyncio.run(
            check_openrouter_balance(
                api_key="k", base_url="u", recipient_ids=[1], send_message=fake_send
            )
        )
        == []
    )
    assert (
        asyncio.run(
            check_openrouter_balance(
                api_key="", base_url="u", recipient_ids=[1], send_message=fake_send
            )
        )
        == []
    )
    assert sent == []


def test_check_survives_api_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(api_key: str, base_url: str) -> OpenRouterBalance:
        raise RuntimeError("network down")

    async def fake_send(chat_id: int, text: str) -> None:
        raise AssertionError("no alert expected when the check itself failed")

    monkeypatch.setattr("src.openrouter_balance.fetch_openrouter_balance", broken)
    assert (
        asyncio.run(
            check_openrouter_balance(
                api_key="k", base_url="u", recipient_ids=[1], send_message=fake_send
            )
        )
        == []
    )


def _generator_with(article_settings: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(settings=SimpleNamespace(article=article_settings))
    )


def _context(period_review_kind: str) -> ArticleEditorialContext:
    return ArticleEditorialContext(
        headline_candidates=(),
        support_index=(),
        support_by_id={},
        recurring_topics=(),
        period_review_kind=period_review_kind,
    )


def test_period_review_gets_its_own_wider_budget() -> None:
    settings = SimpleNamespace(
        article_generation_timeout_seconds=2400,
        period_review_generation_timeout_seconds=10800,
    )
    gen = _generator_with(settings)
    assert ArticleGenerator._article_generation_timeout_seconds(gen, _context("")) == 2400
    assert ArticleGenerator._article_generation_timeout_seconds(gen, _context("monthly")) == 10800


def test_invalid_period_review_budget_is_rejected() -> None:
    settings = SimpleNamespace(
        article_generation_timeout_seconds=2400,
        period_review_generation_timeout_seconds=0,
    )
    gen = _generator_with(settings)
    with pytest.raises(ValueError, match="period_review_generation_timeout_seconds"):
        ArticleGenerator._article_generation_timeout_seconds(gen, _context("monthly"))
