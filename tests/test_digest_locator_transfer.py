"""A house or entrance number must not borrow a neighbouring clause's place (digest).

Regression for the 2026-11-21 digest: «На массиве АЗМОЛ света нет; по словам жителя,
его нет в 91 доме» and «В доме 97 по улице Орджоникидзе свет отключили, а в 7-м
подъезде света нет» put a number with no street of its own under another place.
"""

# ruff: noqa: S101
from __future__ import annotations

import pytest

from src.publication.digest_narrative import (
    _locator_transfer_violations,
    _place_locators,
    _split_visible_clauses,
)
from src.publication.digest_presentation import _load_digest_geography_resolver

AZMOL = "municipal:azmol"
NAHIRNA = "municipal:nahirna"


@pytest.fixture(scope="module")
def resolver():
    loaded = _load_digest_geography_resolver("berdyansk")
    assert loaded is not None
    return loaded


def _violations(sentence, candidates, resolver):
    return _locator_transfer_violations(
        _split_visible_clauses(sentence), resolver=resolver, candidates=candidates
    )


def test_locators_read_house_and_entrance_numbers():
    assert _place_locators("его нет в 91 доме") == frozenset({"дом:91"})
    assert _place_locators("В доме 97 по улице") == frozenset({"дом:97"})
    assert _place_locators("а в 7-м подъезде света нет") == frozenset({"подъезд:7"})
    assert _place_locators("вода есть в девятиэтажке") == frozenset()


def test_split_separates_semicolon_and_contrast_clauses():
    assert _split_visible_clauses(
        "На массиве АЗМОЛ света нет; по словам жителя, его нет в 91 доме."
    ) == ["На массиве АЗМОЛ света нет", "по словам жителя, его нет в 91 доме."]
    assert _split_visible_clauses(
        "В доме 97 по улице Орджоникидзе свет отключили, а в 7-м подъезде света нет."
    ) == ["В доме 97 по улице Орджоникидзе свет отключили", "в 7-м подъезде света нет."]


def test_unlocated_house_next_to_azmol_is_flagged(resolver):
    # The 91-house fact has no resolved area, so the Азмол sentence cannot lend it one.
    candidates = [("91 дом нет света", frozenset(), frozenset())]
    found = _violations(
        "На массиве АЗМОЛ света нет; по словам жителя, его нет в 91 доме.",
        candidates,
        resolver,
    )
    assert len(found) == 1
    assert "дом:91" in found[0]


def test_entrance_without_street_next_to_house_is_flagged(resolver):
    candidates = [
        ("97 Орджоникидзе тоже вырубили", frozenset(), frozenset()),
        ("в 7 подъезде нет света", frozenset(), frozenset()),
    ]
    found = _violations(
        "В доме 97 по улице Орджоникидзе свет отключили, а в 7-м подъезде света нет.",
        candidates,
        resolver,
    )
    assert len(found) == 1
    assert "подъезд:7" in found[0]


def test_house_bound_to_the_same_area_is_not_flagged(resolver):
    # «в 24 доме» is fine when its own fact is located in the sentence's area.
    candidates = [("24 дом на азмоле свет дали", frozenset(), frozenset({AZMOL}))]
    assert (
        _violations(
            "На Азмоле света нет, а в 24 доме свет есть.",
            candidates,
            resolver,
        )
        == []
    )


def test_house_located_elsewhere_is_flagged(resolver):
    candidates = [("91 дом нет света", frozenset(), frozenset({NAHIRNA}))]
    found = _violations("На массиве АЗМОЛ света нет; в 91 доме тоже нет.", candidates, resolver)
    assert len(found) == 1


def test_single_clause_is_never_flagged(resolver):
    candidates = [("91 дом нет света", frozenset(), frozenset())]
    assert _violations("В 91 доме нет света.", candidates, resolver) == []


def test_house_named_with_the_same_street_as_its_fact_is_not_flagged(resolver):
    # The street is unresolved on purpose (Орджоникидзе is an ambiguous alias), but
    # the fact and the neighbouring clause both name it, so the binding is shared.
    candidates = [("91 дом Орджоникидзе нет света", frozenset(), frozenset())]
    assert (
        _violations(
            "На улице Орджоникидзе свет дали, а в 91 доме нет.",
            candidates,
            resolver,
        )
        == []
    )


def test_entrance_still_flagged_next_to_a_named_street(resolver):
    candidates = [("в 7 подъезде нет света", frozenset(), frozenset())]
    found = _violations(
        "На улице Орджоникидзе свет дали, а в 7-м подъезде света нет.",
        candidates,
        resolver,
    )
    assert len(found) == 1
