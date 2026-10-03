from __future__ import annotations

# ruff: noqa: S101
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from src.editorial_models import StoryCard
from src.publication.digest_composition import _core_service_domains, build_digest_composition
from src.publication.digest_presentation import DigestPresentationPlan, RequiredDigestFact


def _candidate(
    story_id: str,
    rubric_id: str,
    subject_key: str,
    text: str,
    *,
    fact_rubric: str | None = None,
    epistemic_kind: str = "service_access",
) -> tuple[StoryCard, RequiredDigestFact]:
    support_id = f"source:{story_id}"
    card = StoryCard(
        id=story_id,
        topic="community update",
        importance="high",
        summary="A reported local update.",
        representative_source_refs=[support_id],
        rubric_id=rubric_id,
        category="local",
    )
    fact = RequiredDigestFact(
        fact_id=f"fact:{story_id}",
        rubric_id=fact_rubric or rubric_id,
        subject_key=subject_key,
        subject_label=subject_key,
        story_ids=(story_id,),
        support_ids=(support_id,),
        text=text,
        epistemic_kind=epistemic_kind,
    )
    return card, fact


def _compose(candidates, *, max_chars: int, reserved_chars: int = 70):
    cards = tuple(card for card, _ in candidates)
    facts = tuple(fact for _, fact in candidates)
    return build_digest_composition(
        DigestPresentationPlan(
            story_ids=tuple(card.id for card in cards),
            required_facts=facts,
        ),
        cards,
        {},
        edition_slug="",
        snapshot_at=None,
        max_chars=max_chars,
        reserved_chars=reserved_chars,
        include_statistics=False,
        rubric_labels={
            "infrastructure": "Infrastructure",
            "education": "Education",
            "economy": "Commerce",
            "culture": "Culture",
            "media": "Media",
            "communications": "Communications",
            "mobility": "Transport",
            "zzz_core": "Core service",
        },
    )


def test_rubric_and_incidental_landmark_do_not_supply_a_core_service_domain() -> None:
    advert = _candidate(
        "story:watch", "civic_services", "repair", "Повторяется объявление «Ремонт часов»."
    )
    power = _candidate(
        "story:power",
        "infrastructure",
        "electricity",
        "Возле городской поликлиники вторые сутки нет света.",
    )
    result = _compose([advert, power], max_chars=4096)
    records = {record.story_ids[0]: record for record in result.fact_records}

    assert _core_service_domains(records["story:watch"]) == set()
    assert _core_service_domains(records["story:power"]) == {"power"}


def test_domain_breadth_does_not_defer_a_second_critical_service_report() -> None:
    critical = [
        _candidate(
            f"story:critical-{index}",
            "infrastructure",
            "electricity",
            f"На улице {index} нет света.",
        )
        for index in range(2)
    ]
    critical = [
        (SimpleNamespace(**{**asdict(card), "importance": "critical"}), fact)
        for card, fact in critical
    ]
    water = _candidate("story:water", "infrastructure", "water", "Вода подаётся ночью.")

    result = _compose([*critical, water], max_chars=310)

    assert {card.id for card, _ in critical} <= result.admitted_story_ids
    assert result.failure_reason == ""


def test_core_service_facts_are_admitted_before_secondary_rubric_breadth() -> None:
    power_reports = [
        _candidate(
            f"story:power-{index}",
            "community",
            "electricity",
            f"Жители сообщают, что на улице {street} нет электричества уже вторые сутки; сроки восстановления не названы.",
            fact_rubric="infrastructure",
        )
        for index, street in enumerate(("Центральной", "Горбенко", "Шевченко", "Ленина"))
    ]
    water_report = _candidate(
        "story:water",
        "community",
        "water",
        "Жители сообщают: вода подаётся на верхние этажи только ночью и раз в 5–6 дней.",
        fact_rubric="infrastructure",
    )
    course = _candidate(
        "story:course",
        "education",
        "enrollment",
        "Городская спортивная школа открыла бесплатный набор детей перед началом учебного года.",
        epistemic_kind="community_report",
    )
    shop = _candidate(
        "story:shop",
        "economy",
        "retail",
        "Магазин сообщил о продаже сезонных товаров и указал часы работы на этой неделе.",
        epistemic_kind="community_report",
    )
    candidates = [*power_reports, water_report, course, shop]

    result = _compose(candidates, max_chars=800, reserved_chars=200)

    core_story_ids = {card.id for card, _ in (*power_reports, water_report)}
    secondary_story_ids = {course[0].id, shop[0].id}
    assert "story:water" in result.admitted_story_ids
    assert len(core_story_ids.intersection(result.admitted_story_ids)) >= 2
    assert not secondary_story_ids.intersection(result.admitted_story_ids)
    assert "fact:story:water" in result.admitted_fact_ids
    water_unit = next(unit for unit in result.units if "story:water" in unit.story_ids)
    assert water_unit.support_ids == ("source:story:water",)
    assert water_unit.fact_ids == ("fact:story:water",)
    assert result.estimated_visible_character_count <= 800


