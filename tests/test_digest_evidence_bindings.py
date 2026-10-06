"""Check actual prose against the owners of source-backed details."""

# ruff: noqa: S101

import pytest
from test_digest_composition_identity import _story

from src.domain.service_taxonomy import detect_service_families
from src.publication.digest_composition import (
    DigestCompositionResult,
    DigestCompositionUnit,
    DigestFactRecord,
)
from src.publication.digest_narrative import (
    _composition_narrative_plan,
    _parse_composition_writer_output,
    validate_digest_narrative,
)
from src.publication.digest_presentation import DigestPresentationPlan, RequiredDigestFact

FARE = (
    "Билет на автобус Бердянск-Мелитополь стоит 800 рублей, "
    "а на проходящих автобусах (Ростов, Москва) — 1000 рублей."
)


def binding_inputs(sources, *, locations=None, states=None, canonical_services=None):
    locations = locations or [""] * len(sources)
    states = states or [""] * len(sources)
    canonical_services = canonical_services or [""] * len(sources)
    stories, facts, records, units, supports = [], [], [], [], {}
    for index, (text, location, state, canonical_service) in enumerate(
        zip(sources, locations, states, canonical_services, strict=True)
    ):
        services = detect_service_families(text)
        canonical_service = canonical_service or (
            next(iter(services)) if len(services) == 1 else ""
        )
        story = _story(f"story:{index}", "infrastructure", f"source:{index}")
        fact = RequiredDigestFact(
            fact_id=f"fact:{index}",
            rubric_id="infrastructure",
            subject_key="service",
            subject_label="Служба",
            story_ids=(story.id,),
            support_ids=(f"source:{index}",),
            text=text,
            original_location=location,
        )
        record = DigestFactRecord(
            fact_id=fact.fact_id,
            story_ids=fact.story_ids,
            support_ids=fact.support_ids,
            rubric_id=fact.rubric_id,
            canonical_subject="service",
            canonical_service=canonical_service,
            canonical_area="",
            canonical_place=(),
            original_location=location,
            effective_time=None,
            observed_time=None,
            service_state=state,
            epistemic_kind="community_report",
            source_publication_time=None,
            text=text,
        )
        unit = DigestCompositionUnit(
            unit_id=f"unit:{index}",
            rubric_id=fact.rubric_id,
            fact_ids=(fact.fact_id,),
            story_ids=fact.story_ids,
            support_ids=fact.support_ids,
            canonical_area_key="",
            priority=40,
        )
        stories.append(story)
        facts.append(fact)
        records.append(record)
        units.append(unit)
        supports[f"source:{index}"] = text
    composition = DigestCompositionResult(
        units=tuple(units),
        relations=(),
        dispositions=(),
        admitted_story_ids=frozenset(s.id for s in stories),
        admitted_fact_ids=frozenset(f.fact_id for f in facts),
        estimated_visible_character_count=200,
        fact_records=tuple(records),
    )
    presentation = DigestPresentationPlan(
        story_ids=tuple(s.id for s in stories),
        required_facts=tuple(facts),
        composition=composition,
    )
    plan = _composition_narrative_plan(
        cards=stories,
        rubrics=({"id": "infrastructure", "title": "Обстановка"},),
        presentation_plan=presentation,
        edition_slug="berdyansk",
    )
    return plan, supports


def validate_body(
    body, sources, *, locations=None, states=None, canonical_services=None, missing_support=False
):
    plan, supports = binding_inputs(
        sources, locations=locations, states=states, canonical_services=canonical_services
    )
    block = plan.blocks[0]
    draft = _parse_composition_writer_output(
        {
            "blocks": [
                {
                    "block_id": block.block_id,
                    "items": [
                        {
                            "composition_unit_ids": [u.unit_id for u in block.composition_units],
                            "covered_fact_ids": [f.fact_id for f in block.required_facts],
                            "headline": "",
                            "body": body,
                            "claims": [],
                        }
                    ],
                }
            ]
        },
        plan=plan,
    )
    if missing_support:
        supports = {}
    return validate_digest_narrative(draft, plan, support_text_by_id=supports)


