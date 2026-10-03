from __future__ import annotations

# ruff: noqa: S101
import datetime as dt
from types import SimpleNamespace

import pytest

from src.editorial_models import StoryCard
from src.publication.city_situation import CitySituationItem, CitySituationRollup
from src.publication.digest_composition import (
    DigestCompositionResult,
    DigestCompositionUnit,
    DigestFactRecord,
    build_digest_composition,
)
from src.publication.digest_narrative import _composition_narrative_plan
from src.publication.digest_presentation import (
    DigestPresentationPlan,
    RequiredDigestFact,
    build_required_digest_facts,
)
from src.publication.errors import DigestCoverageInvariantError
from src.publication.evidence import PublicationEvidence


def _story(story_id: str, rubric_id: str, source_ref: str) -> StoryCard:
    return StoryCard(
        id=story_id,
        topic=f"{rubric_id} update",
        importance="high",
        summary=f"A reported {rubric_id} update at Горбенко street.",
        representative_source_refs=[source_ref],
        rubric_id=rubric_id,
    )


def _required_fact(
    fact_id: str,
    rubric_id: str,
    story_id: str,
    support_id: str,
    text: str,
) -> RequiredDigestFact:
    return RequiredDigestFact(
        fact_id=fact_id,
        rubric_id=rubric_id,
        subject_key=rubric_id,
        subject_label=rubric_id,
        story_ids=(story_id,),
        support_ids=(support_id,),
        text=text,
        original_location="Горбенко",
    )


def _record(
    fact_id: str,
    rubric_id: str,
    story_id: str,
    support_id: str,
    text: str,
) -> DigestFactRecord:
    return DigestFactRecord(
        fact_id=fact_id,
        story_ids=(story_id,),
        support_ids=(support_id,),
        rubric_id=rubric_id,
        canonical_subject=rubric_id,
        canonical_service=rubric_id,
        canonical_area="unknown:Горбенко",
        canonical_place=(),
        original_location="Горбенко",
        effective_time=None,
        observed_time=None,
        service_state="",
        epistemic_kind="community_report",
        source_publication_time=None,
        text=text,
    )


def test_composition_rejects_duplicate_required_fact_ids_before_records_are_built() -> None:
    power = _story("story:101", "utilities", "source-power")
    water = _story("story:102", "water", "source-water")
    plan = DigestPresentationPlan(
        story_ids=(power.id, water.id),
        required_facts=(
            _required_fact("горбенко", "utilities", power.id, "source-power", "Свет отключён."),
            _required_fact("горбенко", "water", water.id, "source-water", "Воды нет."),
        ),
    )

    with pytest.raises(DigestCoverageInvariantError, match="DIGEST_DUPLICATE_REQUIRED_FACT_ID"):
        build_digest_composition(
            plan,
            (power, water),
            {},
            edition_slug="",
            snapshot_at=None,
            max_chars=4096,
            reserved_chars=0,
            include_statistics=False,
        )


def test_narrative_rejects_duplicate_composition_record_ids_before_rubric_lookup() -> None:
    required = _required_fact(
        "shared-fact", "utilities", "story:101", "source-power", "Свет отключён."
    )
    composition = DigestCompositionResult(
        units=(
            DigestCompositionUnit(
                unit_id="unit-power",
                rubric_id="utilities",
                fact_ids=("shared-fact",),
                story_ids=("story:101",),
                support_ids=("source-power",),
                canonical_area_key="unknown:Горбенко",
                priority=40,
            ),
        ),
        relations=(),
        dispositions=(),
        admitted_story_ids=frozenset({"story:101"}),
        admitted_fact_ids=frozenset({"shared-fact"}),
        estimated_visible_character_count=100,
        fact_records=(
            _record("shared-fact", "utilities", "story:101", "source-power", "Свет отключён."),
            _record("shared-fact", "water", "story:102", "source-water", "Воды нет."),
        ),
    )
    plan = DigestPresentationPlan(
        story_ids=("story:101",), required_facts=(required,), composition=composition
    )

    with pytest.raises(DigestCoverageInvariantError, match="DIGEST_DUPLICATE_COMPOSITION_FACT_ID"):
        _composition_narrative_plan(
            cards=(_story("story:101", "utilities", "source-power"),),
            rubrics=({"id": "utilities", "title": "Коммунальная обстановка"},),
            presentation_plan=plan,
        )


