# ruff: noqa: S101
"""Digest fact IDs travel to the model as short labels and come back as the real IDs."""

from __future__ import annotations

from types import SimpleNamespace

from src.publication.digest_fact_labels import (
    FACT_LABEL_INSTRUCTION,
    FactLabels,
    fact_labels_for_plan,
)

REAL = "infrastructure_electricity_3f3242cb3364a1acbdfc"


def test_labels_are_numbered_in_input_order_and_round_trip():
    labels = FactLabels([REAL, "fact:2"])
    payload = {"blocks": [{"items": [{"covered_fact_ids": [REAL, "fact:2"]}]}]}
    encoded = labels.encode(payload)
    assert encoded == {"blocks": [{"items": [{"covered_fact_ids": ["F1", "F2"]}]}]}
    assert labels.decode(encoded) == payload


def test_prose_that_only_contains_a_label_is_not_rewritten():
    labels = FactLabels([REAL])
    response = {"body": "Сообщение F1 в тексте", "covered_fact_ids": ["F1"]}
    decoded = labels.decode(response)
    assert decoded == {"body": "Сообщение F1 в тексте", "covered_fact_ids": [REAL]}


def test_unknown_labels_and_real_ids_pass_through_unchanged():
    labels = FactLabels([REAL])
    assert labels.decode({"covered_fact_ids": ["F9", REAL]}) == {"covered_fact_ids": ["F9", REAL]}


def test_fact_ids_inside_validation_text_are_replaced():
    labels = FactLabels([REAL])
    text = f"DIGEST_FACT_COVERAGE_MISSING:{REAL} in block block:infrastructure:composition"
    assert labels.encode_text(text) == (
        "DIGEST_FACT_COVERAGE_MISSING:F1 in block block:infrastructure:composition"
    )


def test_labels_for_a_plan_without_facts_are_empty():
    assert not fact_labels_for_plan(None)
    assert not fact_labels_for_plan(SimpleNamespace(blocks=()))
    plan = SimpleNamespace(
        blocks=(SimpleNamespace(required_facts=(SimpleNamespace(fact_id=REAL),)),)
    )
    labels = fact_labels_for_plan(plan)
    assert bool(labels)
    assert labels.encode([REAL]) == ["F1"]


def test_instruction_tells_the_model_to_copy_labels():
    assert "short labels" in FACT_LABEL_INSTRUCTION
    assert "exactly" in FACT_LABEL_INSTRUCTION
