# ruff: noqa: S101
"""Deterministic presentation-rubric priority rules."""

from __future__ import annotations

import asyncio

import pytest

from src.config_loader import DigestRubricConfig, DigestRubricsConfig
from src.editorial_models import StoryCard
from src.publication.rubrics import DigestRubricClassifier


def _rubrics(*rubric_ids: str, fallback_id: str = "other") -> DigestRubricsConfig:
    items = [
        DigestRubricConfig(
            id=rubric_id,
            name=rubric_id,
            description=rubric_id,
            fallback=rubric_id == fallback_id,
        )
        for rubric_id in rubric_ids
    ]
    if fallback_id not in rubric_ids:
        items.append(
            DigestRubricConfig(
                id=fallback_id,
                name=fallback_id,
                description=fallback_id,
                fallback=True,
            )
        )
    return DigestRubricsConfig(items=tuple(items))


def _classify(card: StoryCard, rubrics: DigestRubricsConfig) -> str:
    classified, _ = asyncio.run(DigestRubricClassifier().classify([card], rubrics=rubrics))
    return classified[0].rubric_id


def _card(*, topic: str, summary: str, category: str) -> StoryCard:
    return StoryCard(
        id="story-1",
        topic=topic,
        importance="medium",
        summary=summary,
        category=category,
    )


def test_current_dog_training_course_overrides_legacy_economy_category() -> None:
    card = _card(
        topic="Курс дрессировки собак в ДОСААФ",
        summary="В Бердянске открыт курс дрессировки собак в ДОСААФ",
        category="economy",
    )

    actual = _classify(card, _rubrics("economy", "education_culture"))

    assert actual == "education_culture"


def test_safety_event_during_courses_keeps_safety_priority() -> None:
    card = _card(
        topic="Пожар во время курсов в учебном корпусе",
        summary="Во время занятий в здании произошёл пожар",
        category="education_culture",
    )

    actual = _classify(card, _rubrics("education_culture", "safety"))

    assert actual == "safety"


def test_currency_exchange_rate_course_remains_in_economy() -> None:
    card = _card(
        topic="Курс доллара и евро в обменных пунктах",
        summary="Обменники обновили курсы валют на сегодня",
        category="economy",
    )

    actual = _classify(card, _rubrics("economy", "education_culture"))

    assert actual == "economy"


@pytest.mark.parametrize("price", ["500 рублей", "50 долларов"], ids=["rubles", "dollars"])
def test_paid_dog_training_course_remains_in_education(price: str) -> None:
    card = _card(
        topic="Платный курс дрессировки собак в ДОСААФ",
        summary=f"Жителей приглашают на курс дрессировки собак стоимостью {price}",
        category="economy",
    )

    actual = _classify(card, _rubrics("economy", "education_culture"))

    assert actual == "education_culture"


def test_past_schooling_mention_does_not_reclassify_economic_story() -> None:
    card = _card(
        topic="Владелец магазина расширяет торговлю",
        summary="Предприниматель, который учился в школе Бердянска, открыл новый магазин",
        category="economy",
    )

    actual = _classify(card, _rubrics("economy", "education_culture"))

    assert actual == "economy"


def test_bus_route_course_context_remains_in_mobility() -> None:
    card = _card(
        topic="Автобус №4 вернулся на обычный курс",
        summary="Маршрут снова проходит по прежней траектории",
        category="transport",
    )

    actual = _classify(card, _rubrics("mobility", "education_culture"))

    assert actual == "mobility"


def test_education_override_uses_a_configured_compatible_rubric_id() -> None:
    card = _card(
        topic="Открыт набор на курсы дрессировки собак",
        summary="ДОСААФ приглашает жителей на обучение",
        category="economy",
    )

    actual = _classify(card, _rubrics("economy", "education"))

    assert actual == "education"


def test_missing_education_destination_keeps_the_valid_fallback() -> None:
    card = _card(
        topic="Открыт набор на курсы дрессировки собак",
        summary="ДОСААФ приглашает жителей на обучение",
        category="economy",
    )

    actual = _classify(card, _rubrics("other"))

    assert actual == "other"
