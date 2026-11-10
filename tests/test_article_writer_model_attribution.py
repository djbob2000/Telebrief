# ruff: noqa: S101
"""The writer's recorded model is the slot that answered, not the configured chain."""

from __future__ import annotations

from types import SimpleNamespace

from src.article_generator import _answering_model

_CHAIN = "deepseek/deepseek-v4.1-flash-1017:floor,openai/gpt-6-luna,anthropic/claude-haiku-5.5"


def test_successful_cascade_slot_names_the_model_that_answered() -> None:
    provider = SimpleNamespace(
        last_metadata={"provider_slot": "openrouter-secondary", "actual_model": "openai/gpt-6-luna"}
    )

    assert _answering_model(provider, _CHAIN) == "openai/gpt-6-luna"


def test_missing_metadata_falls_back_to_the_configured_chain() -> None:
    assert _answering_model(SimpleNamespace(), _CHAIN) == _CHAIN
    assert _answering_model(SimpleNamespace(last_metadata={}), _CHAIN) == _CHAIN


def test_blank_actual_model_is_not_trusted() -> None:
    provider = SimpleNamespace(last_metadata={"actual_model": "   "})

    assert _answering_model(provider, _CHAIN) == _CHAIN