@pytest.mark.parametrize(
    "body",
    [
        "Билет до Москвы стоит 1000 рублей, билет до Мелитополя — 800 рублей.",
        "Проезд в Ростов стоит 1000 рублей; в Мелитополь — 800 рублей.",
    ],
)
def test_real_prices_cannot_be_attached_to_another_paid_destination(body):
    result = validate_body(body, [FARE])
    assert any("DIGEST_FACT_BINDING_MISMATCH:fare_destination" in v for v in result.violations)


@pytest.mark.parametrize(
    "body",
    [
        "Билет Бердянск—Мелитополь стоит 800 рублей. На проходящих автобусах в Москву "
        "проезд до Мелитополя стоит 1000 рублей.",
        "Проезд до Мелитополя на обычном автобусе стоит 800 рублей, на проходящих — 1000 рублей.",
    ],
)
def test_faithful_paid_leg_paraphrase_remains_publishable(body):
    result = validate_body(body, [FARE])
    assert result.is_valid, result.violations


def test_clock_cannot_gain_an_unsourced_anonymous_street():
    result = validate_body(
        "На одной из улиц Азмола свет включили в 17:20 и отключили в 18:36.",
        ["На Азмоле свет включили в 17:20 и отключили в 18:36."],
        locations=["Азмол"],
    )
    assert any("DIGEST_FACT_BINDING_MISMATCH:clock_street_scope" in v for v in result.violations)


def test_source_may_report_an_anonymous_street_without_official_confirmation():
    source = "На одной из улиц Азмола свет включили в 17:20 и отключили в 18:36."
    result = validate_body(source, [source], locations=["Азмол"])
    assert result.is_valid, result.violations


def test_clock_cannot_be_transferred_between_districts_in_one_synthesized_item():
    result = validate_body(
        "На АКЗ света нет. В Лисках свет появился в 10:35.",
        ["На АКЗ свет появился в 10:35.", "В Лисках света нет."],
        locations=["АКЗ", "Лиски"],
    )
    assert any("DIGEST_FACT_BINDING_MISMATCH:clock_location" in v for v in result.violations)


def test_time_cannot_move_from_water_to_power_in_the_same_place():
    result = validate_body(
        "На Азмоле электричество появилось в 10:35. Воды нет.",
        ["На Азмоле воду дали в 10:35.", "На Азмоле электричества нет."],
        locations=["Азмол", "Азмол"],
    )
    assert any("DIGEST_FACT_BINDING_MISMATCH:clock_service" in v for v in result.violations)


def test_unknown_clock_location_does_not_become_a_location_gate():
    result = validate_body("Свет появился в 10:35, сообщает житель.", ["Дали свет в 10:35."])
    assert result.is_valid, result.violations


def test_area_and_street_reports_do_not_prove_a_rest_of_area_partition():
    result = validate_body(
        "На улице Хмельницкого воды нет. На остальной территории Азмола воду дают через день.",
        ["На улице Хмельницкого воды нет.", "На Азмоле воду дают через день."],
        locations=["улица Хмельницкого", "Азмол"],
    )
    assert any("UNSUPPORTED_DIGEST_RELATION:area_partition" in v for v in result.violations)


def test_citywide_extent_cannot_transfer_between_service_facts():
    result = validate_body(
        "По сообщению жителя, у большей части Бердянска нет газа с субботы "
        "и электричества с 9 августа. У большей части Бердянска нет воды.",
        [
            "По сообщению жителя, газа нет с субботы.",
            "По сообщению жителя, электричества нет с 9 августа.",
            "По сообщению жителя, у большей части города нет воды.",
        ],
        locations=["Бердянск", "Бердянск", "большая часть Бердянска"],
    )
    assert any("DIGEST_FACT_BINDING_MISMATCH:service_extent" in v for v in result.violations)


