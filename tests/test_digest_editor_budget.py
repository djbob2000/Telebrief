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
        self.requests = []

    async def chat_completion(self, **kwargs):
        self.calls += 1
        if self.mode == "sleep":
            await asyncio.sleep(1)
        request = json.loads(kwargs["messages"][1]["content"])
        self.requests.append(request)
        blocks = []
        for b in request["blocks"]:
            fact_rows = [
                fact
                for fact in request.get("required_recomposition_facts", [])
                if fact.get("block_id") == b["block_id"]
            ]
            if b.get("allow_recomposition") and fact_rows:
                facts_by_id = {fact["fact_id"]: fact for fact in fact_rows}
                grouped_ids: list[list[str]] = []
                used_fact_ids: set[str] = set()
                for group in b["reader_synthesis_groups"]:
                    ids = [
                        str(fact_id)
                        for fact_id in group["fact_ids"]
                        if str(fact_id) in facts_by_id and str(fact_id) not in used_fact_ids
                    ]
                    if ids:
                        grouped_ids.append(ids)
                        used_fact_ids.update(ids)
                grouped_ids.extend(
                    [[fact_id]] for fact_id in facts_by_id if fact_id not in used_fact_ids
                )
                items = []
                for item_index, fact_ids in enumerate(grouped_ids):
                    body = " ".join(facts_by_id[fact_id]["text"] for fact_id in fact_ids)
                    if (
                        self.mode in ("local", "no_progress", "safe_then_unsafe")
                        and self.calls == 1
                        and item_index == 0
                    ):
                        body = "В одном из сообщений говорится: " + body
                    if self.mode == "repeat_voltage" and "80 - 60" in body:
                        body += " Напряжение 80–60 В."
                    if self.mode == "unsafe" or self.mode == "safe_then_unsafe" and self.calls > 1:
                        body += " Напряжение 999 В."
                    items.append(
                        {
                            "composition_unit_ids": [],
                            "covered_fact_ids": fact_ids,
                            "headline": "",
                            "emoji": "",
                            "body": body,
                            "claims": [],
                        }
                    )
                for item in b["items"]:
                    if item.get("summary_units"):
                        items.append(
                            {
                                "composition_unit_ids": item["composition_unit_ids"],
                                "covered_fact_ids": [],
                                "headline": item["headline"],
                                "emoji": item["emoji"],
                                "body": item["body"],
                                "claims": [
                                    {
                                        "text": claim["text"],
                                        "summary_unit_ids": claim.get("summary_unit_ids", []),
                                        "covered_fact_ids": [],
                                    }
                                    for claim in item["claims"]
                                    if claim.get("summary_unit_ids")
                                ],
                            }
                        )
                blocks.append(
                    {
                        "block_id": b["block_id"],
                        "recomposed_items": items,
                    }
                )
                continue
            items = []
            for index, i in enumerate(b["items"]):
                body = i["body"]
                if self.mode == "repeat_voltage" and "80 - 60" in body:
                    body += " Напряжение 80–60 В."
                if self.mode == "unsafe":
                    body += " Напряжение 999 В."
                if self.mode in ("local", "no_progress") and self.calls == 1 and index == 0:
                    body = "В одном из сообщений говорится: " + body
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
            if self.calls > 1 and not request["target_recomposition_fact_ids"]:
                patches = [
                    {
                        "item_id": i["item_id"],
                        "headline": i["headline"],
                        "emoji": i["emoji"],
                        "body": ("Также " if self.mode == "no_progress" else "")
                        + i["body"]
                        + (
                            " Напряжение 999 В."
                            if self.mode in ("unsafe", "safe_then_unsafe")
                            else ""
                        ),
                    }
                    for i in b["items"]
                    if i["item_id"] in request["target_item_ids"]
                ]
                blocks.append({"block_id": b["block_id"], "items": patches})
            else:
                blocks.append({"block_id": b["block_id"], "recomposed_items": items})
        return json.dumps({"blocks": blocks})


