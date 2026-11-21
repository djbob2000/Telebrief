"""Colloquial building names resolve only in a locative context (city profile).

«Крылышки» is a resident nickname of the building at ул. Правды, 7 (Нагорная
часть). The word is also an ordinary noun («куриные крылышки»), so the profile
alias is a context-required stem: it resolves after «в»/«на»/«у», and only then.
"""

# ruff: noqa: S101
from __future__ import annotations

import pytest

from src.publication.digest_presentation import (
    _load_digest_geography_resolver,
    _resolved_geographic_scopes,
)

NAHIRNA = "municipal:nahirna"


@pytest.fixture(scope="module")
def resolver():
    loaded = _load_digest_geography_resolver("berdyansk")
    assert loaded is not None
    return loaded


@pytest.mark.parametrize(
    "text",
    [
        "В «Крылышках» без воды четыре месяца",
        "Я сам в «крылышках» живу, воды нет",
        "В крылышках нет света",
        "в «Крылышке» тоже нет лифта",
    ],
)
def test_locative_nickname_resolves_to_pravdy_street_area(resolver, text):
    assert NAHIRNA in _resolved_geographic_scopes(text, resolver)


@pytest.mark.parametrize(
    "text",
    [
        "Куриные крылышки, окорочка запечённые",
        "Продам крылья и крылышки к куриным блюдам",
        "Кур. крылья 100 г. - 1900 ₽",
    ],
)
def test_ordinary_noun_does_not_resolve(resolver, text):
    assert _resolved_geographic_scopes(text, resolver) == {}


def test_street_name_still_resolves(resolver):
    assert NAHIRNA in _resolved_geographic_scopes("На Правды света нет", resolver)
