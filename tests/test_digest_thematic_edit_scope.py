"""Atomic block scope preserves selected material and rejects stale/foreign edits."""

# ruff: noqa: S101
import asyncio
import json
from dataclasses import replace

import pytest
from digest_evaluation_helpers import assessment_inputs

from src.publication.digest_composition import DigestCompositionUnit
from src.publication.digest_narrative import _parse_composition_writer_output


def with_summary():
    values, draft = assessment_inputs()
    plan = values["plan"]
    b = plan.blocks[0]
    support = next(iter(values["evidence"]))
    summary = DigestCompositionUnit(
        "summary:test", b.rubric_id, (), ("story:summary",), (support,), "", 1
    )
    plan = replace(plan, blocks=(replace(b, composition_units=(*b.composition_units, summary)),))
    item = {
        "composition_unit_ids": [summary.unit_id],
        "covered_fact_ids": [],
        "body": values["evidence"][support].text,
        "headline": "",
        "claims": [
            {
                "text": values["evidence"][support].text,
                "covered_fact_ids": [],
                "summary_unit_ids": [summary.unit_id],
            }
        ],
    }
    raw = {
        "blocks": [
            {
                "block_id": b.block_id,
                "items": [
                    {
                        "composition_unit_ids": list(i.composition_unit_ids),
                        "covered_fact_ids": list(i.covered_fact_ids),
                        "body": i.body,
                        "headline": i.headline,
                        "claims": [],
                    }
                    for i in draft.blocks[0].items
                ]
                + [item],
            }
        ]
    }
    return values, plan, _parse_composition_writer_output(raw, plan=plan)


def test_whole_block_recomposition_preserves_summary_only_story():
    from src.publication.digest_edit_scope import (
        build_digest_block_edit_scope,
        validate_digest_block_replacement,
    )
    from src.publication.digest_editor import DigestEditor

    values, plan, draft = with_summary()
    scope = build_digest_block_edit_scope(draft, plan=plan, block_ids=[draft.blocks[0].block_id])
    b = draft.blocks[0]
    raw = {
        "blocks": [
            {
                "block_id": b.block_id,
                "recomposed_items": [
                    {
                        "composition_unit_ids": [
                            uid for i in b.items for uid in i.composition_unit_ids
                        ],
                        "covered_fact_ids": [fid for i in b.items for fid in i.covered_fact_ids],
                        "body": " ".join(i.body for i in b.items),
                        "claims": [
                            {
                                "text": b.items[-1].body,
                                "covered_fact_ids": [],
                                "summary_unit_ids": ["summary:test"],
                            }
                        ],
                    }
                ],
            }
        ]
    }

    class Provider:
        async def chat_completion(self, **kwargs):
            return json.dumps(raw)

    edited = asyncio.run(
        DigestEditor(Provider()).polish_and_compress(
            draft, plan=plan, evidence=values["evidence"], edit_scope=scope
        )
    )
    validate_digest_block_replacement(draft, edited, scope=scope, plan=plan)
    assert len(edited.blocks[0].items) == 1
    assert "story:summary" in edited.blocks[0].items[0].covered_story_ids


def test_writer_may_defer_an_omitted_summary_unit_to_the_editor():
    from src.publication.digest_edit_scope import (
        build_digest_block_edit_scope,
        validate_digest_block_replacement,
    )
    from src.publication.digest_editor import DigestRecompositionError

    _values, plan, complete = with_summary()
    block = complete.blocks[0]
    raw = {
        "blocks": [
            {
                "block_id": block.block_id,
                "items": [
                    {
                        "composition_unit_ids": list(item.composition_unit_ids),
                        "covered_fact_ids": list(item.covered_fact_ids),
                        "headline": item.headline,
                        "body": item.body,
                        "claims": [],
                    }
                    for item in block.items
                    if "summary:test" not in item.composition_unit_ids
                ],
            }
        ]
    }
    incomplete = _parse_composition_writer_output(
        raw,
        plan=plan,
        allow_incomplete_fact_coverage=True,
        allow_incomplete_summary_coverage=True,
    )
    scope = build_digest_block_edit_scope(
        incomplete, plan=plan, block_ids=[block.block_id]
    )
    validate_digest_block_replacement(
        incomplete,
        incomplete,
        scope=scope,
        plan=plan,
        allow_incomplete_fact_coverage=True,
        allow_incomplete_summary_coverage=True,
    )
    with pytest.raises(DigestRecompositionError, match="MEMBERSHIP"):
        validate_digest_block_replacement(
            incomplete, incomplete, scope=scope, plan=plan
        )