def run_repair(provider, scope="thematic_blocks", timeout=10, **extra):
    values, draft = extra.pop("inputs", None) or assessment_inputs()
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
            evaluate_candidate=extra.pop(
                "evaluate_candidate", lambda d: assess_digest_candidate(d, context=context).checks()
            ),
            model="test",
            timeout_seconds=timeout,
            implementation_versions={},
            editor_scope=scope,
            review_without_findings=extra.pop("review_without_findings", True),
            **extra,
        )
    )
    return initial, result, observer


def test_zero_warnings_still_get_one_editorial_call_in_thematic_mode():
    provider = Provider()
    _initial, result, observer = run_repair(provider)
    assert provider.calls == 1
    assert result[0][1].is_valid
    assert result[0][2].story_coverage == result[0][2].material_fact_coverage == 1.0
    assert observer.outcomes[0]["editor_outcome"] == "accepted_change"


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


def test_second_no_progress_edit_retains_first_exact_assessed_checkpoint():
    provider = Provider("safe_then_unsafe")
    _, result, observer = run_repair(provider)
    assert provider.calls == 3
    assert "999" not in result[0][3].visible_text
    assert result[0][1].is_valid
    assert [v["editor_outcome"] for v in observer.outcomes] == [
        "accepted_change",
        "rejected_editorial_no_progress",
        "rejected_editorial_no_progress",
    ]


def test_total_editor_calls_never_exceed_three():
    provider = Provider("unsafe")
    _, result, observer = run_repair(provider)
    assert provider.calls <= 3
    assert len(observer.outcomes) <= 3
    assert result[0][1].is_valid
    assert result[0][2].story_coverage == result[0][2].material_fact_coverage == 1.0


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


def test_production_thematic_mode_does_not_pay_for_clean_writer():
    provider = Provider()
    _, result, _ = run_repair(provider, review_without_findings=False)
    assert provider.calls == 0
    assert result[2] == 0


def test_production_thematic_findings_authorize_the_complete_block(monkeypatch):
    from src.publication import generation

    original = generation._digest_repair_request

    def request(checkpoint):
        findings, targets, blocks = original(checkpoint)
        return findings + ["STYLE_OBSERVATION: repair this theme"], targets, blocks

    monkeypatch.setattr(generation, "_digest_repair_request", request)
    provider = Provider()
    _, result, observer = run_repair(provider, review_without_findings=False)
    assert provider.calls == 1
    assert result[0][1].is_valid
    assert result[0][2].story_coverage == result[0][2].material_fact_coverage == 1.0
    assert observer.outcomes[0]["editor_outcome"] == "accepted_change"


def test_thematic_editor_receives_the_same_reporting_navigation_as_writer():
    from dataclasses import replace

    from src.publication.digest_composition import (
        DigestFactRelation,
        DigestFactRelationKind,
    )
    from src.publication.digest_narrative import (
        _reader_synthesis_groups,
        _related_reporting_sets,
        _same_situation_groups,
    )

    values, draft = assessment_inputs()
    first_block = values["plan"].blocks[0]
    fact_ids = [str(fact.fact_id) for fact in first_block.required_facts]
    assert len(fact_ids) >= 2
    relation = DigestFactRelation(
        left_fact_id=fact_ids[0],
        right_fact_id=fact_ids[1],
        kind=DigestFactRelationKind.SAME_SITUATION,
        reason="same resolved service state",
    )
    values["plan"] = replace(
        values["plan"],
        blocks=(
            replace(
                first_block,
                composition_relations=(*first_block.composition_relations, relation),
            ),
            *values["plan"].blocks[1:],
        ),
    )

    provider = Provider()
    initial, result, _ = run_repair(provider, inputs=(values, draft))
    plan_blocks = {block.block_id: block for block in values["plan"].blocks}
    for block in provider.requests[0]["blocks"]:
        planned = plan_blocks[block["block_id"]]
        assert block["reader_synthesis_groups"] == _reader_synthesis_groups(planned)
        assert block["related_reporting_sets"] == _related_reporting_sets(planned)
        assert block["same_situation_groups"] == _same_situation_groups(planned)
        assert all("observed_time" not in fact for item in block["items"] for fact in item["facts"])
        assert {row["body"] for row in block["prior_draft_context"]} == {
            item.body for item in initial.draft.blocks[0].items
        }
    assert result[0][1].is_valid
    assert result[0][2].story_coverage == result[0][2].material_fact_coverage == 1.0
    assert provider.calls == 1


