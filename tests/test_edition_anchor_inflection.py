# ruff: noqa: S101
"""Edition anchors are found in their inflected Russian and Ukrainian forms.

Regression: «під Бердянськом» (Ukrainian instrumental) was read as a street
adjective, and the multi-word anchor «Бердянский район» matched only in the
nominative. A post that named the city only in these forms counted as naming
no edition place, so the same-message and broad-region guards could move its
local paragraph out of scope. Anchors with a parenthetical note, such as
«Осипенко (село Бердянского района)», matched nothing at all.
"""

from __future__ import annotations

import pytest

from src.config_loader import load_config
from src.domain.edition_geography import resolve_edition_geography
from src.processing.edition_scope import (
    FragmentMessageContext,
    _contains_any_anchor,
    _settlement_anchors,
    broad_region_without_focus_impact,
    fragment_placed_outside_by_message,
)

SCOPE = load_config("config.yaml").settings.edition_scopes["berdyansk"]
GEO = resolve_edition_geography("berdyansk", SCOPE.name)
SETTLEMENTS = _settlement_anchors(SCOPE, GEO)


@pytest.mark.parametrize(
    "text",
    [
        "Удар під Бердянськом",
        "Жители Бердянского района вернулись из плена",
        "З полону повернулися жителі Бердянського району",
        "У Бердянському районі зникло світло",
        "в Бердянском районе нет воды",
        "у Бердянську зник світ",
    ],
)
def test_city_and_district_are_found_in_any_case(text: str) -> None:
    assert _contains_any_anchor(text, SETTLEMENTS)


def test_anchor_with_parenthetical_note_matches_its_name() -> None:
    assert _contains_any_anchor(
        "В Осипенко третий день нет света", {"Осипенко (село Бердянского района)"}
    )
    assert _contains_any_anchor("Азовское без воды", {"Азовское (село)"})


def test_multi_word_anchor_declines_every_word() -> None:
    anchors = {"Нагорная часть (Гора)"}

    assert _contains_any_anchor("в нагорной части города нет света", anchors)
    assert _contains_any_anchor("Нагорная часть без воды", anchors)
    assert _contains_any_anchor("Обстріл Кривого Рогу", {"Кривой Рог"})
    assert _contains_any_anchor("у супермаркеті «Зеркальний»", {"супермаркет «Зеркальный»"})


@pytest.mark.parametrize(
    ("text", "anchors"),
    [
        # A street named after another city is not that city.
        ("на Севастопольской улице прорвало трубу", {"Севастополь"}),
        ("на улице Бердянской в Запорожье", {"Бердянск"}),
        # Each word of a multi-word anchor must be present.
        ("на Приморском бульваре", {"Приморский район"}),
        ("в Бердянском порту", {"Бердянский залив"}),
        ("в районе рынка", {"Бердянский район"}),
        # A case ending is at most three letters, not a longer word on the same stem.
        ("бердянские косметологи открыли запись", {"Бердянская коса"}),
    ],
)
def test_lookalike_words_are_not_anchors(text: str, anchors: set[str]) -> None:
    assert not _contains_any_anchor(text, anchors)


def test_ukrainian_instrumental_keeps_split_paragraph_local() -> None:
    fragment = FragmentMessageContext(
        text="Під Бердянськом пролунали вибухи.",
        earlier_message_texts=("Росіяни вдарили по Запоріжжю та Мелітополю.",),
    )

    assert not fragment_placed_outside_by_message(fragment, scope=SCOPE, geo_context=GEO)


def test_declined_district_is_a_focus_place_not_a_broad_region_report() -> None:
    assert not broad_region_without_focus_impact(
        basis_texts=["У Запорізькій області з полону повернулися жителі Бердянського району."],
        scope=SCOPE,
        geo_context=GEO,
    )
