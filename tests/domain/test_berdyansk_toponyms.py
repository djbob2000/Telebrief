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


@pytest.mark.unit
def test_normalize_liski_preposition() -> None:
    t1 = "В Лисках и на Лиепайской свет был, но позже в Лисках пропал."
    assert (
        normalize_berdyansk_toponyms(t1)
        == "На Лисках и на Лиепайской свет был, но позже на Лисках пропал."
    )

    t2 = "Поехали в Лиски за покупками."
    assert normalize_berdyansk_toponyms(t2) == "Поехали на Лиски за покупками."


@pytest.mark.unit
def test_normalize_zerkalny_toponym() -> None:
    t1 = "📦 **Магазин у «Зеркального» вывозит товар:** Возле бывшего супермаркета «Зеркальный» в Бердянске местные жители заметили, что из магазина вывозят товар."
    res1 = normalize_berdyansk_toponyms(t1)
    assert "Магазин у «Зеркального»" not in res1
    assert "Вывоз товара из «Зеркального»" in res1
    assert "из бывшего супермаркета «Зеркальный» вывозят товар" in res1

    t2 = "Возле бывшего супермаркета «Дзеркальний» вывозят вещи."
    assert (
        normalize_berdyansk_toponyms(t2) == "Возле бывшего супермаркета «Зеркальный» вывозят вещи."
    )

    t3 = "В Бердянске магазин возле «Дзеркального» вывозит товар"
    assert (
        normalize_berdyansk_toponyms(t3) == "В Бердянске из супермаркета «Зеркальный» вывозят товар"
    )


@pytest.mark.unit
def test_normalize_gorbenko_center_misattribution() -> None:
    t1 = "Сообщают, что в Колонии на Горбенко включили воду."
    assert (
        normalize_edition_toponyms(t1, edition_slug="berdyansk")
        == "Сообщают, что в Центре на улице Горбенко включили воду."
    )


@pytest.mark.unit
def test_berdyansk_toponym_rules_coverage() -> None:
    from src.domain.edition_geography import _BERDYANSK_TOPONYM_RULES

    rules_text = "\n".join(_BERDYANSK_TOPONYM_RULES)
    # Check Central / Karl Marx / Tverskaya rule
    assert "Центральная" in rules_text
    assert "Карла Маркса" in rules_text
    assert "Тверская" in rules_text
    assert "ОДНА И ТА ЖЕ" in rules_text

    # Check Gorbenko / Lyuteranskaya in Center, not Koloniya
    assert "Горбенко" in rules_text
    assert "Лютеранская" in rules_text
    assert "НЕ относится к Колонии" in rules_text
    assert "Розы Люксембург" in rules_text or "Грецкой" in rules_text

    # Check 8 Marta distinction from Gora
    assert "8 Марта" in rules_text
    assert "НЕ входит в Нагорную часть" in rules_text

    # Check relief and distance rules
    assert "через два квартала" in rules_text
    assert "50-метровым" in rules_text


@pytest.mark.unit
def test_berdyansk_city_profile_street_aliases() -> None:
    from pathlib import Path

    import yaml

    profile_path = Path("data/city_profiles/berdyansk.yaml")
    assert profile_path.exists()
    with open(profile_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    geo = data["stable_context"]["geography"]

    # Check Gora description doesn't claim 8 Marta is part of Gora
    areas = []
    for area_set in geo.get("area_sets", []):
        areas.extend(area_set.get("areas", []))
    named_areas = {a["id"]: a for a in areas if "id" in a}
    assert "gora" in named_areas
    assert "8 Марта" not in named_areas["gora"].get("description", "")

    # Check street entities in street_gazetteer
    entries = {
        e["entity_id"]: e
        for e in geo.get("street_gazetteer", {}).get("entries", [])
        if "entity_id" in e
    }

    # street:Центральна has Тверская and Карла Маркса
    central = entries.get("street:Центральна")
    assert central is not None
    alias_texts = [a["text"] for a in central.get("aliases", [])]
    assert "Тверская" in alias_texts
    assert "Карла Маркса" in alias_texts

    # street:Земська has Красная
    zemska = entries.get("street:Земська")
    assert zemska is not None
    zemska_aliases = [a["text"] for a in zemska.get("aliases", [])]
    assert "Красная" in zemska_aliases

    # street:Грецька has Грецкая
    gretska = entries.get("street:Грецька")
    assert gretska is not None
    gretska_aliases = [a["text"] for a in gretska.get("aliases", [])]
    assert "Грецкая" in gretska_aliases

    # street:Лютеранська is in Center
    lyut = entries.get("street:Лютеранська")
    assert lyut is not None
    area_ids = [m["area_id"] for m in lyut.get("area_memberships", [])]
    assert "center" in area_ids
