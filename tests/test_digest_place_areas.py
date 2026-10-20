# ruff: noqa: S101
"""Digest writer material names the profile district of a place, and only that."""

from __future__ import annotations

from src.publication.digest_place_areas import place_area_hints


def test_russian_street_name_gets_its_profile_district():
    hints = place_area_hints("berdyansk", "вау свет дали по Маяковского")
    assert len(hints) == 1
    assert hints[0].startswith("Маяковського:")
    assert "Колонія-Макорти" in hints[0]


def test_street_is_hinted_again_when_the_text_repeats_it():
    hints = place_area_hints("berdyansk", "на Маяковского света нет")
    assert [hint.split(":")[0] for hint in hints] == ["Маяковського"]


def test_text_without_a_profile_place_has_no_hint():
    assert place_area_hints("berdyansk", "свет есть у всех") == ()


def test_unknown_or_unsafe_edition_slug_has_no_hint():
    assert place_area_hints("no-such-city", "по Маяковского") == ()
    assert place_area_hints("../berdyansk", "по Маяковского") == ()
    assert place_area_hints("", "по Маяковского") == ()
