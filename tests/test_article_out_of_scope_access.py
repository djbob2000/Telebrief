# ruff: noqa: S101
"""A service location in another settlement is not city-life material for this edition."""

from __future__ import annotations

import pytest

from src.publication.article_material import _drop_out_of_scope_access_sentences


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The writer-facing sentence from the weekly article of 2026-10-18.
        ("Приём проходит в Запорожье.", ""),
        (
            "Бердянский центр разъяснил порядок. Приём проходит в Запорожье. Срок до 30 дней.",
            "Бердянский центр разъяснил порядок. Срок до 30 дней.",
        ),
        # Source text in Ukrainian; the abbreviations must not split the sentence.
        (
            "📍 Прийом відбувається у м. Запоріжжя, вул. Незалежної України, 86-А "
            "(зупинка громадського транспорту – вул. Дмитра Донцова (колишня вул. Гагаріна)). "
            "Термін розгляду заяви становить до 30 днів.",
            "Термін розгляду заяви становить до 30 днів.",
        ),
    ],
)
def test_intake_address_in_another_settlement_is_removed(text, expected):
    assert _drop_out_of_scope_access_sentences(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # An outage notice from a local source; the company name only contains the city name.
        "«Запорожьеэнерго» объявило об отключении света завтра в пяти округах.",
        # Mentions the other settlement without a service location.
        "В Запорожье прошёл митинг.",
        # A local district name is not an out-of-scope settlement.
        "Мелитопольский округ без света.",
        # An intake sentence with a local place stays.
        "Приём проходит в Бердянске, на Шевченко.",
    ],
)
def test_sentences_without_out_of_scope_service_location_are_unchanged(text):
    assert _drop_out_of_scope_access_sentences(text) == text