def test_citywide_extent_stays_with_its_service_in_a_shared_source_message():
    source = "С субботы нет газа, с 9 августа света, у большей части города воды."
    result = validate_body(
        "У большей части Бердянска нет газа с субботы и электричества с 9 августа. "
        "У большей части Бердянска нет воды.",
        [source, source, source],
        locations=["Бердянск", "Бердянск", "большая часть Бердянска"],
        canonical_services=["gas", "power", "water"],
    )
    assert any(
        "DIGEST_FACT_BINDING_MISMATCH:service_extent" in violation and "fact:0" in violation
        for violation in result.violations
    )
    assert any(
        "DIGEST_FACT_BINDING_MISMATCH:service_extent" in violation and "fact:1" in violation
        for violation in result.violations
    )
    assert not any(
        "DIGEST_FACT_BINDING_MISMATCH:service_extent" in violation and "fact:2" in violation
        for violation in result.violations
    )


def test_explicit_extent_can_cover_a_compound_service_list_in_one_clause():
    source = "У большей части города нет газа, света и воды."
    result = validate_body(
        source,
        [source, source, source],
        canonical_services=["gas", "power", "water"],
    )
    assert not any("DIGEST_FACT_BINDING_MISMATCH:service_extent" in v for v in result.violations)


def test_explicit_single_source_citywide_water_scope_remains_publishable():
    source = "По сообщению жителя, у большей части города нет воды."
    result = validate_body(
        "По сообщению жителя, у большей части Бердянска нет воды.",
        [source],
        locations=["большая часть Бердянска"],
    )
    assert not any("DIGEST_FACT_BINDING_MISMATCH:service_extent" in v for v in result.violations)


def test_scope_before_short_attribution_still_binds_to_its_service():
    source = "У большей части города, по словам жителей, нет воды."
    result = validate_body(
        source,
        [source],
        locations=["большая часть Бердянска"],
    )
    assert result.is_valid, result.violations


def test_explicit_partition_in_single_source_is_allowed():
    source = "На улице Хмельницкого воды нет, на остальных улицах Азмола воду дают через день."
    result = validate_body(source, [source], locations=["Азмол"])
    assert result.is_valid, result.violations


def test_conflicting_untimed_reports_do_not_prove_a_restoration_sequence():
    result = validate_body(
        "На АКЗ света не было. После этого электричество восстановили.",
        ["На АКЗ света нет.", "На АКЗ дали свет."],
        locations=["АКЗ", "АКЗ"],
        states=["UNAVAILABLE", "AVAILABLE"],
    )
    assert any("UNSUPPORTED_DIGEST_RELATION:unproven_sequence" in v for v in result.violations)


def test_source_reported_chronology_is_allowed():
    source = "На АКЗ света не было. После этого дали свет."
    result = validate_body(source, [source], locations=["АКЗ"])
    assert result.is_valid, result.violations


def test_incomplete_source_context_is_not_evidence_for_a_binding_mismatch():
    result = validate_body(
        "На одной из улиц свет появился в 10:35.", ["Дали свет в 10:35."], missing_support=True
    )
    assert not any("DIGEST_FACT_BINDING_MISMATCH" in v for v in result.violations)
    assert not any("UNSUPPORTED_DIGEST_RELATION:unproven_sequence" in v for v in result.violations)


def test_unrelated_sequence_word_is_not_a_restoration_claim():
    result = validate_body(
        "На АКЗ сообщают и об отсутствии света, и о включении. Затем житель проверил телефон.",
        ["На АКЗ света нет. Житель проверил телефон.", "На АКЗ дали свет."],
        locations=["АКЗ", "АКЗ"],
        states=["UNAVAILABLE", "AVAILABLE"],
    )
    assert not any("unproven_sequence" in v for v in result.violations)


