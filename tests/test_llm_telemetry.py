"""Tests for task-local LLM stage telemetry metadata."""

from __future__ import annotations

from src.llm_telemetry import get_llm_call_context, llm_call_context


def test_llm_call_context_is_set_and_reset():
    assert get_llm_call_context() is None
    with llm_call_context(stage="event_triage", prompt_hash="abc", story_count=3):
        context = get_llm_call_context()
        assert context is not None
        assert context.stage == "event_triage"
        assert context.prompt_hash == "abc"
        assert context.story_count == 3
    assert get_llm_call_context() is None


def test_nested_llm_call_context_restores_outer_context():
    with llm_call_context(stage="outer"):
        with llm_call_context(stage="inner"):
            assert get_llm_call_context().stage == "inner"  # type: ignore[union-attr]
        assert get_llm_call_context().stage == "outer"  # type: ignore[union-attr]
