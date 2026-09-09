"""Unit tests for Berdyansk toponym and entity normalization."""

from __future__ import annotations

import pytest

from src.domain.edition_geography import (
    normalize_edition_toponyms,
)
from src.processing.operational_semantics import (
    normalize_berdyansk_toponyms,
    normalize_operational_location_and_entity,
    sanitize_operational_detail,
)


@pytest.mark.unit
def test_edition_aware_toponym_normalization_berdyansk() -> None:
    text = "В районе Юпитер пока отсутствует оптоволокно."
    assert (
        normalize_edition_toponyms(text, edition_slug="berdyansk")
        == "У провайдера «Юпитер» пока отсутствует оптоволокно."
    )


@pytest.mark.unit
def test_edition_aware_toponym_normalization_unknown_edition() -> None:
    text = "В районе Юпитер пока отсутствует оптоволокно."
    assert normalize_edition_toponyms(text, edition_slug="unknown_city") == text


@pytest.mark.unit
def test_normalize_jupiter_provider_not_district() -> None:
    # Capitalized at start of sentence
    t1 = "В районе Юпитер пока отсутствует оптоволокно."
    assert normalize_berdyansk_toponyms(t1) == "У провайдера «Юпитер» пока отсутствует оптоволокно."

    # With quotes and lowercase
    t2 = "в районе «Юпитер» связь нестабильна"
    assert normalize_berdyansk_toponyms(t2) == "у провайдера «Юпитер» связь нестабильна"

    # With adjective "бердянском"
    t3 = "В бердянском районе «Юпитер» пока нет оптоволоконного подключения"
    assert (
        normalize_berdyansk_toponyms(t3)
        == "У провайдера «Юпитер» пока нет оптоволоконного подключения"
    )

    # Direct mention of provider area
    t4 = "район Юпитер сообщает о перебоях"
    assert normalize_berdyansk_toponyms(t4) == "провайдер «Юпитер» сообщает о перебоях"

    # "в Юпитере" when discussing fiber
    t5 = "У кого есть оптоволокно, в Юпитере пока ещё его нет"
    assert "у провайдера «Юпитер»" in normalize_berdyansk_toponyms(t5)


@pytest.mark.unit
def test_normalize_plane_monument_berdyansk() -> None:
    # In digest from user report
    t1 = "У «Літака» выброшены новые фонари, предназначавшиеся для установки — сообщают жители."
    assert (
        normalize_berdyansk_toponyms(t1)
        == "У памятника Самолёту выброшены новые фонари, предназначавшиеся для установки — сообщают жители."
    )

    # Lowercase preposition
    t2 = "работы ведутся возле «Літака»"
    assert "у памятника Самолёту" in normalize_berdyansk_toponyms(t2)

    # Transliterated / typo Russian forms
    t3 = "у Литака собрались люди"
    assert normalize_berdyansk_toponyms(t3) == "у памятника Самолёту собрались люди"

    t4 = "возле литака чисто"
    assert normalize_berdyansk_toponyms(t4) == "у памятника Самолёту чисто"

    # Ukrainian full form
    t5 = "Окупаційна влада відзвітувала про ремонт біля пам'ятника «Літак» на вул. Волонтерів"
    assert "возле памятника Самолёту" in normalize_berdyansk_toponyms(t5)


@pytest.mark.unit
def test_normalize_lanterns_translation() -> None:
    t1 = "новые лихтари лежат в кустах"
    assert normalize_berdyansk_toponyms(t1) == "новые фонари лежат в кустах"

    t2 = "установили ліхтарі на улице"
    assert normalize_berdyansk_toponyms(t2) == "установили фонари на улице"

    t3 = "замена лихтарей завершена"
    assert normalize_berdyansk_toponyms(t3) == "замена фонарей завершена"


@pytest.mark.unit
def test_normalize_operational_location_and_entity_jupiter_and_monument() -> None:
    # Jupiter -> entity
    loc, ent = normalize_operational_location_and_entity("район Юпитер")
    assert ent == "Юпитер"
    assert loc == ""

    loc, ent = normalize_operational_location_and_entity("в районе Юпитер")
    assert ent == "Юпитер"
    assert loc == ""

    # Plane monument in Berdyansk
    loc, ent = normalize_operational_location_and_entity("у Літака")
    assert "памятник Самолёту" in loc
    assert "Довганюка" in loc

    # Plane monument in Osypenko village
    loc, ent = normalize_operational_location_and_entity("памятник Самолёт в селе Осипенко")
    assert "село Осипенко" in loc
    assert "памятник Самолёту" in loc


@pytest.mark.unit
def test_sanitize_operational_detail_integrates_toponyms() -> None:
    raw = "В районе Юпитер пока отсутствует оптоволокно. (кто знает когда починят?)"
    sanitized = sanitize_operational_detail(raw)
    assert "У провайдера «Юпитер» пока отсутствует оптоволокно." in sanitized
    assert "кто знает" not in sanitized


@pytest.mark.unit
def test_normalize_akz_not_azmol_complex() -> None:
    t1 = "Жители сообщают, что весь Азмоловский комплекс остаётся без электроэнергии."
    assert (
        normalize_berdyansk_toponyms(t1)
        == "Жители сообщают, что весь АКЗ остаётся без электроэнергии."
    )

    t2 = "В Азмоловском комплексе дали свет."
    assert normalize_berdyansk_toponyms(t2) == "В АКЗ дали свет."
