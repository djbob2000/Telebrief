"""Unit tests for generic EditionGeographyContext domain model and resolution."""

from __future__ import annotations

import pytest

from src.domain.edition_geography import (
    EditionGeographyContext,
    resolve_edition_geography,
)


@pytest.mark.unit
def test_edition_geography_context_dataclass_properties() -> None:
    geo = EditionGeographyContext(
        edition_slug="berdyansk",
        edition_name="Бердянск",
        target_locations=("Бердянск", "АКЗ", "Колония", "Коса", "Слободка"),
        district_locations=(
            "Бердянский район",
            "Азовское",
            "Нововасильевка",
            "Осипенко",
            "Дмитровка",
        ),
        region_name="Запорожская область",
        neighboring_regions=("Донецкая область", "Херсонская область", "Ростовская область"),
        out_of_scope_locations=("Киев", "Харьков", "Одесса", "Львов", "Москва", "Санкт-Петербург"),
    )
    assert geo.edition_slug == "berdyansk"
    assert "Бердянск" in geo.target_locations
    assert "Колония" in geo.target_locations
    assert "Бердянский район" in geo.district_locations
    assert geo.region_name == "Запорожская область"


@pytest.mark.unit
def test_resolve_edition_geography_berdyansk_builtin() -> None:
    geo = resolve_edition_geography("berdyansk", edition_name="Бердянск")
    assert geo.edition_slug == "berdyansk"
    assert geo.edition_name == "Бердянск"
    assert any("Бердянск" in loc for loc in geo.target_locations)
    assert any("АКЗ" in loc or "Колония" in loc for loc in geo.target_locations)
    assert geo.region_name == "Запорожская область"
    assert len(geo.out_of_scope_locations) > 0


@pytest.mark.unit
def test_resolve_edition_geography_generic_fallback() -> None:
    geo = resolve_edition_geography("melitopol", edition_name="Мелитополь")
    assert geo.edition_slug == "melitopol"
    assert geo.edition_name == "Мелитополь"
    assert "Мелитополь" in geo.target_locations
    assert geo.region_name != ""


@pytest.mark.unit
def test_edition_geography_prompt_formatting() -> None:
    geo = resolve_edition_geography("berdyansk", edition_name="Бердянск")
    prompt_str = geo.to_prompt_section()
    assert "ГЕОГРАФИЧЕСКИЙ КОНТЕКСТ ИЗДАНИЯ" in prompt_str
    assert "Бердянск" in prompt_str
    assert "Запорожская область" in prompt_str