def test_stale_fingerprint_and_lost_fact_rollback_entire_batch():
    from src.publication.digest_edit_scope import (
        build_digest_block_edit_scope,
        validate_digest_block_replacement,
    )
    from src.publication.digest_editor import DigestRecompositionError

    values, draft = assessment_inputs()
    scope = build_digest_block_edit_scope(
        draft, plan=values["plan"], block_ids=[draft.blocks[0].block_id]
    )
    b = draft.blocks[0]
    changed = replace(
        draft, blocks=(replace(b, items=(replace(b.items[0], body="Другой текст"), *b.items[1:])),)
    )
    with pytest.raises(DigestRecompositionError, match="FINGERPRINT"):
        validate_digest_block_replacement(changed, draft, scope=scope, plan=values["plan"])
    missing = replace(draft, blocks=(replace(b, items=b.items[1:]),))
    with pytest.raises(DigestRecompositionError, match="MEMBERSHIP"):
        validate_digest_block_replacement(draft, missing, scope=scope, plan=values["plan"])


def test_unrelated_block_is_unchanged():
    from src.publication.digest_edit_scope import (
        build_digest_block_edit_scope,
        validate_digest_block_replacement,
    )
    from src.publication.digest_editor import DigestRecompositionError

    values, draft = assessment_inputs()
    b = draft.blocks[0]
    extra = replace(b, block_id="untouched")
    base = replace(draft, blocks=(b, extra))
    scope = build_digest_block_edit_scope(base, plan=values["plan"], block_ids=[b.block_id])
    altered = replace(extra, items=(replace(extra.items[0], body="Changed"), *extra.items[1:]))
    with pytest.raises(DigestRecompositionError, match="UNAUTHORIZED"):
        validate_digest_block_replacement(
            base, replace(base, blocks=(b, altered)), scope=scope, plan=values["plan"]
        )


def test_complete_context_budget_skip_before_provider():
    from src.publication.digest_edit_scope import build_digest_block_edit_scope
    from src.publication.digest_editor import DigestEditor, DigestEditorContextBudgetError

    values, draft = assessment_inputs()
    scope = build_digest_block_edit_scope(
        draft, plan=values["plan"], block_ids=[draft.blocks[0].block_id]
    )

    class Provider:
        async def chat_completion(self, **kwargs):
            raise AssertionError("truncated request must not reach provider")

    with pytest.raises(DigestEditorContextBudgetError):
        asyncio.run(
            DigestEditor(Provider()).polish_and_compress(
                draft,
                plan=values["plan"],
                evidence=values["evidence"],
                edit_scope=scope,
                max_context_chars=10,
            )
        )


def test_known_id_without_grounded_claim_is_not_enough():
    from src.publication.digest_assessment import DigestAssessmentContext, assess_digest_candidate

    values, draft = assessment_inputs()
    b = draft.blocks[0]
    bad = replace(
        draft,
        blocks=(
            replace(b, items=(replace(b.items[0], body="На АКЗ напряжение 999 В."), *b.items[1:])),
        ),
    )
    result = assess_digest_candidate(bad, context=DigestAssessmentContext(**values))
    assert not result.is_safe
    assert result.coverage.material_fact_coverage == 1.0


@pytest.mark.parametrize(
    "relative,allowed",
    [
        ("В другом доме ", False),
        ("В соседнем доме ", False),
        ("В другом сообщении ", True),
        ("", True),
    ],
)
def test_unspecified_household_woven_without_invented_area(relative, allowed):
    from pathlib import Path

    from scripts.digest_evaluation.fixtures import load_digest_case
    from src.publication.digest_assessment import assess_digest_candidate

    case = load_digest_case(Path("tests/fixtures/digest_editorial/community_microdetails.json"))
    block = case.context.plan.blocks[0]
    text = " ".join(f.text for f in block.required_facts)
    text = text.replace("Жильцы скинулись", relative + "жильцы скинулись")
    raw = {
        "blocks": [
            {
                "block_id": block.block_id,
                "items": [
                    {
                        "composition_unit_ids": [u.unit_id for u in block.composition_units],
                        "covered_fact_ids": [f.fact_id for f in block.required_facts],
                        "body": text,
                        "claims": [],
                    }
                ],
            }
        ]
    }
    draft = _parse_composition_writer_output(raw, plan=case.context.plan)
    result = assess_digest_candidate(draft, context=case.context)
    assert result.validation.is_valid is allowed, result.validation.violations


def test_explicit_single_source_household_relation_remains_publishable():
    from src.publication.digest_relation_support import (
        find_unsupported_relative_household_relations,
    )

    text = "В соседнем доме жильцы скинулись по 300 рублей."
    assert not find_unsupported_relative_household_relations(text, [text])


def test_thematic_editor_missing_complete_support_context_is_explicit_skip():
    from src.publication.digest_edit_scope import build_digest_block_edit_scope
    from src.publication.digest_editor import DigestEditor, DigestEditorContextMissingError

    values, draft = assessment_inputs()
    scope = build_digest_block_edit_scope(
        draft, plan=values["plan"], block_ids=[draft.blocks[0].block_id]
    )

    class Provider:
        async def chat_completion(self, **kwargs):
            raise AssertionError("incomplete evidence reached provider")

    with pytest.raises(DigestEditorContextMissingError, match="DIGEST_EDITOR_CONTEXT_MISSING"):
        asyncio.run(
            DigestEditor(Provider()).polish_and_compress(
                draft, plan=values["plan"], evidence={}, edit_scope=scope
            )
        )


