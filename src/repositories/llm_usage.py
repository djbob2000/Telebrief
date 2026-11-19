"""Persistence for per-call LLM usage records (``llm_usage_events``)."""

from __future__ import annotations

from collections.abc import Sequence

import psycopg

from src.llm_usage import LLMUsageRecord

_INSERT = """
INSERT INTO llm_usage_events (
    created_at, stage, provider, model, origin, response_id,
    prompt_tokens, cached_tokens, completion_tokens, reasoning_tokens,
    cost_usd, story_count, finish_reason, elapsed_ms
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


async def insert_llm_usage_events(
    conn: psycopg.AsyncConnection, records: Sequence[LLMUsageRecord]
) -> None:
    if not records:
        return
    async with conn.cursor() as cursor:
        await cursor.executemany(
            _INSERT,
            [
                (
                    r.created_at,
                    r.stage,
                    r.provider,
                    r.model,
                    r.origin,
                    r.response_id,
                    r.prompt_tokens,
                    r.cached_tokens,
                    r.completion_tokens,
                    r.reasoning_tokens,
                    r.cost_usd,
                    r.story_count,
                    r.finish_reason,
                    r.elapsed_ms,
                )
                for r in records
            ],
        )
