"""Repeated measurements are local repair cues, never publication vetoes."""

# ruff: noqa: S101
from dataclasses import replace

from digest_evaluation_helpers import assessment_inputs
from test_digest_synthesis import _fixture

from src.publication.digest_quality_diagnostics import audit_digest_prose_quality


def test_unique_supported_voltage_repeated_in_another_item_requests_local_repair():
    _, evidence, presentation, _ = _fixture(
        ("electricity", "", "Житель сообщает о напряжении 80 - 60 вольт.")
    )
    _, draft = assessment_inputs()
    block = draft.blocks[0]
    items = tuple(
        replace(item, body=item.body + " Напряжение 80–60 В.") if index < 2 else item
        for index, item in enumerate(block.items)
    )
    candidate = replace(draft, blocks=(replace(block, items=items),))
    audit = audit_digest_prose_quality(candidate, evidence, presentation)
    repeated = [w for w in audit.warnings if w.code == "REPEATED_SUPPORTED_MEASUREMENT"]
    assert {w.item_index for w in repeated} == {0, 1}


def test_equal_voltage_from_distinct_facts_does_not_imply_repetition():
    _, evidence, presentation, _ = _fixture(
        ("electricity", "Баха", "На Баха напряжение 154 В."),
        ("electricity", "Крылова", "На Крылова напряжение 154 В."),
    )
    _, draft = assessment_inputs()
    block = draft.blocks[0]
    items = tuple(replace(item, body=item.body + " Напряжение 154 В.") for item in block.items)
    candidate = replace(draft, blocks=(replace(block, items=items),))
    assert not any(
        w.code == "REPEATED_SUPPORTED_MEASUREMENT"
        for w in audit_digest_prose_quality(candidate, evidence, presentation).warnings
    )
