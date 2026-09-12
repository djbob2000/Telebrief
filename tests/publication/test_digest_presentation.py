"""Tests for fact-driven digest presentation planning."""

import datetime as dt

import pytest

from src.editorial_models import StoryCard, StoryElement
from src.publication.city_situation import CitySituationItem, CitySituationRollup
from src.publication.digest_presentation import (
    DigestPresentationPlan,
    DigestPresentationUnit,
    RequiredDigestFact,
    _are_cards_merge_compatible,
    _compute_merge_groups,
    build_digest_presentation_plan,
    build_digest_presentation_units,
)
from src.publication.errors import DigestCoverageInvariantError
from src.publication.evidence import PublicationEvidence

_NOW = dt.datetime(2026, 9, 11, 16, 0, tzinfo=dt.timezone.utc)


@pytest.mark.unit
def test_digest_presentation_plan_to_audit_dict() -> None:
    fact = RequiredDigestFact(
        fact_id="center_voltage",
        rubric_id="infrastructure",
        subject_key="electricity",
        subject_label="Электроснабжение",
        story_ids=("story:1",),
        support_ids=("telegram:103",),
        text="в центре зафиксировано низкое напряжение около 170 В",
    )
    plan = DigestPresentationPlan(
        story_ids=("story:1",),
        required_facts=(fact,),
    )

    audit = plan.to_audit_dict()
    assert audit == {
        "story_ids": ["story:1"],
        "required_facts": [fact.to_dict()],
    }


def _build_run_44_fixture():
    candidate_cards = [
        StoryCard(
            id="story:1",
            topic="Электричество пропало в нескольких районах",
            importance="high",
            summary="По сообщениям жителей, свет отключили в нагорной части города и на Слободке. В центре города зафиксировано низкое напряжение — около 170 В.",
            rubric_id="infrastructure",
            story_kind="operational_status",
            hard_facts=[
                StoryElement(
                    text="свет отключили в нагорной части города",
                    source_refs=["telegram:101"],
                ),
                StoryElement(
                    text="на Слободке тоже 0 по свету",
                    source_refs=["telegram:102"],
                ),
                StoryElement(
                    text="в центре города зафиксировано низкое напряжение около 170 В",
                    source_refs=["telegram:103"],
                ),
            ],
        ),
        StoryCard(
            id="story:2",
            topic="Электроснабжение восстановлено на Петровского",
            importance="high",
            summary="В местных чатах подтверждают, что электричество вернулось на улицу Петровского.",
            rubric_id="infrastructure",
            story_kind="operational_status",
            hard_facts=[
                StoryElement(
                    text="электричество вернулось на улицу Петровского",
                    source_refs=["telegram:104"],
                ),
            ],
        ),
    ]

    city_rollup = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="electricity",
                subject_label="Электроснабжение",
                dimension="power_supply",
                state="UNAVAILABLE",
                location="Нагорная часть",
                entity="",
                detail="свет отключили в нагорной части города",
                source_refs=("telegram:101",),
                first_observed_at=_NOW,
                last_observed_at=_NOW,
                observation_count=1,
            ),
            CitySituationItem(
                subject_key="electricity",
                subject_label="Электроснабжение",
                dimension="power_supply",
                state="UNAVAILABLE",
                location="Слободка",
                entity="",
                detail="на Слободке тоже 0 по свету",
                source_refs=("telegram:102",),
                first_observed_at=_NOW,
                last_observed_at=_NOW,
                observation_count=1,
            ),
            CitySituationItem(
                subject_key="electricity",
                subject_label="Электроснабжение",
                dimension="power_supply",
                state="DEGRADED",
                location="Центр",
                entity="",
                detail="в центре города зафиксировано низкое напряжение около 170 В",
                source_refs=("telegram:103",),
                first_observed_at=_NOW,
                last_observed_at=_NOW,
                observation_count=1,
            ),
            CitySituationItem(
                subject_key="electricity",
                subject_label="Электроснабжение",
                dimension="power_supply",
                state="AVAILABLE",
                location="ул. Петровского",
                entity="",
                detail="электричество вернулось на улицу Петровского",
                source_refs=("telegram:104",),
                first_observed_at=_NOW,
                last_observed_at=_NOW,
                observation_count=1,
            ),
        )
    )

    evidence_map = {
        "story:1:evi:1": PublicationEvidence(
            evidence_id="story:1:evi:1",
            story_id=1,
            text="свет отключили в нагорной части города",
            source_text="свет отключили в нагорной части города",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=101,
            source_ref="telegram:101",
            source_id=1,
            source_item_id=101,
            source_role="primary",
            observed_at=_NOW,
        ),
        "story:1:evi:2": PublicationEvidence(
            evidence_id="story:1:evi:2",
            story_id=1,
            text="на Слободке тоже 0 по свету",
            source_text="на Слободке тоже 0 по свету",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=102,
            source_ref="telegram:102",
            source_id=1,
            source_item_id=102,
            source_role="primary",
            observed_at=_NOW,
        ),
        "story:1:evi:3": PublicationEvidence(
            evidence_id="story:1:evi:3",
            story_id=1,
            text="в центре города зафиксировано низкое напряжение около 170 В",
            source_text="в центре города зафиксировано низкое напряжение около 170 В",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=103,
            source_ref="telegram:103",
            source_id=1,
            source_item_id=103,
            source_role="primary",
            observed_at=_NOW,
        ),
        "story:2:evi:1": PublicationEvidence(
            evidence_id="story:2:evi:1",
            story_id=2,
            text="электричество вернулось на улицу Петровского",
            source_text="электричество вернулось на улицу Петровского",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=104,
            source_ref="telegram:104",
            source_id=1,
            source_item_id=104,
            source_role="primary",
            observed_at=_NOW,
        ),
    }

    return candidate_cards, city_rollup, evidence_map