def test_thematic_editor_receives_actual_publish_sources_for_each_item():
    provider = Provider()
    values, _draft = assessment_inputs()
    plan_blocks = {block.block_id: block for block in values["plan"].blocks}
    run_repair(provider)
    for block in provider.requests[0]["blocks"]:
        planned = plan_blocks[block["block_id"]]
        records = {str(record.fact_id): record for record in planned.composition_fact_records}
        for fact in (
            row
            for row in provider.requests[0]["required_recomposition_facts"]
            if row["block_id"] == block["block_id"]
        ):
            sources = fact["supports"]
            assert {row["support_id"] for row in sources} == set(
                records[fact["fact_id"]].support_ids
            )
            assert all(row["texts"] and all(row["texts"]) for row in sources)
            assert all(row["publication_use"] == "PUBLISH" for row in sources)


def test_second_editor_call_recomposes_when_checkpoint_is_still_unsafe():
    from dataclasses import replace

    values, draft = assessment_inputs()
    context = DigestAssessmentContext(**values)
    initial = assess_digest_candidate(draft, context=context)
    unsafe_checkpoint = list(initial.checkpoint())
    unsafe_checkpoint[1] = replace(
        unsafe_checkpoint[1],
        is_valid=False,
        violations=("TEST_UNSAFE_CHECKPOINT: structural recovery required",),
    )

    class RecoveringProvider:
        def __init__(self):
            self.calls = 0
            self.requests = []

        async def chat_completion(self, **kwargs):
            self.calls += 1
            request = json.loads(kwargs["messages"][1]["content"])
            self.requests.append(request)
            fact_ids = request["target_recomposition_fact_ids"]
            if self.calls == 1:
                return json.dumps(
                    {
                        "blocks": [
                            {
                                "block_id": request["blocks"][0]["block_id"],
                                "recomposed_items": [
                                    {
                                        "covered_fact_ids": [
                                            fact_ids[0],
                                            fact_ids[0],
                                            *fact_ids[1:],
                                        ],
                                        "body": "повтор факта",
                                        "claims": [],
                                    }
                                ],
                            }
                        ]
                    }
                )
            facts = request["required_recomposition_facts"]
            return json.dumps(
                {
                    "blocks": [
                        {
                            "block_id": request["blocks"][0]["block_id"],
                            "recomposed_items": [
                                {
                                    "covered_fact_ids": fact_ids,
                                    "body": " ".join(fact["text"] for fact in facts),
                                    "claims": [],
                                }
                            ],
                        }
                    ]
                }
            )

    provider = RecoveringProvider()
    observer = ReplayObserver()
    checkpoint, used, calls = asyncio.run(
        _repair_digest_candidate(
            checkpoint=tuple(unsafe_checkpoint),
            plan=context.plan,
            evidence=context.evidence,
            editor=DigestEditor(provider),
            observer=observer,
            evaluate_candidate=lambda draft: assess_digest_candidate(
                draft, context=context
            ).checks(),
            model="test",
            timeout_seconds=10,
            implementation_versions={},
            editor_scope="thematic_blocks",
            review_without_findings=True,
        )
    )

    assert provider.calls == calls == 2
    assert all(request["target_recomposition_fact_ids"] for request in provider.requests)
    assert checkpoint[2].story_coverage == checkpoint[2].material_fact_coverage == 1.0
    assert checkpoint[1].is_valid and checkpoint[4].is_publishable
    assert used
    assert observer.outcomes[0]["editor_outcome"] == "invalid_response"


