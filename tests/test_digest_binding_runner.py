# ruff: noqa: S101
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.digest_evaluation.binding import _build_prompt, bind_digest_candidate
from src.publication.digest_presentation import RequiredDigestFact


class FakeProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def chat_completion(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.response


def context() -> SimpleNamespace:
    facts = (
        RequiredDigestFact(
            fact_id="power",
            rubric_id="infrastructure",
            subject_key="power_supply",
            subject_label="Электроснабжение",
            story_ids=("story:power",),
            support_ids=("support:power",),
            text="В районе нет электричества.",
        ),
    )
    plan = SimpleNamespace(
        blocks=(
            SimpleNamespace(
                support_ids_by_story=(
                    ("story:power", ("support:power",)),
                    ("story:library", ("support:library",)),
                )
            ),
        )
    )
    presentation_plan = SimpleNamespace(
        story_ids=("story:power", "story:library"), required_facts=facts
    )
    return SimpleNamespace(
        plan=plan,
        presentation_plan=presentation_plan,
        support_text_by_id={
            "support:power": "На улице нет света.",
            "support:library": "Библиотека закроется раньше в пятницу.",
        },
        evidence={},
    )


def test_binder_returns_full_valid_fact_and_story_coverage() -> None:
    draft = "Power is unavailable.\nThe library will close early."
    response = {
        "paragraphs": [
            {
                "exact_text": "Power is unavailable.",
                "fact_bindings": [
                    {
                        "fact_id": "F001",
                        "support_ids": ["S002"],
                        "status": "fully_supported",
                    }
                ],
                "story_bindings": [
                    {
                        "story_id": "T001",
                        "support_ids": ["S002"],
                        "status": "fully_supported",
                    }
                ],
            },
            {
                "exact_text": "The library will close early.",
                "fact_bindings": [],
                "story_bindings": [
                    {
                        "story_id": "T002",
                        "support_ids": ["S001"],
                        "status": "fully_supported",
                    }
                ],
            },
        ],
        "uncovered_fact_ids": [],
        "uncovered_story_ids": [],
        "unsupported_spans": [],
    }
    provider = FakeProvider(json.dumps(response))

    result = pytest.importorskip("asyncio").run(
        bind_digest_candidate(
            context(),
            draft,
            provider=provider,
            model="openai/gpt-6-luna",
            variant="paragraph_first",
        )
    )

    assert result.status == "binding_ready_for_manual_review"
    assert result.audit is not None and result.audit.has_full_coverage
    assert result.audit.covered_story_ids == ("story:power", "story:library")
    assert result.binding_result is not None
    assert result.binding_result["paragraphs"][0]["fact_bindings"][0]["fact_id"] == "power"
    assert result.max_output_tokens > 1800
    assert provider.calls[0]["model"] == "openai/gpt-6-luna"
    assert provider.calls[0]["response_format"] == {"type": "json_object"}


def test_prompt_treats_fact_text_as_checklist_not_evidence() -> None:
    prompt, _, _ = _build_prompt(context(), "A draft.", "paragraph_first")

    assert "required-fact text is a checklist label, not evidence" in prompt
    assert "parent statement or nearby line cannot add a location" in prompt


def test_unknown_short_id_is_not_repaired_or_counted_as_coverage() -> None:
    response = {
        "paragraphs": [
            {
                "exact_text": "Power is unavailable.",
                "fact_bindings": [
                    {
                        "fact_id": "F999",
                        "support_ids": ["S002"],
                        "status": "fully_supported",
                    }
                ],
                "story_bindings": [
                    {
                        "story_id": "T001",
                        "support_ids": ["S002"],
                        "status": "fully_supported",
                    }
                ],
            }
        ],
        "uncovered_fact_ids": ["F001"],
        "uncovered_story_ids": ["T002"],
        "unsupported_spans": [],
    }
    provider = FakeProvider(json.dumps(response))

    result = pytest.importorskip("asyncio").run(
        bind_digest_candidate(
            context(),
            "Power is unavailable.",
            provider=provider,
            model="openai/gpt-6-luna",
            variant="fact_first",
        )
    )

    assert result.status == "invalid"
    assert result.audit is not None
    assert result.audit.unknown_fact_ids == ("F999",)
    assert result.audit.unaccounted_fact_ids == ()


def test_truncated_or_unparseable_binder_response_is_not_repaired() -> None:
    provider = FakeProvider('{"paragraphs": [')

    result = pytest.importorskip("asyncio").run(
        bind_digest_candidate(
            context(),
            "Power is unavailable.",
            provider=provider,
            model="openai/gpt-6-luna",
            variant="fact_first",
        )
    )

    assert result.status == "failed"
    assert result.binding_result is None
    assert result.audit is None
    assert result.error_code == "BINDER_RESPONSE_UNPARSEABLE"
