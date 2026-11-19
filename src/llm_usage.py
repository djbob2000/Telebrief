"""Per-call LLM usage and cost records, buffered and written to PostgreSQL.

Every provider response becomes one :class:`LLMUsageRecord`: the stage that made
the call (from :mod:`src.llm_telemetry`), the model, token counts and the cost
OpenRouter reports in ``usage.cost``. Records are buffered in process memory
and written in small batches, so a model call never waits for the database and
never fails because of it. A process without an installed writer (legacy paths,
unit tests) keeps only the ``llm_usage`` log line.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from src.llm_telemetry import get_llm_call_context

logger = logging.getLogger(__name__)

UNTAGGED_STAGE = "untagged"
# Records that arrive within this window are written in one transaction.
FLUSH_DELAY_SECONDS = 5.0
# Bound on unwritten records; the oldest are dropped when the database is down.
MAX_BUFFERED_RECORDS = 5000

UsageWriter = Callable[[Sequence["LLMUsageRecord"]], Awaitable[None]]


@dataclass(frozen=True)
class LLMUsageRecord:
    """Usage of one completed provider request."""

    created_at: dt.datetime
    stage: str
    provider: str
    model: str
    origin: str
    response_id: str | None
    prompt_tokens: int | None
    cached_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: float | None
    story_count: int | None
    finish_reason: str | None
    elapsed_ms: int | None


def _field(container: Any, name: str) -> Any:
    if container is None:
        return None
    if isinstance(container, dict):
        return container.get(name)
    value = getattr(container, name, None)
    if value is None:
        extra = getattr(container, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(name)
    return value


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def process_origin() -> str:
    """Name of the running entry point, e.g. ``worker.py`` or ``preview_article.py``."""
    argv0 = sys.argv[0] if sys.argv else ""
    return os.path.basename(argv0) or "unknown"


def usage_record_from_response(
    response: Any,
    *,
    provider: str,
    model: str,
    elapsed_seconds: float | None,
    finish_reason: str | None,
) -> LLMUsageRecord:
    """Build a record from a chat-completion response and the current call context.

    OpenRouter returns ``usage.cost`` (USD credits) and
    ``usage.prompt_tokens_details.cached_tokens`` on every chat completion; other
    providers leave those fields empty.
    """
    usage = _field(response, "usage")
    context = get_llm_call_context()
    response_id = _field(response, "id")
    return LLMUsageRecord(
        created_at=dt.datetime.now(dt.UTC),
        stage=context.stage if context is not None else UNTAGGED_STAGE,
        provider=provider,
        model=model,
        origin=process_origin(),
        response_id=response_id if isinstance(response_id, str) else None,
        prompt_tokens=_int(_field(usage, "prompt_tokens")),
        cached_tokens=_int(_field(_field(usage, "prompt_tokens_details"), "cached_tokens")),
        completion_tokens=_int(_field(usage, "completion_tokens")),
        reasoning_tokens=_int(
            _field(_field(usage, "completion_tokens_details"), "reasoning_tokens")
        ),
        cost_usd=_float(_field(usage, "cost")),
        story_count=context.story_count if context is not None else None,
        finish_reason=finish_reason,
        elapsed_ms=int(elapsed_seconds * 1000) if elapsed_seconds is not None else None,
    )


class LLMUsageRecorder:
    """Buffer usage records and write them in debounced batches."""

    def __init__(self, writer: UsageWriter, *, flush_delay: float = FLUSH_DELAY_SECONDS) -> None:
        self._writer = writer
        self._flush_delay = flush_delay
        self._buffer: deque[LLMUsageRecord] = deque(maxlen=MAX_BUFFERED_RECORDS)
        self._pending: asyncio.Task[None] | None = None
        self.dropped = 0

    def record(self, record: LLMUsageRecord) -> None:
        if len(self._buffer) == self._buffer.maxlen:
            self.dropped += 1
        self._buffer.append(record)
        if self._pending is not None and not self._pending.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # flushed on close
        self._pending = loop.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(self._flush_delay)
        await self.flush()

    async def flush(self) -> None:
        """Write every buffered record; on failure, log and drop the batch."""
        while self._buffer:
            batch = list(self._buffer)
            self._buffer.clear()
            try:
                await self._writer(batch)
            except Exception as exc:  # noqa: BLE001 - usage accounting must not break calls
                self.dropped += len(batch)
                logger.warning(
                    "could not write %d LLM usage records (%s: %s); they remain in the log",
                    len(batch),
                    type(exc).__name__,
                    exc,
                )
                return

    async def close(self) -> None:
        """Cancel the pending delayed flush and write what is left."""
        pending, self._pending = self._pending, None
        if pending is not None and not pending.done():
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
        await self.flush()


_recorder: LLMUsageRecorder | None = None


def install_llm_usage_recorder(recorder: LLMUsageRecorder) -> None:
    global _recorder
    _recorder = recorder


async def close_llm_usage_recorder(recorder: LLMUsageRecorder) -> None:
    """Flush ``recorder`` and uninstall it when it is the installed one."""
    global _recorder
    if _recorder is recorder:
        _recorder = None
    await recorder.close()


def record_llm_usage(record: LLMUsageRecord) -> None:
    """Log the record and hand it to the installed recorder, if any."""
    logger.info(
        "llm_usage stage=%s model=%s origin=%s prompt_tokens=%s cached_tokens=%s "
        "completion_tokens=%s reasoning_tokens=%s cost_usd=%s story_count=%s",
        record.stage,
        record.model,
        record.origin,
        record.prompt_tokens,
        record.cached_tokens,
        record.completion_tokens,
        record.reasoning_tokens,
        record.cost_usd,
        record.story_count if record.story_count is not None else "-",
    )
    if _recorder is not None:
        _recorder.record(record)
