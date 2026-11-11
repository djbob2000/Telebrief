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


def test_nagornaya_is_on_gora(resolver):
    """Нагорная: entrance to Средняя гора (OSM centre 46 m)."""
    assert _mismatches("На Горе на улице Нагорная света нет.", resolver) == []


def test_petrovskyi_shliakh_is_8_marta_not_gora_or_lower(resolver):
    """Elevated street of the 8 Марта district, rising from Kolonia-Makorty (6-48 m)."""
    assert _mismatches("На Горе на улице Петровський шлях света нет.", resolver)
    assert _mismatches("В районе 8 Марта на улице Петровський шлях света нет.", resolver) == []


def test_melitopolskoe_shosse_leads_up_to_gora(resolver):
    """Мелітопольське шосе runs up to АКЗ and РТС, both on Gora."""
    assert _mismatches("На Горе на Мелитопольском шоссе света нет.", resolver) == []


def test_50_let_sssr_is_nagornaya_on_gora(resolver):
    """50 лет СССР is the same street as Нагорная (Gora)."""
    assert _mismatches("На Горе на улице 50 лет СССР света нет.", resolver) == []


def _mismatch_pairs(text: str, resolver) -> list[tuple[str, str]]:
    entities = aq._resolved_entities(text, resolver)
    spans = [
        (start, end, entity)
        for entity in entities
        if entity.kind == "area" and entity.confidence == "high"
        for start, end in aq._entity_text_spans(text, entity)
        if resolver.geographic_area_group_keys(entity)
    ]
    pairs: list[tuple[object, object]] = []
    aq._place_area_mismatch_entities(text, entities, spans, resolver, pairs)
    return [
        (aq._entity_display_name(place), aq._entity_display_name(area)) for place, area in pairs
    ]


@pytest.mark.parametrize(
    "text",
    [
        # Run 388 (2026-11-11): roster paragraphs list areas and streets side by side.
        "Свет появился на Горе, в Лисках и на улице Пушкина.",
        "По сообщениям жителей, свет был на Горе, а также на улице Пушкина.",
        "Жители сообщали о свете на Горе, в Колонии, на улице Пушкина и на АКЗ.",
        "На Горе света нет, а на улице Пушкина свет дали утром.",
        "На улице Пушкина свет дали утром, тогда как на Горе его нет.",
        "На АКЗ и возле 7 Листопада света нет.",
    ],
)
def test_listed_or_contrasted_locations_are_not_assigned_to_one_area(resolver, text):
    assert _mismatches(text, resolver) == []


@pytest.mark.parametrize(
    "text",
    [
        # Apposition: the street is presented as part of the area.
        "На Горе, на улице Пушкина, света нет.",
        "На Горе, а именно на улице Пушкина, света нет.",
        # The area frames both clauses: another street stands between them.
        "На Горе света нет на Димитрова, а на улице Пушкина свет дали утром.",
        # A colon introduces the area's own streets.
        "На Горе света нет: на Димитрова с утра, а на улице Пушкина с обеда.",
        # A semicolon may separate items of one area's list.
        "Свет дали в Колонии; на улице Пушкина его не было до вечера.",
        # Two names of the same upland area in different views restate it.
        "На Горе, в Нагорной части, на улице Пушкина света нет.",
    ],
)
def test_street_presented_inside_another_area_is_still_flagged(resolver, text):
    assert _mismatches(text, resolver)


def test_mismatch_names_the_place_and_the_area_for_the_editor(resolver):
    assert _mismatch_pairs("На АКЗ возле 7 Листопада света нет.", resolver) == [
        ("7 листопада", "АКЗ")
    ]
    assert _mismatch_pairs("На Горе на улице Пушкина света нет.", resolver) == [
        ("улице пушкина", "горе")
    ]
