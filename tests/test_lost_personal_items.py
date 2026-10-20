# ruff: noqa: S101
"""A private lost personal belonging is not city news; civic losses stay."""

from __future__ import annotations

import pytest

from src.publication.story_quality import is_non_editorial_fact


@pytest.mark.parametrize(
    "text",
    [
        "Утром в маршрутке №9 потеряли коричневую сумку с паспортом, ключами и помадой.",
        "В автобусе нашли ключи от квартиры.",
        "Забыл рюкзак на остановке «Автовокзал».",
    ],
)
def test_private_lost_belongings_are_not_digest_facts(text):
    assert is_non_editorial_fact(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "На улице Горького воды нет девятый день.",
        "Пропал свет на Пушкина.",
        "Потеряли воду на Колонии, жители сообщают.",
    ],
)
def test_civic_losses_stay_facts(text):
    assert is_non_editorial_fact(text) is False