def test_thematic_prompt_has_one_consistent_complete_scope_contract():
    from src.publication.digest_edit_scope import build_digest_block_edit_scope
    from src.publication.digest_editor import DigestEditor

    values, plan, draft = with_summary()
    scope = build_digest_block_edit_scope(draft, plan=plan, block_ids=[draft.blocks[0].block_id])

    class Provider:
        system = ""

        async def chat_completion(self, **kwargs):
            self.system = kwargs["messages"][0]["content"]
            return "{}"

    provider = Provider()
    with pytest.raises(ValueError):
        asyncio.run(
            DigestEditor(provider).polish_and_compress(
                draft, plan=plan, evidence=values["evidence"], edit_scope=scope
            )
        )
    assert "Keep standalone summary-only items unchanged" not in provider.system
    assert "target_recomposition_summary_unit_ids" in provider.system
    assert "without a fixed paragraph or item quota" in provider.system


def test_text_editor_may_return_only_patched_blocks_and_preserves_others():
    from src.publication.digest_editor import DigestEditor
    from src.publication.digest_narrative import DigestNarrativeBlockDraft

    values, plan, draft = with_summary()
    original_block = draft.blocks[0]
    untouched_block_id = "block:untouched"
    untouched_item = replace(original_block.items[0], item_id="item:untouched")
    untouched_block = DigestNarrativeBlockDraft(
        block_id=untouched_block_id, items=(untouched_item,)
    )
    second_plan_block = replace(plan.blocks[0], block_id=untouched_block_id)
    plan = replace(plan, blocks=(*plan.blocks, second_plan_block))
    draft = replace(draft, blocks=(*draft.blocks, untouched_block))
    target_item = original_block.items[0]

    class Provider:
        async def chat_completion(self, **kwargs):
            return json.dumps(
                {
                    "blocks": [
                        {
                            "block_id": original_block.block_id,
                            "items": [
                                {
                                    "item_id": target_item.item_id,
                                    "headline": "Обновлённый заголовок",
                                    "body": "Уточнённый текст.",
                                }
                            ],
                            "merges": [],
                        }
                    ]
                }
            )

    edited = asyncio.run(
        DigestEditor(Provider()).polish_and_compress(
            draft,
            plan=plan,
            evidence=values["evidence"],
            target_item_ids=[target_item.item_id],
        )
    )

    assert edited.blocks[0].items[0].headline == "Обновлённый заголовок"
    assert edited.blocks[1] == untouched_block


def test_thematic_editor_receives_and_can_restore_writer_omitted_facts():
    from src.publication.digest_edit_scope import (
        build_digest_block_edit_scope,
        validate_digest_block_replacement,
    )
    from src.publication.digest_editor import DigestEditor

    values, complete_draft = assessment_inputs()
    plan = values["plan"]
    plan_block = plan.blocks[0]
    omitted_fact = plan_block.required_facts[-1]
    incomplete_raw = {
        "blocks": [
            {
                "block_id": plan_block.block_id,
                "items": [
                    {
                        "composition_unit_ids": list(item.composition_unit_ids),
                        "covered_fact_ids": [
                            fact_id
                            for fact_id in item.covered_fact_ids
                            if fact_id != omitted_fact.fact_id
                        ],
                        "headline": item.headline,
                        "body": item.body,
                        "claims": [],
                    }
                    for item in complete_draft.blocks[0].items
                    if set(item.covered_fact_ids) != {omitted_fact.fact_id}
                ],
            }
        ]
    }
    from src.publication.digest_narrative import _parse_composition_writer_output

    incomplete = _parse_composition_writer_output(
        incomplete_raw, plan=plan, allow_incomplete_fact_coverage=True
    )
    scope = build_digest_block_edit_scope(
        incomplete, plan=plan, block_ids=[plan_block.block_id]
    )
    fact_unit = {
        str(fact_id): str(unit.unit_id)
        for unit in plan_block.composition_units
        for fact_id in unit.fact_ids
    }
    editor_output = {
        "blocks": [
            {
                "block_id": plan_block.block_id,
                "recomposed_items": [
                    {
                        "composition_unit_ids": [fact_unit[str(fact.fact_id)]],
                        "covered_fact_ids": [str(fact.fact_id)],
                        "headline": "",
                        "body": str(fact.text),
                        "claims": [],
                    }
                    for fact in plan_block.required_facts
                ],
            }
        ]
    }

    class Provider:
        async def chat_completion(self, **kwargs):
            request = "\n".join(message["content"] for message in kwargs["messages"])
            assert str(omitted_fact.fact_id) in request
            assert str(omitted_fact.text) in request
            return json.dumps(editor_output)

    repaired = asyncio.run(
        DigestEditor(Provider()).polish_and_compress(
            incomplete,
            plan=plan,
            evidence=values["evidence"],
            edit_scope=scope,
        )
    )
    validate_digest_block_replacement(incomplete, repaired, scope=scope, plan=plan)
    assert {
        fact_id for item in repaired.blocks[0].items for fact_id in item.covered_fact_ids
    } == {str(fact.fact_id) for fact in plan_block.required_facts}