@pytest.mark.unit
def test_build_digest_presentation_plan_extracts_required_facts() -> None:
    cards, city_rollup, evidence_map = _build_run_44_fixture()

    plan = build_digest_presentation_plan(
        cards=cards,
        city_situation=city_rollup,
        evidence=evidence_map,
    )

    assert plan.story_ids == ("story:1", "story:2")
    assert {f.fact_id for f in plan.required_facts} == {
        "нагорная_часть",
        "слободка",
        "center_voltage",
        "ул_петровского",
    }

    center = next(f for f in plan.required_facts if f.fact_id == "center_voltage")
    assert center.story_ids == ("story:1",)
    assert "telegram:103" in center.support_ids
    assert center.rubric_id == "infrastructure"


@pytest.mark.unit
def test_build_digest_presentation_plan_unmapped_fact_fails_closed() -> None:
    cards, _, evidence_map = _build_run_44_fixture()

    # City rollup has an unmapped water outage fact not related to any selected card
    unmapped_rollup = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="water",
                subject_label="Водоснабжение",
                dimension="water_supply",
                state="UNAVAILABLE",
                location="Колония",
                entity="Горводоканал",
                detail="Порыв водопровода в районе Колонии",
                source_refs=("telegram:999",),
                first_observed_at=_NOW,
                last_observed_at=_NOW,
                observation_count=1,
            ),
        )
    )

    with pytest.raises(DigestCoverageInvariantError) as exc_info:
        build_digest_presentation_plan(
            cards=cards,
            city_situation=unmapped_rollup,
            evidence=evidence_map,
        )

    assert "UNMAPPED_REQUIRED_FACT" in str(exc_info.value)


@pytest.mark.unit
def test_workaround_plumber_tank_does_not_merge_with_operational_water_status() -> None:
    card_status = StoryCard(
        id="story:1",
        topic="Водоснабжение",
        importance="medium",
        summary="На Самолёте вода есть со слабым напором",
        tags=["вода", "самолёт"],
        rubric_id="utilities",
        category="utilities",
    )
    card_workaround = StoryCard(
        id="story:2",
        topic="Услуги сантехника",
        importance="low",
        summary="Установка накопительных баков и насосов для воды",
        tags=["накопительные баки", "насосы", "сантехник"],
        rubric_id="utilities",
        category="utilities",
    )
    assert not _are_cards_merge_compatible(card_status, card_workaround)


@pytest.mark.unit
def test_homogeneous_power_status_merges_up_to_6() -> None:
    cards = [
        StoryCard(
            id=f"story:p{i}",
            topic=f"Свет на районе {i}",
            importance="medium",
            summary=f"В районе {i} дали свет после отключения",
            tags=["свет"],
            rubric_id="utilities",
            category="utilities",
        )
        for i in range(1, 8)
    ]
    groups = _compute_merge_groups(cards)
    mg1 = groups["story:p1"]
    for i in range(2, 7):
        assert groups[f"story:p{i}"] == mg1
    assert groups["story:p7"] != mg1


@pytest.mark.unit
def test_digest_compression_units_power_cluster_becomes_synthesis_unit() -> None:
    cards = [
        StoryCard(
            id=f"story:power:{i}",
            topic=f"Отключение света в районе {i}",
            importance="high",
            summary=f"В районе {i} электричество отсутствует более суток",
            tags=["electricity", "power", "blackout"],
            rubric_id="utilities",
            category="utilities",
        )
        for i in range(1, 18)
    ]

    units = build_digest_presentation_units(cards)
    assert len(units) <= 2
    assert all(isinstance(u, DigestPresentationUnit) for u in units)
    assert all(u.kind == "SYNTHESIS" for u in units)
    assert all(u.rubric_id == "utilities" for u in units)
    all_story_ids = [sid for u in units for sid in u.story_ids]
    assert set(all_story_ids) == {c.id for c in cards}
    assert len(all_story_ids) == len(cards)


@pytest.mark.unit
def test_build_digest_presentation_plan_without_city_situation() -> None:
    cards, _, evidence_map = _build_run_44_fixture()

    plan = build_digest_presentation_plan(
        cards=cards,
        evidence=evidence_map,
        city_situation=None,
    )

    assert plan.story_ids == ("story:1", "story:2")
    assert len(plan.required_facts) == 4
    assert {f.fact_id for f in plan.required_facts} == {
        "нагорная_часть",
        "слободка",
        "center_voltage",
        "ул_петровского",
    }
    for fact in plan.required_facts:
        assert fact.story_ids in (("story:1",), ("story:2",))
        assert len(fact.support_ids) > 0