def test_second_editor_call_recomposes_remaining_structural_style_findings():
    from dataclasses import replace

    from src.publication.digest_narrative import DIGEST_ITEM_BODY_MAX_CHARS
    from src.publication.digest_quality_diagnostics import DigestQualityWarning

    values, draft = assessment_inputs()
    context = DigestAssessmentContext(**values)
    block_id = draft.blocks[0].block_id

    class RepackingProvider:
        def __init__(self):
            self.calls = 0
            self.requests = []
            self.system_prompts = []

        async def chat_completion(self, **kwargs):
            self.calls += 1
            self.system_prompts.append(kwargs["messages"][0]["content"])
            request = json.loads(kwargs["messages"][1]["content"])
            self.requests.append(request)
            rows = request["required_recomposition_facts"]
            items = (
                [
                    {
                        "covered_fact_ids": [row["fact_id"]],
                        "body": row["text"],
                        "claims": [],
                    }
                    for row in rows
                ]
                if self.calls == 1
                else [
                    {
                        "covered_fact_ids": [row["fact_id"] for row in rows],
                        "body": " ".join(row["text"] for row in rows),
                        "claims": [],
                    }
                ]
            )
            return json.dumps({"blocks": [{"block_id": block_id, "recomposed_items": items}]})

    provider = RepackingProvider()
    observer = ReplayObserver()
    assessment_calls = 0

    def evaluate(candidate):
        nonlocal assessment_calls
        assessment_calls += 1
        checks = assess_digest_candidate(candidate, context=context).checks()
        if assessment_calls == 1:
            validation, coverage, artifact, audit = checks
            warning = DigestQualityWarning(
                code="FRAGMENTED_SERVICE_REPORTS",
                message="group related service reports",
                block_id=block_id,
            )
            audit = replace(
                audit,
                prose_audit=replace(audit.prose_audit, warnings=(warning,)),
            )
            return validation, coverage, artifact, audit
        return checks

    checkpoint, _used, calls = asyncio.run(
        _repair_digest_candidate(
            checkpoint=assess_digest_candidate(draft, context=context).checkpoint(),
            plan=context.plan,
            evidence=context.evidence,
            editor=DigestEditor(provider),
            observer=observer,
            evaluate_candidate=evaluate,
            model="test",
            timeout_seconds=10,
            implementation_versions={},
            editor_scope="thematic_blocks",
            review_without_findings=True,
        )
    )

    assert provider.calls == calls == 2
    expected = {str(fact.fact_id) for fact in context.plan.blocks[0].required_facts}
    assert set(provider.requests[1]["target_recomposition_fact_ids"]) == expected
    assert provider.requests[1]["blocks"][0]["allow_recomposition"] is True
    assert (
        f"Each replacement item body must stay within {DIGEST_ITEM_BODY_MAX_CHARS} characters; "
        "split a longer synthesis into coherent items without dropping or truncating any fact."
        in provider.system_prompts[1]
    )
    assert checkpoint[2].material_fact_coverage == 1.0


def test_editor_cannot_introduce_new_fare_ambiguity_into_safe_checkpoint():
    from dataclasses import replace

    from src.publication.digest_quality_diagnostics import DigestQualityWarning

    values, base = assessment_inputs()
    context = DigestAssessmentContext(**values)
    original = assess_digest_candidate(base, context=context)

    # The detector itself is covered in test_digest_fare_context. Exercise the
    # editor's acceptance policy with an otherwise safe, changed candidate.
    def evaluate(candidate):
        assessment = assess_digest_candidate(candidate, context=context)
        if candidate != original.draft:
            warning = DigestQualityWarning(
                code="AMBIGUOUS_PASSING_BUS_FARE",
                message="paid leg became ambiguous",
                block_id=candidate.blocks[0].block_id,
                item_index=0,
            )
            assessment = replace(
                assessment,
                audit=replace(
                    assessment.audit,
                    prose_audit=replace(
                        assessment.audit.prose_audit,
                        warnings=assessment.audit.prose_audit.warnings + (warning,),
                    ),
                ),
            )
        return assessment.checks()

    provider = Provider("safe_then_unsafe")
    initial, result, observer = run_repair(provider, evaluate_candidate=evaluate)
    assert result[0][0] == initial.draft
    assert result[0][1].is_valid
    assert result[0][4].is_publishable
    assert not result[1]
    assert provider.calls == 2
    assert observer.outcomes[0]["editor_outcome"] == "rejected_editorial_regression"