@pytest.mark.parametrize(
    ("duplicate_collection", "error_code"),
    (
        ("required", "DIGEST_DUPLICATE_REQUIRED_FACT_ID"),
        ("records", "DIGEST_DUPLICATE_COMPOSITION_FACT_ID"),
    ),
)
def test_narrative_validates_fact_ids_before_empty_composition_return(
    duplicate_collection: str, error_code: str
) -> None:
    required = _required_fact(
        "empty-composition-fact", "utilities", "story:101", "source-power", "Свет отключён."
    )
    records = (_record("record-fact", "utilities", "story:101", "source-power", "Свет отключён."),)
    presentation_facts = (required,)
    if duplicate_collection == "required":
        presentation_facts = (required, required)
    else:
        records = (*records, *records)
    plan = DigestPresentationPlan(
        story_ids=("story:101",),
        required_facts=presentation_facts,
        composition=SimpleNamespace(
            units=(),
            admitted_story_ids=frozenset(),
            admitted_fact_ids=frozenset(),
            fact_records=records,
            relations=(),
        ),
    )

    with pytest.raises(DigestCoverageInvariantError, match=error_code):
        _composition_narrative_plan(
            cards=(_story("story:101", "utilities", "source-power"),),
            rubrics=(),
            presentation_plan=plan,
        )


@pytest.mark.parametrize(
    ("use_supplied_ids", "water_rubric"),
    (
        pytest.param(False, "water", id="generated-distinct-rubrics"),
        pytest.param(True, "water", id="supplied-distinct-rubrics"),
        pytest.param(False, "utilities", id="generated-same-rubric"),
    ),
)
def test_same_address_facts_keep_story_support_and_situation_membership(
    use_supplied_ids: bool, water_rubric: str
) -> None:
    power = _story("story:101", "utilities", "source-power")
    water = _story("story:102", water_rubric, "source-water")
    observed_at = dt.datetime(2026, 10, 3, 8, tzinfo=dt.timezone.utc)
    power_detail = "Residents report that electricity is unavailable."
    water_detail = "Residents report that water is unavailable."
    situation = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="power",
                subject_label="Electricity",
                dimension="availability",
                location="Горбенко",
                entity="power supply",
                state="UNAVAILABLE",
                detail=power_detail,
                source_refs=("source-power",),
                first_observed_at=observed_at,
                last_observed_at=observed_at,
                observation_count=1,
                current_source_refs=("source-power",),
                fact_id="power-gorbenko" if use_supplied_ids else "",
            ),
            CitySituationItem(
                subject_key="water",
                subject_label="Water supply",
                dimension="availability",
                location="Горбенко",
                entity="water supply",
                state="UNAVAILABLE",
                detail=water_detail,
                source_refs=("source-water",),
                first_observed_at=observed_at,
                last_observed_at=observed_at,
                observation_count=1,
                current_source_refs=("source-water",),
                fact_id="water-gorbenko" if use_supplied_ids else "",
            ),
        )
    )
    evidence = {
        "evidence-power": PublicationEvidence(
            evidence_id="evidence-power",
            story_id=101,
            text=f"Горбенко: {power_detail}",
            source_text=power_detail,
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=1,
            source_ref="source-power",
            source_id=501,
            source_item_id=1001,
            source_role="community_report",
            observed_at=observed_at,
        ),
        "evidence-water": PublicationEvidence(
            evidence_id="evidence-water",
            story_id=102,
            text=f"Горбенко: {water_detail}",
            source_text=water_detail,
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=2,
            source_ref="source-water",
            source_id=502,
            source_item_id=1002,
            source_role="community_report",
            observed_at=observed_at,
        ),
    }

    required_facts = build_required_digest_facts(
        cards=(power, water), evidence=evidence, city_situation=situation
    )
    plan = DigestPresentationPlan(
        story_ids=(power.id, water.id),
        required_facts=required_facts,
        city_situation=situation,
    )
    composition = build_digest_composition(
        plan,
        (power, water),
        evidence,
        edition_slug="",
        snapshot_at=observed_at,
        max_chars=4096,
        reserved_chars=0,
        include_statistics=False,
        rubric_labels={
            "utilities": "Коммунальная обстановка",
            "water": "Водоснабжение",
        },
    )
    planned = plan.with_composition(composition)
    narrative_plan = _composition_narrative_plan(
        cards=(power, water),
        rubrics=(
            {"id": "utilities", "title": "Коммунальная обстановка"},
            {"id": "water", "title": "Водоснабжение"},
        ),
        presentation_plan=planned,
    )

    assert len({fact.fact_id for fact in required_facts}) == 2
    assert len({fact.fact_id for fact in composition.fact_records}) == 2
    assert {story_id for unit in composition.units for story_id in unit.story_ids} == {
        "story:101",
        "story:102",
    }
    expected_supports = {
        "source-power",
        "source-water",
        "evidence-power",
        "evidence-water",
        "fragment:1",
        "fragment:2",
    }
    actual_supports = {support_id for unit in composition.units for support_id in unit.support_ids}
    assert actual_supports == expected_supports
    assert planned.city_situation.items == situation.items
    blocks_by_rubric = {block.rubric_id: block for block in narrative_plan.blocks}
    assert set(blocks_by_rubric) == {"utilities", water_rubric}
    if water_rubric == "water":
        assert blocks_by_rubric["utilities"].story_ids == ("story:101",)
        assert set(blocks_by_rubric["utilities"].support_ids) == {
            "source-power",
            "evidence-power",
            "fragment:1",
        }
        assert blocks_by_rubric["water"].story_ids == ("story:102",)
        assert set(blocks_by_rubric["water"].support_ids) == {
            "source-water",
            "evidence-water",
            "fragment:2",
        }
    else:
        assert set(blocks_by_rubric["utilities"].story_ids) == {"story:101", "story:102"}
        assert set(blocks_by_rubric["utilities"].support_ids) == expected_supports


