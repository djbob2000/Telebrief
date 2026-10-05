"""Exact candidate assessment shares production evidence and render checks."""

# ruff: noqa: S101
from dataclasses import replace

from digest_evaluation_helpers import assessment_inputs


def test_assessment_matches_current_generation_checks():
    from src.publication.digest_assessment import DigestAssessmentContext, assess_digest_candidate

    values, draft = assessment_inputs()
    result = assess_digest_candidate(draft, context=DigestAssessmentContext(**values))
    assert result.validation.is_valid, result.validation.violations
    assert result.coverage.story_coverage == 1.0
    assert result.coverage.material_fact_coverage == 1.0
    assert "62" in result.artifact.visible_text
    assert "15 минут" in result.artifact.visible_text
    assert result.draft == draft
    assert result.artifact.utf16_character_count <= 4096


def test_unsupported_number_is_rejected_without_dropping_single_source():
    from src.publication.digest_assessment import DigestAssessmentContext, assess_digest_candidate

    values, draft = assessment_inputs()
    b = draft.blocks[0]
    item = b.items[0]
    bad = replace(
        draft,
        blocks=(
            replace(b, items=(replace(item, body=item.body + " Напряжение 999 В."), *b.items[1:])),
        ),
    )
    result = assess_digest_candidate(bad, context=DigestAssessmentContext(**values))
    assert not result.validation.is_valid
    assert result.coverage.story_coverage == 1.0
