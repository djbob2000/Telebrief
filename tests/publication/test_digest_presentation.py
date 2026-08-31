import json
from pathlib import Path


def test_city_life_short_read_golden_fixture_has_required_cases() -> None:
    path = Path(__file__).parents[1] / "fixtures" / "city_life_short_read_digest_golden.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = {case["id"] for case in data["cases"]}
    assert {
        "rich_day_dashboard_is_capped_without_losing_detail_story",
        "microdetails_survive_thematic_compression",
        "unrelated_small_stories_remain_separate",
        "non_operational_city_life_stays_out_of_dashboard",
    } <= ids


def test_plan_city_situation_presentation_groups_same_subject_and_dimension() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime.now(dt.timezone.utc)
    items = (
        CitySituationItem(
            subject_key="water_supply",
            subject_label="Водоснабжение",
            dimension="availability",
            location="Азмол",
            entity="горводоканал",
            state="UNAVAILABLE",
            detail="Воды нет третий день",
            source_refs=("ref-water-1",),
            first_observed_at=now,
            last_observed_at=now,
            observation_count=1,
        ),
        CitySituationItem(
            subject_key="water_supply",
            subject_label="Водоснабжение",
            dimension="availability",
            location="АКЗ",
            entity="горводоканал",
            state="DEGRADED",
            detail="Слабый напор",
            source_refs=("ref-water-2",),
            first_observed_at=now,
            last_observed_at=now,
            observation_count=1,
        ),
    )
    rollup = CitySituationRollup(items=items)
    plan = plan_city_situation_presentation(rollup, max_items=7, max_details_per_item=2)
    assert len(plan.groups) == 1
    grp = plan.groups[0]
    assert grp.group_kind == "subject_status"
    assert grp.subject_key == "water_supply"
    assert grp.state == "UNAVAILABLE"
    assert set(grp.source_refs) == {"ref-water-1", "ref-water-2"}
    assert len(grp.detail_lines) == 2
    assert set(plan.covered_source_refs) == {"ref-water-1", "ref-water-2"}


def test_plan_city_situation_presentation_caps_at_max_items_and_tracks_covered_refs() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime.now(dt.timezone.utc)
    items = tuple(
        CitySituationItem(
            subject_key=f"subject_{i}",
            subject_label=f"Служба {i}",
            dimension="availability",
            location="Город",
            entity=f"entity_{i}",
            state="DEGRADED",
            detail=f"Проблема {i}",
            source_refs=(f"ref-{i}",),
            first_observed_at=now,
            last_observed_at=now - dt.timedelta(minutes=i),
            observation_count=1,
        )
        for i in range(9)
    )
    rollup = CitySituationRollup(items=items)
    plan = plan_city_situation_presentation(rollup, max_items=7, max_details_per_item=2)
    assert len(plan.groups) == 7
    # Only the 7 selected items' source refs are in covered_source_refs
    assert len(plan.covered_source_refs) == 7
    for i in range(7):
        assert f"ref-{i}" in plan.covered_source_refs
    assert "ref-7" not in plan.covered_source_refs
    assert "ref-8" not in plan.covered_source_refs


def test_plan_city_situation_presentation_reserves_slot_for_available_bundle() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime.now(dt.timezone.utc)
    items = [
        CitySituationItem(
            subject_key=f"degraded_{i}",
            subject_label=f"Деградировавшая служба {i}",
            dimension="availability",
            location="Город",
            entity=f"entity_{i}",
            state="DEGRADED",
            detail=f"Проблема {i}",
            source_refs=(f"ref-deg-{i}",),
            first_observed_at=now,
            last_observed_at=now,
            observation_count=1,
        )
        for i in range(7)
    ]
    items.extend(
        [
            CitySituationItem(
                subject_key="banks",
                subject_label="Банки",
                dimension="availability",
                location="Центр",
                entity="Сбербанк",
                state="AVAILABLE",
                detail="Отделения работают штатно",
                source_refs=("ref-avail-1",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
            ),
            CitySituationItem(
                subject_key="transport",
                subject_label="Транспорт",
                dimension="availability",
                location="Город",
                entity="Автобусы",
                state="RESOLVED",
                detail="Движение по расписанию",
                source_refs=("ref-avail-2",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
            ),
        ]
    )
    rollup = CitySituationRollup(items=tuple(items))
    plan = plan_city_situation_presentation(rollup, max_items=7, max_details_per_item=2)
    assert len(plan.groups) == 7
    kinds = [g.group_kind for g in plan.groups]
    assert kinds.count("available_services") == 1
    assert kinds.count("subject_status") == 6
    avail_grp = next(g for g in plan.groups if g.group_kind == "available_services")
    assert set(avail_grp.source_refs) == {"ref-avail-1", "ref-avail-2"}
    assert "ref-avail-1" in plan.covered_source_refs
    assert "ref-avail-2" in plan.covered_source_refs
