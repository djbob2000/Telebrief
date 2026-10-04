"""Capture provider-reported accounting without retaining response/source content."""

from __future__ import annotations

import math
from typing import Any, cast


def summarize_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    def total(key: str) -> int | float | None:
        values = [record.get(key) for record in records]
        if not values or any(
            not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0
            for v in values
        ):
            return None
        return sum(cast(float, v) for v in values)

    return {
        "cost": total("cost"),
        "prompt_tokens": total("prompt_tokens"),
        "completion_tokens": total("completion_tokens"),
        "transport_requests": len(records),
    }


def instrument_usage(provider: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def visit(current: Any) -> None:
        slots = getattr(current, "providers", None)
        if slots:
            for slot in slots:
                visit(slot[1])
            return
        client = getattr(current, "client", None)
        if client is None:
            return
        completions = client.chat.completions
        original = completions.create

        async def record_create(*args: Any, **kwargs: Any) -> Any:
            row = {
                "model": kwargs.get("model"),
                "cost": None,
                "prompt_tokens": None,
                "completion_tokens": None,
            }
            records.append(row)
            response = await original(*args, **kwargs)
            usage = getattr(response, "usage", None)
            if usage is not None:
                data = usage.model_dump() if hasattr(usage, "model_dump") else vars(usage)
                row.update(
                    {key: data.get(key) for key in ("cost", "prompt_tokens", "completion_tokens")}
                )
            return response

        completions.create = record_create

    visit(provider)
    return records
