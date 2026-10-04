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