def test_partition_cannot_borrow_its_proof_from_an_unrelated_service():
    result = validate_body(
        "На улице Хмельницкого воды нет, на остальной территории Азмола воду дают через день. "
        "На остальных улицах ремонтируют дороги.",
        [
            "На улице Хмельницкого воды нет.",
            "На Азмоле воду дают через день.",
            "На остальных улицах ремонтируют дороги.",
        ],
        locations=["улица Хмельницкого", "Азмол", ""],
    )
    assert any("UNSUPPORTED_DIGEST_RELATION:area_partition" in v for v in result.violations)


def test_shared_clock_value_keeps_ambiguous_ownership_explicit():
    result = validate_body(
        "На АКЗ и в Лисках свет появился в 10:35.",
        ["На АКЗ дали свет в 10:35.", "В Лисках дали свет в 10:35."],
        locations=["АКЗ", "Лиски"],
    )
    assert not any("DIGEST_FACT_BINDING_MISMATCH" in v for v in result.violations)
    assert any("DIGEST_LEDGER_CLOCK_OWNER_NOT_EVALUATED" in v for v in result.not_evaluated)


@pytest.mark.parametrize("accept_repair", [True, False], ids=["repair", "retain-checkpoint"])
def test_existing_editor_gets_concrete_rejection_and_repairs_within_two_calls(accept_repair):
    import asyncio
    import datetime as dt
    import json

    from scripts.digest_evaluation.replay import ReplayObserver
    from src.editorial_models import EditorialAnalysis, PreparedBundle
    from src.publication.digest_assessment import DigestAssessmentContext, assess_digest_candidate
    from src.publication.digest_editor import DigestEditor
    from src.publication.editorial_adapter import FrozenEditorialInput
    from src.publication.evidence import PublicationEvidence
    from src.publication.generation import _repair_digest_candidate
    from src.publication.renderers import PublicationDigestRenderer

    plan, supports = binding_inputs([FARE])
    fact = plan.blocks[0].required_facts[0]
    presentation = DigestPresentationPlan(story_ids=fact.story_ids, required_facts=(fact,))
    evidence = PublicationEvidence(
        evidence_id="source:0",
        story_id=0,
        text=FARE,
        source_text=FARE,
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=1,
        source_ref="source:0",
        source_id=1,
        source_item_id=1,
        source_role="community",
        observed_at=dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc),
    )
    cards = [_story("story:0", "infrastructure", "source:0")]
    frozen = FrozenEditorialInput(
        EditorialAnalysis(cards=cards, evidence={"source:0": evidence}),
        PreparedBundle({}, "", 1, 1),
        edition_slug="berdyansk",
    )
    context = DigestAssessmentContext(
        frozen=frozen,
        plan=plan,
        presentation_plan=presentation,
        evidence={"source:0": evidence},
        support_text_by_id=supports,
        allowed_context_terms=(),
        snapshot_at=evidence.observed_at,
        timezone_name="UTC",
        renderer=PublicationDigestRenderer(include_statistics=False),
    )
    draft = _parse_composition_writer_output(
        {
            "blocks": [
                {
                    "block_id": plan.blocks[0].block_id,
                    "items": [
                        {
                            "composition_unit_ids": ["unit:0"],
                            "covered_fact_ids": ["fact:0"],
                            "headline": "",
                            "body": FARE,
                            "claims": [],
                        }
                    ],
                }
            ]
        },
        plan=plan,
    )
    initial = assess_digest_candidate(draft, context=context)
    assert initial.is_safe, initial.validation.violations
    repaired = (
        "Проезд до Мелитополя стоит 800 рублей на обычном автобусе и 1000 рублей "
        "на проходящих автобусах в Ростов и Москву."
    )

    class Provider:
        calls = 0

        async def chat_completion(self, **kwargs):
            self.calls += 1
            request = json.loads(kwargs["messages"][1]["content"])
            # Return a repair only if the real policy forwards the actual
            # mismatch. A generic 'safety checks failed' is insufficient.
            specific = (
                "DIGEST_FACT_BINDING_MISMATCH:fare_destination" in kwargs["messages"][0]["content"]
            )
            body = (
                repaired
                if specific and accept_repair
                else "Билет до Москвы стоит 1000 рублей, до Мелитополя — 800 рублей."
            )
            if self.calls > 1:
                return json.dumps(
                    {
                        "blocks": [
                            {
                                "block_id": request["blocks"][0]["block_id"],
                                "items": [{"item_id": request["target_item_ids"][0], "body": body}],
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    "blocks": [
                        {
                            "block_id": request["blocks"][0]["block_id"],
                            "recomposed_items": [
                                {
                                    "covered_fact_ids": ["fact:0"],
                                    "headline": "",
                                    "body": body,
                                    "claims": [],
                                    "composition_unit_ids": ["unit:0"],
                                }
                            ],
                        }
                    ]
                }
            )

    provider = Provider()
    checkpoint, used, calls = asyncio.run(
        _repair_digest_candidate(
            checkpoint=initial.checkpoint(),
            plan=plan,
            evidence=context.evidence,
            editor=DigestEditor(provider),
            observer=ReplayObserver(),
            evaluate_candidate=lambda candidate: assess_digest_candidate(
                candidate, context=context
            ).checks(),
            model="test",
            timeout_seconds=10,
            implementation_versions={},
            editor_scope="thematic_blocks",
            review_without_findings=True,
            support_text_by_id=supports,
        )
    )
    assert checkpoint[0].blocks[0].items[0].body == (repaired if accept_repair else FARE)
    assert checkpoint[1].is_valid
    assert used is accept_repair
    assert calls == provider.calls == 2
    if not accept_repair:
        assert checkpoint == initial.checkpoint()


def test_area_clock_cannot_gain_a_known_street_even_without_the_word_street():
    result = validate_body(
        "На Кирова свет включили в 17:20 и отключили в 18:36.",
        ["В Стекловолокно свет включили в 17:20 и отключили в 18:36.", "На Кирова света нет."],
        locations=["Стекловолокно", "Кирова"],
    )
    assert any("DIGEST_FACT_BINDING_MISMATCH:clock_street_scope" in v for v in result.violations)


def test_colloquial_and_municipal_area_views_do_not_create_a_false_location_error():
    source = "В Колонии на Крылова свет появился в 10:35."
    result = validate_body("На Крылова свет появился в 10:35.", [source], locations=["Колония"])
    assert not any("DIGEST_FACT_BINDING_MISMATCH" in v for v in result.violations)


def test_same_price_for_another_explicit_journey_is_not_a_false_fare_mismatch():
    result = validate_body(
        "Билет до Москвы стоит 1000 рублей. Проезд до Мелитополя стоит 800 рублей "
        "на обычном автобусе и 1000 рублей на проходящих.",
        [FARE, "Билет Бердянск—Москва стоит 1000 рублей."],
    )
    assert not any("DIGEST_FACT_BINDING_MISMATCH:fare_destination" in v for v in result.violations)


def test_source_clock_cannot_become_supply_duration():
    source = "В субботу свет дали в р-не 13 часов, в р-не 12 ночи снова выключили."
    result = validate_body(
        "По словам жителя, в субботу подача электричества продолжалась около 13 часов.",
        [source],
    )
    assert any("clock_duration" in value for value in result.violations)


def test_source_clock_preserves_hour_and_supplied_duration_is_allowed():
    source = "В субботу свет дали в р-не 13 часов, в р-не 12 ночи снова выключили."
    result = validate_body(
        "По словам жителя, свет дали около 13:00 и выключили около полуночи.", [source]
    )
    assert not any("clock_duration" in value for value in result.violations)
    result = validate_body(
        "По словам жителя, свет был около 13 часов.", [source + " До этого свет был 13 часов."]
    )
    assert not any("clock_duration" in value for value in result.violations)
