# ruff: noqa: S101

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from src.editorial_models import StoryCard, StoryElement
from src.publication.city_situation import CitySituationItem, CitySituationRollup
from src.publication.digest_presentation import (
    DigestPresentationPlan,
    RequiredDigestFact,
    build_required_digest_facts,
)
from src.publication.errors import DigestCoverageInvariantError

_NOW = dt.datetime(2026, 10, 2, 12, tzinfo=dt.timezone.utc)


def _situation_item(
    support_id: str,
    detail: str,
    *,
    location: str = "Центр",
    subject_key: str = "electricity",
    subject_label: str = "Электроснабжение",
    state: str = "UNAVAILABLE",
    fact_id: str = "",
) -> CitySituationItem:
    return CitySituationItem(
        subject_key=subject_key,
        subject_label=subject_label,
        dimension="availability",
        location=location,
        entity="",
        state=state,
        detail=detail,
        source_refs=(support_id,),
        first_observed_at=_NOW - dt.timedelta(hours=1),
        last_observed_at=_NOW,
        observation_count=1,
        current_source_refs=(support_id,),
        fact_id=fact_id,
    )


def _build_situation(items: tuple[CitySituationItem, ...]):
    cards = tuple(
        StoryCard(
            id=f"story:{item.source_refs[0]}",
            topic=item.subject_label,
            importance="high",
            summary="A local service report",
            representative_source_refs=[item.source_refs[0]],
            rubric_id=("communal" if item.subject_key == "water" else "utilities"),
            story_kind="operational_status",
        )
        for item in items
    )
    return build_required_digest_facts(
        cards=cards,
        city_situation=CitySituationRollup(items=items),
    )


def _fact_by_support(facts: tuple[RequiredDigestFact, ...]) -> dict[str, str]:
    return {fact.support_ids[0]: fact.fact_id for fact in facts}


def test_situation_fact_ids_are_topic_scoped_and_stable_when_item_order_changes() -> None:
    electricity = _situation_item(
        "ref:electricity", "Жители сообщили об отключении электричества до 18:00"
    )
    water = _situation_item(
        "ref:water",
        "Жители сообщили об ограничении доступности услуги до 18:00",
        subject_key="water",
        subject_label="Водоснабжение",
    )
    first_order = _build_situation((electricity, water))
    reverse_order = _build_situation((water, electricity))

    assert len(first_order) == 2
    assert len({fact.fact_id for fact in first_order}) == 2
    assert {fact.rubric_id for fact in first_order} == {"utilities", "communal"}
    assert _fact_by_support(first_order) == _fact_by_support(reverse_order)
    assert all("_" in fact.fact_id for fact in first_order)


def test_situation_fact_ids_include_full_detail_and_preserve_explicit_ids() -> None:
    earlier = _situation_item(
        "ref:earlier",
        "Жители сообщили об отключении электричества на улице до 18:00",
        location="Набережная",
    )
    later = _situation_item(
        "ref:later",
        "Жители сообщили об отключении электричества на улице до 20:00",
        location="Набережная",
    )
    supplied = _situation_item(
        "ref:supplied",
        "Жители сообщили о напряжении в сети до вечера",
        location="Набережная",
        fact_id="  upstream-fact-17  ",
    )
    facts = _build_situation((earlier, later, supplied))

    assert len(facts) == 3
    assert len({fact.fact_id for fact in facts}) == 3
    assert _fact_by_support(facts)["ref:supplied"] == "upstream-fact-17"


def test_duplicate_explicit_situation_fact_ids_fail_with_safe_code() -> None:
    first = _situation_item(
        "ref:first", "Жители сообщили об отключении света на улице", fact_id="shared-id"
    )
    second = _situation_item(
        "ref:second",
        "Жители сообщили о восстановлении подачи воды на улице",
        subject_key="water",
        subject_label="Водоснабжение",
        fact_id="shared-id",
    )

    with pytest.raises(DigestCoverageInvariantError, match="DIGEST_DUPLICATE_REQUIRED_FACT_ID"):
        _build_situation((first, second))


def test_unmapped_situation_fact_still_fails_with_safe_diagnostic() -> None:
    item = _situation_item("ref:unmapped", "Жители сообщили об отключении света на улице")

    with pytest.raises(DigestCoverageInvariantError, match="UNMAPPED_REQUIRED_FACT:"):
        build_required_digest_facts(
            cards=(),
            city_situation=CitySituationRollup(items=(item,)),
        )


def test_generated_id_avoids_an_explicit_id_reserved_later_in_input() -> None:
    generated_item = _situation_item(
        "ref:collision", "Жители сообщили об отключении электричества до 18:00"
    )
    generated_id = _build_situation((generated_item,))[0].fact_id
    explicit_item = _situation_item(
        "ref:collision",
        "Жители сообщили об отключении электричества до 18:00",
        fact_id=f"{generated_id}_2",
    )

    facts = _build_situation((generated_item, generated_item, explicit_item))

    assert len(facts) == 3
    assert {fact.fact_id for fact in facts} == {
        generated_id,
        f"{generated_id}_2",
        f"{generated_id}_3",
    }


