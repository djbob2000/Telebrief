"""Context-local metadata for structured LLM usage telemetry."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class LLMCallContext:
    """Metadata associated with one stage-level LLM request."""

    stage: str
    prompt_hash: str | None = None
    story_count: int | None = None


current_llm_call_context: ContextVar[LLMCallContext | None] = ContextVar(
    "current_llm_call_context", default=None
)


@contextmanager
def llm_call_context(
    *,
    stage: str,
    prompt_hash: str | None = None,
    story_count: int | None = None,
) -> Iterator[LLMCallContext]:
    """Set stage metadata for the duration of one provider call."""
    context = LLMCallContext(stage=stage, prompt_hash=prompt_hash, story_count=story_count)
    token = current_llm_call_context.set(context)
    try:
        yield context
    finally:
        current_llm_call_context.reset(token)


def get_llm_call_context() -> LLMCallContext | None:
    """Return the current task-local call metadata, if a stage set it."""
    return current_llm_call_context.get()
