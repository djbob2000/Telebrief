"""Style observations do not certify truth or veto a safe digest."""

# ruff: noqa: S101
from dataclasses import replace

from digest_evaluation_helpers import assessment_inputs


def test_named_chat_meta_is_advisory():
    from src.publication.digest_quality_diagnostics import assess_digest_editorial_readiness

    _, draft = assessment_inputs()
    block = draft.blocks[0]
    draft = replace(
        draft,
        blocks=(
            replace(
                block,
                items=(
                    replace(
                        block.items[0],
                        body="По сообщению в бердянском чате, в городе заполняют отопление.",
                    ),
                ),
            ),
        ),
    )
    readiness = assess_digest_editorial_readiness(draft)
    assert "SOURCE_PROCESS_DESCRIPTION" in {w.code for w in readiness.observations}
    assert "бердянском" not in str(readiness.as_metadata())
    assert readiness.semantic_review_status == "not_evaluated"


def test_safe_single_sentence_is_not_fragmentation_blocker():
    from src.publication.digest_quality_diagnostics import assess_digest_editorial_readiness

    _, draft = assessment_inputs()
    assert "FRAGMENTED_SERVICE_REPORTS" not in {
        w.code for w in assess_digest_editorial_readiness(draft).observations
    }


def test_zero_observations_is_not_semantic_quality_certificate():
    from src.publication.digest_quality_diagnostics import assess_digest_editorial_readiness

    _, draft = assessment_inputs()
    assert assess_digest_editorial_readiness(draft).semantic_review_status == "not_evaluated"


def test_readiness_does_not_change_coverage_or_publishability():
    from src.publication.digest_assessment import DigestAssessmentContext, assess_digest_candidate
    from src.publication.digest_quality_diagnostics import assess_digest_editorial_readiness

    values, draft = assessment_inputs()
    before = assess_digest_candidate(draft, context=DigestAssessmentContext(**values))
    assess_digest_editorial_readiness(draft)
    after = assess_digest_candidate(draft, context=DigestAssessmentContext(**values))
    assert before == after
    assert after.is_safe
