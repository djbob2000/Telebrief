"""Existing editor handles whole themes within two calls and exact safety checkpoints."""

# ruff: noqa: S101
import asyncio
import json

import pytest
from digest_evaluation_helpers import assessment_inputs

from scripts.digest_evaluation.replay import ReplayObserver
from src.publication.digest_assessment import DigestAssessmentContext, assess_digest_candidate
from src.publication.digest_editor import DigestEditor
from src.publication.generation import _repair_digest_candidate


class Provider:
    def __init__(self, mode="noop"):
        self.calls = 0
        self.mode = mode

    async def chat_completion(self, **kwargs):
        self.calls += 1
        if self.mode == "sleep":
            await asyncio.sleep(1)
        request = json.loads(kwargs["messages"][1]["content"])
        blocks = []
        for b in request["blocks"]:
            items = []
            for i in b["items"]:
                body = i["body"]
                if self.mode == "unsafe":
                    body += " Напряжение 999 В."
                if self.mode == "safe_then_unsafe":
                    body = (
                        "В одном из сообщений говорится: " + body
                        if self.calls == 1
                        else body + " Напряжение 999 В."
                    )
                claims = [
                    {
                        "text": c["text"],
                        "summary_unit_ids": c.get("summary_unit_ids", []),
                        "covered_fact_ids": [],
                    }
                    for c in i["claims"]
                    if c.get("summary_unit_ids")
                ]
                items.append(
                    {
                        "composition_unit_ids": i["composition_unit_ids"],
                        "covered_fact_ids": i["covered_fact_ids"],
                        "headline": i["headline"],
                        "emoji": i["emoji"],
                        "body": body,
                        "claims": claims,
                    }
                )
            blocks.append({"block_id": b["block_id"], "recomposed_items": items})
        return json.dumps({"blocks": blocks})


def run_repair(provider, scope="thematic_blocks", timeout=10, **extra):
    values, draft = assessment_inputs()
    context = DigestAssessmentContext(**values)
    initial = assess_digest_candidate(draft, context=context)
    observer = ReplayObserver()
    result = asyncio.run(
        _repair_digest_candidate(
            checkpoint=initial.checkpoint(),
            plan=context.plan,
            evidence=context.evidence,
            editor=DigestEditor(provider),
            observer=observer,
            evaluate_candidate=lambda d: assess_digest_candidate(d, context=context).checks(),
            model="test",
            timeout_seconds=timeout,
            implementation_versions={},
            editor_scope=scope,
            **extra,
        )
    )
    return initial, result, observer


def test_zero_warnings_still_get_one_editorial_call_in_thematic_mode():
    provider = Provider()
    initial, result, observer = run_repair(provider)
    assert provider.calls == 1
    assert result[0][0] == initial.draft
    assert observer.outcomes[0]["editor_outcome"] == "unchanged_safe"


def test_legacy_mode_zero_warnings_still_skips_editor():
    provider = Provider()
    _, result, _ = run_repair(provider, scope="targeted_items")
    assert provider.calls == 0
    assert result[2] == 0


def test_noop_safe_edit_does_not_force_second_call():
    provider = Provider()
    _, result, _ = run_repair(provider)
    assert result[2] == 1
    assert provider.calls == 1


def test_second_unsafe_edit_retains_first_exact_assessed_checkpoint():
    provider = Provider("safe_then_unsafe")
    _, result, observer = run_repair(provider)
    assert provider.calls == 2
    assert "999" not in result[0][3].visible_text
    assert result[0][1].is_valid
    assert [v["editor_outcome"] for v in observer.outcomes] == [
        "accepted_change",
        "rejected_unsafe",
    ]


def test_total_editor_calls_never_exceed_two():
    provider = Provider("unsafe")
    initial, result, _ = run_repair(provider)
    assert provider.calls == 2
    assert result[0] == initial.checkpoint()


def test_timeout_and_cancellation_follow_existing_generation_contract():
    with pytest.raises(TimeoutError):
        run_repair(Provider("sleep"), timeout=0.01)


def test_skipped_context_budget_returns_existing_checkpoint_for_final_gate():
    provider = Provider()
    initial, result, observer = run_repair(provider, max_context_chars=10)
    assert result[0] == initial.checkpoint()
    assert provider.calls == 0
    assert observer.outcomes[0]["editor_outcome"] == "skipped_context_budget"


def test_unknown_scope_config_rejected():
    from src.config.schemas.publication import PublicationEditorialConfig

    with pytest.raises(ValueError, match="digest_editor_scope"):
        PublicationEditorialConfig(digest_editor_scope="typo")
