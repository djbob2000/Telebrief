"""Daily OpenRouter balance check.

Credits that run out stop every model call: publications fail and the Event
processing jobs retry until the balance returns. This check reads the account
balance once a day and warns the operators before that happens.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Warn when the account has less than this many USD of credits left.
MIN_CREDITS_REMAINING_USD = 5.0
# Warn when the workspace daily budget (set in OpenRouter) is nearly used up.
MIN_DAILY_BUDGET_REMAINING_USD = 0.5

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


@dataclasses.dataclass(frozen=True)
class OpenRouterBalance:
    credits_remaining_usd: float | None
    daily_budget_remaining_usd: float | None
    daily_budget_resets_at: str | None


def parse_balance(credits: dict[str, Any], key: dict[str, Any]) -> OpenRouterBalance:
    """Build a balance snapshot from the `/credits` and `/key` response bodies."""
    total = credits.get("total_credits")
    used = credits.get("total_usage")
    remaining = float(total) - float(used) if total is not None and used is not None else None
    budget = key.get("effective_budget") or {}
    daily = budget.get("remaining_usd")
    return OpenRouterBalance(
        credits_remaining_usd=remaining,
        daily_budget_remaining_usd=float(daily) if daily is not None else None,
        daily_budget_resets_at=budget.get("resets_at"),
    )


def balance_warnings(
    balance: OpenRouterBalance,
    *,
    min_credits_usd: float = MIN_CREDITS_REMAINING_USD,
    min_daily_usd: float = MIN_DAILY_BUDGET_REMAINING_USD,
) -> list[str]:
    """Return human-readable warnings; an empty list means the balance is fine."""
    warnings: list[str] = []
    credits = balance.credits_remaining_usd
    if credits is not None and credits < min_credits_usd:
        warnings.append(
            f"На счёте OpenRouter осталось ${credits:.2f}. Публикации остановятся, "
            "когда баланс закончится. Пополните счёт."
        )
    daily = balance.daily_budget_remaining_usd
    if daily is not None and daily < min_daily_usd:
        resets = balance.daily_budget_resets_at or "в полночь UTC"
        warnings.append(
            f"Дневной лимит OpenRouter почти исчерпан: осталось ${daily:.2f}, "
            f"сбросится {resets}. Вечерние статьи могут не выйти."
        )
    return warnings


async def fetch_openrouter_balance(api_key: str, base_url: str) -> OpenRouterBalance:
    """Read the account balance and the workspace budget from OpenRouter."""
    root = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT, headers=headers) as session:
        async with session.get(f"{root}/credits") as response:
            response.raise_for_status()
            credits = (await response.json()).get("data") or {}
        async with session.get(f"{root}/key") as response:
            response.raise_for_status()
            key = (await response.json()).get("data") or {}
    return parse_balance(credits, key)


async def check_openrouter_balance(
    *,
    api_key: str,
    base_url: str,
    recipient_ids: Sequence[int],
    send_message: Callable[[int, str], Awaitable[None]],
) -> list[str]:
    """Check the balance once, alert the operators when needed, and return the warnings."""
    if not api_key:
        return []
    try:
        balance = await fetch_openrouter_balance(api_key, base_url)
    except Exception:
        logger.warning("OpenRouter balance check failed", exc_info=True)
        return []
    logger.info(
        "OpenRouter balance: credits_remaining_usd=%s daily_budget_remaining_usd=%s",
        balance.credits_remaining_usd,
        balance.daily_budget_remaining_usd,
    )
    warnings = balance_warnings(balance)
    if not warnings:
        return []
    text = "⚠️ " + "\n".join(warnings)
    for recipient_id in recipient_ids:
        try:
            await send_message(recipient_id, text)
        except Exception:
            logger.warning("Could not send OpenRouter balance alert", exc_info=True)
    return warnings