def test_existing_fare_advisory_does_not_prevent_recovery_from_unsafe_writer():
    from dataclasses import replace

    from src.publication.digest_quality_diagnostics import DigestQualityWarning

    values, base = assessment_inputs()
    context = DigestAssessmentContext(**values)
    original = assess_digest_candidate(base, context=context)
    unsafe = replace(
        original.validation, is_valid=False, violations=("UNSUPPORTED_CONCRETE_CLAIM:test",)
    )
    checkpoint = (original.draft, unsafe, original.coverage, original.artifact, original.audit)

    def evaluate(candidate):
        assessment = assess_digest_candidate(candidate, context=context)
        warning = DigestQualityWarning(
            code="AMBIGUOUS_PASSING_BUS_FARE",
            message="clarify paid leg",
            block_id=candidate.blocks[0].block_id,
            item_index=0,
        )
        audit = replace(
            assessment.audit,
            prose_audit=replace(
                assessment.audit.prose_audit,
                warnings=assessment.audit.prose_audit.warnings + (warning,),
            ),
        )
        return assessment.validation, assessment.coverage, assessment.artifact, audit

    provider = Provider("safe_then_unsafe")
    observer = ReplayObserver()
    result = asyncio.run(
        _repair_digest_candidate(
            checkpoint=checkpoint,
            plan=context.plan,
            evidence=context.evidence,
            editor=DigestEditor(provider),
            observer=observer,
            evaluate_candidate=evaluate,
            model="test",
            timeout_seconds=10,
            implementation_versions={},
            editor_scope="thematic_blocks",
            review_without_findings=True,
        )
    )
    assert result[1]
    assert result[0][1].is_valid
    assert result[0][4].is_publishable
    assert result[0][0] != original.draft
    assert provider.calls == 3
    assert observer.outcomes[0]["editor_outcome"] == "accepted_change"


def test_final_editor_passes_are_local_and_do_not_recompose():
    provider = Provider("safe_then_unsafe")
    run_repair(provider)
    assert provider.calls == 3
    second = provider.requests[1]
    assert second["target_recomposition_fact_ids"] == []
    assert second["target_recomposition_summary_unit_ids"] == []
    third = provider.requests[2]
    assert third["target_recomposition_fact_ids"] == []
    assert third["target_recomposition_summary_unit_ids"] == []


def test_editor_cannot_duplicate_one_supported_voltage_in_safe_checkpoint():
    provider = Provider("repeat_voltage")
    initial, result, observer = run_repair(
        provider,
        inputs=assessment_inputs(
            ("electricity", "", "Житель сообщает о напряжении 80 - 60 вольт.")
        ),
    )
    assert result[0] == initial.checkpoint()
    assert observer.outcomes[0]["editor_outcome"] == "rejected_editorial_regression"
    assert result[0][4].is_publishable


def test_local_second_pass_preserves_untargeted_items_exactly():
    provider = Provider("local")
    _, result, _ = run_repair(provider)
    assert provider.calls == 2
    request = provider.requests[1]
    targets = set(request["target_item_ids"])
    assert targets
    untouched = [
        item
        for block in request["blocks"]
        for item in block["items"]
        if item["item_id"] not in targets
    ]
    assert untouched
    final_items = {item.item_id: item for block in result[0][0].blocks for item in block.items}
    for original in untouched:
        final = final_items[original["item_id"]]
        assert (final.headline, final.body, final.emoji) == (
            original["headline"],
            original["body"],
            original["emoji"],
        )


def test_local_edit_with_unresolved_targeted_issue_does_not_replace_checkpoint():
    provider = Provider("no_progress")
    _, result, observer = run_repair(provider)
    assert provider.calls == 3
    assert observer.outcomes[1]["editor_outcome"] == "rejected_editorial_no_progress"
    assert observer.outcomes[2]["editor_outcome"] == "rejected_editorial_no_progress"
    assert not any(
        item.body.startswith("Также ") for block in result[0][0].blocks for item in block.items
    )
    assert result[0][4].is_publishable
