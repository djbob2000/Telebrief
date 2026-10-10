"""Place/area consistency: catch a wrong district without self-overlap false alarms."""

# ruff: noqa: S101
from pathlib import Path

import pytest

from src.city_context import CityContextResolver
from src.publication import article_quality as aq

PROFILE = Path(__file__).resolve().parents[1] / "data/city_profiles/berdyansk.yaml"


@pytest.fixture(scope="module")
def resolver():
    return aq._DiagnosisPlaceResolver(CityContextResolver.from_yaml(PROFILE))


def _mismatches(text: str, resolver) -> list[str]:
    entities = aq._resolved_entities(text, resolver)
    spans = [
        (start, end, entity)
        for entity in entities
        if entity.kind == "area" and entity.confidence == "high"
        for start, end in aq._entity_text_spans(text, entity)
        if resolver.geographic_area_group_keys(entity)
    ]
    return [
        entity.matched_text
        for entity in aq._place_area_mismatch_entities(text, entities, spans, resolver)
    ]


def test_street_named_after_a_colloquial_area_is_not_a_wrong_area(resolver):
    """Run 324: «улице Морозова» was compared with the colloquial area «Морозова»."""
    text = (
        "Житель сообщил, что на РТС света не было десять дней. В частном секторе на улице "
        "Морозова диспетчер РЭС, по словам жителя, сообщил, что свет следует ожидать раз в "
        "пять дней; других прогнозов он не дал."
    )
    assert _mismatches(text, resolver) == []


def test_street_assigned_to_another_district_is_still_flagged(resolver):
    assert _mismatches("На АКЗ возле 7 Листопада света нет.", resolver)