def test_useful_single_source_nonutility_fact_is_admitted_when_budget_has_room() -> None:
    course = _candidate(
        "story:course",
        "education",
        "enrollment",
        "Городская спортивная школа открыла бесплатный набор детей перед началом учебного года.",
        epistemic_kind="community_report",
    )

    result = _compose([course], max_chars=4096)

    assert result.admitted_story_ids == frozenset({"story:course"})
    assert result.admitted_fact_ids == frozenset({"fact:story:course"})
    assert result.units[0].support_ids == ("source:story:course",)
    disposition = next(item for item in result.dispositions if item.candidate_id == "story:course")
    assert disposition.disposition == "selected"


def test_power_reports_do_not_consume_the_budget_before_other_core_services() -> None:
    power = [
        _candidate(
            f"story:power-{index}",
            "infrastructure",
            "electricity",
            f"На улице {street} жители сообщают, что света нет уже вторые сутки.",
        )
        for index, street in enumerate(
            ("Крылова", "Баха", "Тургенева", "Шевченко", "Ленина", "Горбенко", "РТС", "АКЗ")
        )
    ]
    water = _candidate(
        "story:water",
        "infrastructure",
        "water",
        "Вода подаётся на верхние этажи только ночью раз в 5–6 дней.",
    )
    transit = _candidate(
        "story:transit", "mobility", "transport", "Автобус №4 ходит примерно раз в час."
    )
    connection = _candidate(
        "story:connection",
        "communications",
        "connectivity",
        "На Орджоникидзе не работают мобильная связь и интернет.",
    )
    result = _compose([*power, water, transit, connection], max_chars=850, reserved_chars=128)
    assert {"story:water", "story:transit", "story:connection"} <= result.admitted_story_ids
    assert any(card.id in result.admitted_story_ids for card, _ in power)
    assert result.estimated_visible_character_count <= 850


@pytest.mark.parametrize(
    "service_text",
    (
        "Вода подаётся только ночью.",
        "Воды нет на верхних этажах.",
        "На улице нет света уже сутки.",
        "Подача газа восстановлена.",
        "Мобильная связь работает с перебоями.",
        "Автобус №4 ходит примерно раз в час.",
    ),
    ids=("water-supply", "water-absent", "power", "gas", "connectivity", "transit"),
)
@pytest.mark.parametrize(
    ("secondary_id", "rubric_id", "subject_key", "secondary_text"),
    (
        (
            "story:warm-clothes",
            "economy",
            "retail",
            "Магазин продаёт теплую одежду и куртки.",
        ),
        (
            "story:driver-course",
            "education",
            "driver_course",
            "Открыты курсы для водителей категории B.",
        ),
        (
            "story:art-announcement",
            "culture",
            "art",
            "Анонс выставки, связанной с историей города.",
        ),
        (
            "story:newspaper",
            "media",
            "local_media",
            "Городская газета опубликовала афишу на выходные.",
        ),
    ),
    ids=("warm-clothing", "driver-course", "related-art", "newspaper"),
)
def test_russian_lookalikes_do_not_starve_grounded_service_facts(
    service_text: str,
    secondary_id: str,
    rubric_id: str,
    subject_key: str,
    secondary_text: str,
) -> None:
    service = _candidate(
        "story:grounded-service",
        "community",
        "local",
        service_text,
        fact_rubric="zzz_core",
    )
    secondary = _candidate(
        secondary_id,
        rubric_id,
        subject_key,
        secondary_text,
        epistemic_kind="community_report",
    )

    result = _compose([secondary, service], max_chars=270, reserved_chars=100)

    assert "story:grounded-service" in result.admitted_story_ids
    assert secondary_id not in result.admitted_story_ids
