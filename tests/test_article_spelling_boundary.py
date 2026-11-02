# ruff: noqa: S101
"""Evidence Boundary: faithful spelling variants pass, invented names and quotes still block."""

from __future__ import annotations

import pytest

from src.publication.article_claims import find_unsupported_claims, fold_apostrophes
from src.publication.article_semantic_support import assess_semantic_support


@pytest.mark.parametrize(
    ("support", "writer"),
    [
        ("На Горе света нет с утра.", "На ГОРЕ света нет с утра."),
        ("Ёлочная улица без света.", "Елочная улица без света."),
        ("Об этом сообщила Мар'їна з вулиці.", "Об этом сообщила Марїна з вулиці."),
        ("Житель Бур'ян написал.", "Житель Бурян написал."),
        ("Про это написала Іванова.", "Про это написала Иванова."),
        ("Сказав Ґудзь з району.", "Сказал Гудзь из района."),
        ("Сообщил Горбенко из района.", "Сообщил о Горбенко."),
    ],
)
def test_faithful_spelling_variant_does_not_block_names(support: str, writer: str):
    signals = assess_semantic_support(writer, [support])

    assert signals.blocking_proper_names == ()
    assert not find_unsupported_claims(writer, [support], direct_quote_source_texts=[support])


@pytest.mark.parametrize(
    ("support", "writer"),
    [
        ("Житель написал: «Свет дали на час».", "Житель написал: “Свет дали на час”."),
        ("Житель написал: «Акз-ртс света нет».", "Житель написал: «Акз ртс света нет»."),
        ("Житель написал: «Всё ещё нет света».", "Житель написал: «Все еще нет света»."),
        ("Житель написал: «Мар'їна чекає».", "Житель написал: «Марїна чекає»."),
    ],
)
def test_faithful_quote_spelling_variant_passes(support: str, writer: str):
    assert not find_unsupported_claims(writer, [support], direct_quote_source_texts=[support])


def test_invented_surname_still_blocks():
    support = "Сообщил житель района."
    writer = "Сообщил Петренко из района."

    signals = assess_semantic_support(writer, [support])

    assert "петренко" in signals.blocking_proper_names


def test_invented_quote_wording_still_blocks():
    support = "Житель написал: «Свет дали на час»."
    writer = "Житель написал: «Свет дали на три часа»."

    unsupported = find_unsupported_claims(writer, [support], direct_quote_source_texts=[support])

    assert [claim.kind for claim in unsupported] == ["direct_quote"]


def test_fold_apostrophes_only_inside_words():
    assert fold_apostrophes("Мар'їна") == "Марїна"
    assert fold_apostrophes("Мар’їна ' ок") == "Марїна ' ок"
