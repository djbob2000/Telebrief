"""Sealed JSON inputs never rehydrate from mutable runtime state."""

# ruff: noqa: S101
import json
from dataclasses import replace

import pytest
from digest_evaluation_helpers import assessment_inputs


def test_case_roundtrip_preserves_all_writer_inputs(tmp_path):
    from scripts.digest_evaluation.fixtures import (
        FrozenDigestCase,
        load_digest_case,
        write_digest_case,
    )
    from src.publication.digest_assessment import DigestAssessmentContext

    values, _ = assessment_inputs()
    key = next(iter(values["evidence"]))
    values["evidence"][key] = replace(
        values["evidence"][key], reply_parent_context_text="Точный контекст «не исправлять»"
    )
    case = FrozenDigestCase(
        "rich",
        DigestAssessmentContext(**values),
        {"language": "Russian", "model": None, "max_output_tokens": 4096, "timeout_seconds": 120},
        {"writer": "test"},
    )
    path = tmp_path / "case.json"
    original_hash = write_digest_case(case, path)
    loaded = load_digest_case(path)
    assert loaded.context.plan == case.context.plan
    assert loaded.context.presentation_plan == case.context.presentation_plan
    assert loaded.context.evidence == case.context.evidence
    assert loaded.context.frozen == case.context.frozen
    assert loaded.context.renderer.rubrics == case.context.renderer.rubrics
    assert write_digest_case(loaded, path) == original_hash
    changed = replace(loaded, generation={**loaded.generation, "language": "Ukrainian"})
    assert write_digest_case(changed, path) != original_hash


def test_missing_required_input_rejected_before_provider_call(tmp_path):
    from scripts.digest_evaluation.fixtures import load_digest_case

    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"version": "digest-case-v1", "name": "incomplete"}))
    with pytest.raises(ValueError, match="DIGEST_CASE"):
        load_digest_case(path)