def test_three_identical_event_first_observations_remain_distinct() -> None:
    observations = [
        SimpleNamespace(
            location="Центр",
            detail="На центральной улице напряжение 170В вечером",
            source_refs=["ref:identical"],
            state="DEGRADED",
            subject_key="electricity",
            subject_label="Электроснабжение",
            effective_from=None,
        )
        for _ in range(3)
    ]
    card = StoryCard(
        id="story:voltage",
        topic="Электроснабжение",
        importance="high",
        summary="Voltage report",
        representative_source_refs=["ref:identical"],
        rubric_id="infrastructure",
        story_kind="operational_status",
    )
    card.operational_observations = observations

    facts = build_required_digest_facts(cards=(card,))

    assert len(facts) == 3
    assert len({fact.fact_id for fact in facts}) == 3
    assert all(fact.fact_id != "center_voltage" for fact in facts)


def test_event_first_observation_and_hard_fact_ids_follow_content_not_list_index() -> None:
    observations = [
        SimpleNamespace(
            location="Центр",
            detail=detail,
            source_refs=[ref],
            state="DEGRADED",
            subject_key="electricity",
            subject_label="Электроснабжение",
            effective_from=None,
        )
        for ref, detail in (
            ("ref:observation-a", "На центральной улице напряжение 170В вечером"),
            ("ref:observation-b", "На центральной улице напряжение 170В утром"),
        )
    ]
    card = StoryCard(
        id="story:voltage",
        topic="Электроснабжение",
        importance="high",
        summary="Voltage report",
        representative_source_refs=["ref:observation-a", "ref:observation-b"],
        rubric_id="infrastructure",
        story_kind="operational_status",
    )
    card.operational_observations = observations
    observation_ids = _fact_by_support(build_required_digest_facts(cards=(card,)))
    card.operational_observations.reverse()
    reversed_observation_ids = _fact_by_support(build_required_digest_facts(cards=(card,)))
    assert observation_ids == reversed_observation_ids
    assert len(set(observation_ids.values())) == 2

    hard_fact_texts = (
        "Городской автобусный маршрут №4 продолжает ходить примерно раз в час вечером",
        "Городской автобусный маршрут №4 продолжает ходить примерно раз в час утром",
    )
    hard_cards = tuple(
        StoryCard(
            id=f"story:{ref}",
            topic="Электроснабжение",
            importance="high",
            summary="Voltage report",
            representative_source_refs=[ref],
            rubric_id="infrastructure",
            story_kind="operational_status",
            hard_facts=[StoryElement(text=text, source_refs=[ref])],
        )
        for ref, text in zip(("ref:hard-a", "ref:hard-b"), hard_fact_texts)
    )
    forward_hard_ids = _fact_by_support(build_required_digest_facts(cards=hard_cards))
    reverse_hard_ids = _fact_by_support(build_required_digest_facts(cards=hard_cards[::-1]))

    assert forward_hard_ids == reverse_hard_ids
    assert len(set(forward_hard_ids.values())) == 2


def test_composition_rejects_duplicate_fact_records_before_mapping() -> None:
    plan = DigestPresentationPlan()
    record = SimpleNamespace(
        fact_id="duplicate-record",
        support_ids=("ref:support",),
        rubric_id="utilities",
        canonical_subject="electricity",
        story_ids=("story:one",),
        text="An electrical service report",
        original_location="Центр",
        canonical_area="",
        observed_time=None,
        effective_time=None,
        service_state="UNAVAILABLE",
        epistemic_kind="community_report",
        source_publication_time=None,
    )
    composition = SimpleNamespace(
        admitted_story_ids=frozenset({"story:one"}),
        admitted_fact_ids=frozenset({"duplicate-record"}),
        fact_records=(record, record),
    )

    with pytest.raises(DigestCoverageInvariantError, match="DIGEST_DUPLICATE_COMPOSITION_FACT_ID"):
        plan.with_composition(composition)


def test_presentation_plan_rejects_duplicate_required_fact_ids() -> None:
    fact = RequiredDigestFact(
        fact_id="duplicate-required",
        rubric_id="utilities",
        subject_key="electricity",
        subject_label="Электроснабжение",
        story_ids=("story:one",),
        support_ids=("ref:support",),
        text="An electrical service report",
    )
    plan = DigestPresentationPlan(required_facts=(fact, fact))
    composition = SimpleNamespace(
        admitted_story_ids=frozenset({"story:one"}),
        admitted_fact_ids=frozenset({"duplicate-required"}),
        fact_records=(),
    )

    with pytest.raises(DigestCoverageInvariantError, match="DIGEST_DUPLICATE_REQUIRED_FACT_ID"):
        plan.with_composition(composition)
