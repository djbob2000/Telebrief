"""Street-level geography confirmed by the editor (2026-11-21).

Орджоникидзе (Довганюка) and Димитрова (Софіївська) lie whole in Нагорная часть.
Труда lies in the lower city (у моря), not in Гора. The digest must never place
two of them in one district.
"""

# ruff: noqa: S101
from __future__ import annotations

import pytest

from src.publication.digest_presentation import (
    _load_digest_geography_resolver,
    _resolved_geographic_scopes,
)


@pytest.fixture(scope="module")
def resolver():
    loaded = _load_digest_geography_resolver("berdyansk")
    assert loaded is not None
    return loaded


@pytest.mark.parametrize(
    "text",
    [
        "На Орджоникидзе у мясного магазина света нет",
        "В доме 91 на Орджоникидзе света нет",
        "Орджоникидзе без воды",
        "На улице Довганюка нет света",
    ],
)
def test_ordzhonikidze_is_nahirna(resolver, text):
    assert list(_resolved_geographic_scopes(text, resolver)) == ["municipal:nahirna"]


@pytest.mark.parametrize("text", ["На улице Димитрова света нет", "Димитрова без воды"])
def test_dimitrova_is_nahirna(resolver, text):
    assert list(_resolved_geographic_scopes(text, resolver)) == ["municipal:nahirna"]


@pytest.mark.parametrize("text", ["На улице Труда электричество есть", "Труда без воды"])
def test_truda_is_lower_city(resolver, text):
    assert list(_resolved_geographic_scopes(text, resolver)) == ["colloquial:lower_city"]


def test_truda_and_ordzhonikidze_are_separate_areas(resolver):
    scopes = _resolved_geographic_scopes(
        "На улице Труда электричество есть, а на Орджоникидзе у мясного магазина нет",
        resolver,
    )
    assert set(scopes) == {"colloquial:lower_city", "municipal:nahirna"}
