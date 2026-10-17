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


def test_area_sharing_a_street_name_is_not_a_wrong_area(resolver):
    """Run 337: the microdistrict «8 Марта» was read as the street 8 Марта."""
    text = (
        "Житель сообщал, что 21 октября свет будет в центре, на Горе, в АКЗ и на Лисках, "
        "но его не будет в Колонии и на 8 Марта."
    )
    assert _mismatches(text, resolver) == []
    assert (
        _mismatches(
            "По сообщению жителя, услуги были доступны в микрорайоне 8 Марта, Военном городке "
            "и центре Бердянска.",
            resolver,
        )
        == []
    )


@pytest.mark.parametrize("street", ["Орджоникидзе", "Чапаева", "Димитрова"])
def test_upland_streets_belong_to_gora(resolver, street):
    """Editorial note 2026-10-17: these streets are on Gora, close to each other."""
    assert _mismatches(f"На Горе на улице {street} света нет.", resolver) == []


def test_pushkina_is_not_on_gora(resolver):
    """Пушкина is in the lower city, far from Gora."""
    assert _mismatches("На Горе на улице Пушкина света нет.", resolver)


@pytest.mark.parametrize("street", ["Святомиколаївська", "Північна"])
def test_checked_upland_streets_belong_to_gora(resolver, street):
    """OSM centres at 44-45 m above sea level (SRTM, 2026-10-17)."""
    assert _mismatches(f"На Горе на улице {street} света нет.", resolver) == []


@pytest.mark.parametrize("street", ["Центральна", "Лютеранська", "Шевченка"])
def test_checked_lower_streets_are_not_on_gora(resolver, street):
    """OSM centres at 2-5 m above sea level (SRTM, 2026-10-17)."""
    assert _mismatches(f"На Горе на улице {street} света нет.", resolver)


@pytest.mark.parametrize(
    "street",
    ["Переможців", "Європейська", "Олега Сухарєва", "Гетьманська"],
)
def test_measured_gora_streets(resolver, street):
    """Street-centre elevations 36-44 m (OSM + SRTM, 2026-10-17)."""
    assert _mismatches(f"На Горе на улице {street} света нет.", resolver) == []


@pytest.mark.parametrize(
    "street",
    ["Петровський шлях", "Захисників України", "Азовський проспект", "Консульська"],
)
def test_measured_lower_streets(resolver, street):
    """Street-centre elevations 0-7 m (OSM + SRTM, 2026-10-17)."""
    assert _mismatches(f"На Горе на улице {street} света нет.", resolver)


def test_gora_boulevard_shevchenka(resolver):
    """Бульвар Шевченко (Гайдара) is on Gora at 43 m; the street Шевченка is lower."""
    assert _mismatches("На Горе на бульваре Шевченка света нет.", resolver) == []
