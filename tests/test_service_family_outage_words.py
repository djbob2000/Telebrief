# ruff: noqa: S101
"""Residents name a power cut by its sign or a provider by its name, not by «свет»."""

from __future__ import annotations

import pytest

from src.domain.service_taxonomy import detect_service_families, profile_telecom_provider_names


@pytest.mark.parametrize(
    ("text", "families"),
    [
        ("Центр точно потух", {"power"}),
        ("Всё погасло", {"power"}),
        ("На генераторах", {"power"}),
        ("Сутки мигало, и стабилизатор не помог", {"power"}),
        # The remark names what went out, so it is not a power cut.
        ("Юпитер у всех погас", {"telecom"}),
        ("Юпитер точно не работает", {"telecom"}),
        # Paying off a debt is not a light going out.
        ("Надо погасить кредит", set()),
        ("По всему городу нет", set()),
    ],
)
def test_outage_signs_and_providers(text, families):
    assert set(detect_service_families(text)) == families


def test_provider_names_come_from_profiles_and_skip_multiword_aliases():
    names = profile_telecom_provider_names()
    assert {"юпитер", "онет"} <= names
    # «Юпитер Интернет» and «ONet Group» are aliases, not names of their own.
    assert "group" not in names
    assert all(" " not in name for name in names)
