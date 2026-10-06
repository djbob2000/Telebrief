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


def test_generic_power_labels_request_regrouping_without_blocking_publication():
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality

    values, draft = assessment_inputs()
    block = draft.blocks[0]
    power_items = block.items[:2]
    draft = replace(
        draft,
        blocks=(
            replace(
                block,
                items=(
                    replace(power_items[0], headline="Электричество"),
                    replace(power_items[1], headline="Электричество в городе"),
                    *block.items[2:],
                ),
            ),
        ),
    )
    audit = audit_digest_prose_quality(draft, values["evidence"])
    warnings = [w for w in audit.warnings if w.code == "FRAGMENTED_SERVICE_REPORTS"]
    assert {w.item_index for w in warnings} == {0, 1}
    assert audit.is_publishable
    from src.publication.digest_assessment import DigestAssessmentContext, assess_digest_candidate
    from src.publication.generation import _digest_repair_request

    assessment = assess_digest_candidate(draft, context=DigestAssessmentContext(**values))
    _, targets, recompose_blocks = _digest_repair_request(assessment.checkpoint())
    assert set(targets) == {item.item_id for item in power_items}
    assert recompose_blocks == (block.block_id,)


def test_distinct_power_developments_are_not_duplicates_merely_by_service():
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality

    values, draft = assessment_inputs()
    block = draft.blocks[0]
    draft = replace(
        draft,
        blocks=(
            replace(
                block,
                items=(
                    replace(block.items[0], headline="Краткое включение на АКЗ"),
                    replace(block.items[1], headline="Затяжное отключение на Крылова"),
                ),
            ),
        ),
    )
    audit = audit_digest_prose_quality(draft, values["evidence"])
    assert "FRAGMENTED_SERVICE_REPORTS" not in {w.code for w in audit.warnings}


def test_large_power_synthesis_group_split_over_three_items_requests_recomposition():
    from src.publication.digest_quality_diagnostics import audit_digest_prose_quality

    reports = tuple(
        ("electricity", f"район {index}", f"В районе {index} Бердянска света нет двое суток.")
        for index in range(4, 13)
    )
    values, draft = assessment_inputs(*reports)
    plan_block = values["plan"].blocks[0]
    draft_block = draft.blocks[0]
    facts = [fact for fact in plan_block.required_facts if fact.subject_key == "electricity"]
    units_by_fact = {
        fact_id: unit.unit_id for unit in plan_block.composition_units for fact_id in unit.fact_ids
    }
    items_by_fact = {
        fact_id: item for item in draft_block.items for fact_id in item.covered_fact_ids
    }
    grouped_items = []
    for pair_index in range(0, len(facts), 2):
        pair = facts[pair_index : pair_index + 2]
        source_item = items_by_fact[pair[0].fact_id]
        grouped_items.append(
            replace(
                source_item,
                item_id=f"power-pair-{pair_index}",
                headline="",
                body=" ".join(fact.text for fact in pair),
                covered_fact_ids=tuple(fact.fact_id for fact in pair),
                composition_unit_ids=tuple(units_by_fact[fact.fact_id] for fact in pair),
                covered_story_ids=tuple(
                    dict.fromkeys(story_id for fact in pair for story_id in fact.story_ids)
                ),
                cited_support_ids=tuple(
                    dict.fromkeys(support_id for fact in pair for support_id in fact.support_ids)
                ),
                claims=(),
            )
        )
    water_items = [
        item
        for item in draft_block.items
        if not set(item.covered_fact_ids).intersection(fact.fact_id for fact in facts)
    ]
    candidate = replace(
        draft,
        blocks=(replace(draft_block, items=tuple(grouped_items + water_items)),),
    )

    audit = audit_digest_prose_quality(
        candidate,
        values["evidence"],
        values["presentation_plan"],
    )
    warnings = [w for w in audit.warnings if w.code == "FRAGMENTED_SERVICE_REPORTS"]
    assert {warning.item_index for warning in warnings} == set(range(len(grouped_items)))
    assert audit.is_publishable
