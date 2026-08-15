"""Unit tests for Berdyansk City Profile and City Context Models."""

from pathlib import Path

import pytest
import yaml

from src.city_context import CityContextResolver, CityProfileError
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


def test_city_profile_yaml_coverage_conversion():
    path = Path("data/city_profiles/berdyansk.yaml")
    with open(path, "r", encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    entries = profile["stable_context"]["geography"]["street_gazetteer"]["entries"]
    rule_count = 0
    coverage_count = 0
    for entry in entries:
        for m in entry.get("area_memberships", []):
            if "rule" in m:
                rule_count += 1
            if "coverage" in m:
                coverage_count += 1

    assert rule_count == 0
    assert coverage_count == 468


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


def test_city_context_resolver_exact_and_aliases():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    # Direct area mentions (stable identity + canonical name)
    res_center = resolver.resolve("В центре снова нет света")
    area_center = next(
        e for e in res_center.entities if e.kind == "area" and e.entity_id == "center"
    )
    assert area_center.entity_id == "center"
    assert area_center.canonical_name == "Центр міста"

    res_liski = resolver.resolve("На Лисках нет воды")
    area_liski = next(e for e in res_liski.entities if e.kind == "area" and e.entity_id == "liski")
    assert area_liski.entity_id == "liski"
    assert area_liski.canonical_name == "Ліски"

    # Streets and aliases
    res1 = resolver.resolve("На ул. Шевченко нет света")
    assert any(e.entity_id == "street:Шевченка" for e in res1.entities)

    res2 = resolver.resolve("На бульваре Шевченко нет света")
    assert any(e.entity_id == "boulevard:Шевченка" for e in res2.entities)

    res3 = resolver.resolve("На Морозова воды нет")
    assert any(e.entity_id == "street:Північна" for e in res3.entities)

    # Providers and routes
    res4 = resolver.resolve("Юпитер снова работает")
    assert any(e.entity_id == "jupiter" for e in res4.entities)

    res5 = resolver.resolve("Поинт лежит")
    assert any(e.entity_id == "point" for e in res5.entities)

    res6 = resolver.resolve("маршрут 15 сегодня не ходит")
    assert any(e.entity_id == "route:15" for e in res6.entities)

    # Bare number outside transport context does not become route 15
    res7 = resolver.resolve("В доме 15 отключили воду")
    assert not any(e.entity_id == "route:15" for e in res7.entities)


def test_city_context_resolver_multi_area_ambiguity():
    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")

    res = resolver.resolve("На Мелитопольском шоссе нет света")
    street_entity = next(
        e
        for e in res.entities
        if "Мелітопольське" in e.canonical_name or "шосе" in e.canonical_name
    )
    assert len(street_entity.municipal_areas) > 1
    assert street_entity.confidence == "ambiguous"


def test_city_context_resolver_loader_error_contract(tmp_path):
    # Non-existent file propagates FileNotFoundError
    with pytest.raises(FileNotFoundError):
        CityContextResolver.from_yaml(tmp_path / "non_existent.yaml")

    # Corrupt YAML raises CityProfileError
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("schema_version: 999\nprofile_id: [invalid", encoding="utf-8")
    with pytest.raises(CityProfileError):
        CityContextResolver.from_yaml(bad_yaml)