def test_fix_duplicated_attribution_cleans_infix_headline_attribution() -> None:
    from src.publication.digest_narrative import _fix_duplicated_attribution

    hl = "В районе РТС, по словам жителя, неделю нет света"
    body = "Житель сообщает, что электроснабжение в районе РТС отсутствует уже неделю."
    clean_hl, clean_body = _fix_duplicated_attribution(hl, body)
    assert "по словам жителя" not in clean_hl
    assert clean_hl == "В районе РТС неделю нет света"
    assert clean_body == body


def test_compact_observation_keeps_fact_coverage_and_renders_once() -> None:
    from src.editorial_models import EditorialAnalysis, PreparedBundle
    from src.publication.digest_narrative import (
        _parse_composition_writer_output,
        sanitize_digest_narrative_draft,
        validate_digest_narrative,
    )
    from src.publication.editorial_adapter import FrozenEditorialInput
    from src.publication.renderers import PublicationDigestRenderer

    text = "Житель сообщает, что на улице Горбенко неделю нет света."
    story = _story("story:101", "utilities", "source-power")
    fact = _required_fact("power-gorbenko", "utilities", story.id, "source-power", text)
    unit = DigestCompositionUnit(
        unit_id="unit-power",
        rubric_id="utilities",
        fact_ids=(fact.fact_id,),
        story_ids=(story.id,),
        support_ids=("source-power",),
        canonical_area_key="unknown:Горбенко",
        priority=40,
    )
    composition = DigestCompositionResult(
        units=(unit,),
        relations=(),
        dispositions=(),
        admitted_story_ids=frozenset({story.id}),
        admitted_fact_ids=frozenset({fact.fact_id}),
        estimated_visible_character_count=100,
        fact_records=(_record(fact.fact_id, "utilities", story.id, "source-power", text),),
    )
    plan = _composition_narrative_plan(
        cards=(story,),
        rubrics=({"id": "utilities", "title": "Коммунальная обстановка"},),
        presentation_plan=DigestPresentationPlan(
            story_ids=(story.id,), required_facts=(fact,), composition=composition
        ),
    )
    draft = _parse_composition_writer_output(
        {
            "blocks": [
                {
                    "block_id": plan.blocks[0].block_id,
                    "items": [
                        {
                            "composition_unit_ids": [unit.unit_id],
                            "covered_fact_ids": [fact.fact_id],
                            "headline": "",
                            "body": text,
                            "claims": [{"text": text, "covered_fact_ids": [fact.fact_id]}],
                        }
                    ],
                }
            ]
        },
        plan=plan,
    )
    draft = sanitize_digest_narrative_draft(draft)
    validation = validate_digest_narrative(draft, plan, {"source-power": text})
    assert validation.is_valid, validation.violations
    item = draft.blocks[0].items[0]
    assert item.headline == ""
    assert item.body == text
    assert item.covered_story_ids == (story.id,)
    assert item.covered_fact_ids == (fact.fact_id,)
    assert item.cited_support_ids == ("source-power",)
    frozen = FrozenEditorialInput(
        analysis=EditorialAnalysis(cards=[story]),
        writer_bundle=PreparedBundle(
            records={}, prompt_text="", total_messages=1, candidate_count=1
        ),
    )
    _, lead, rendered = PublicationDigestRenderer().render_grouped_digest(
        frozen, narrative_draft=draft
    )
    assert lead == ""
    assert rendered.count(text) == 1
