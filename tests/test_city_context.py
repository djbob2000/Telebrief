"""Unit tests for Berdyansk City Profile and City Context Models."""

from pathlib import Path

import yaml

from src.city_context_models import (
    AreaCandidate,
    AreaEvidence,
    CityContextAnnotation,
    ResolvedEntity,
    ScaleEvidence,
    StoryContext,
)


def test_city_profile_yaml_structure():
    path = Path("data/city_profiles/berdyansk.yaml")
    with open(path, "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    assert profile["schema_version"] == 2
    assert profile["profile_id"] == "berdyansk"
    assert profile["safety_contract"]["context_not_event_evidence"] is True
    assert (
        profile["stable_context"]["geography"]["editorial_scale_area_set"]["exhaustive_for_scale"]
        is False
    )
    assert len(profile["stable_context"]["geography"]["street_gazetteer"]["entries"]) >= 400


def test_city_context_models_instantiation():
    cand = AreaCandidate(
        area_set="municipal",
        area_id="center",
        area_name="Центр",
        confidence="high",
        coverage_kind="whole_object",
        source_ref="gazetteer",
    )
    entity = ResolvedEntity(
        kind="place",
        entity_id="street:Шевченка",
        matched_text="Шевченко",
        canonical_name="вулиця Шевченка",
        object_type="street",
        municipal_areas=(cand,),
    )
    annot = CityContextAnnotation(entities=(entity,))
    assert len(annot.entities) == 1
    assert annot.entities[0].entity_id == "street:Шевченка"

    evidence = AreaEvidence(
        area_set="municipal_neighborhood_committees_2021",
        area_id="center",
        source_refs=("S000001", "S000002"),
        direct_area_refs=("S000002",),
        inferred_from_place_refs=("S000001",),
    )
    assert evidence.area_set == "municipal_neighborhood_committees_2021"
    assert evidence.area_id == "center"
    assert len(evidence.source_refs) == 2

    scale = ScaleEvidence(
        observed_area_ids=("center", "liski"),
        observed_count=2,
        geographic_spread=True,
        broad_prevalence_supported=False,
        majority_supported=False,
    )
    context = StoryContext(
        card_id="SC001",
        municipal_areas=(evidence,),
        scale=scale,
    )
    assert context.card_id == "SC001"
    assert context.scale.geographic_spread is True
