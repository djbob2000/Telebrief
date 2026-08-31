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


def test_build_digest_presentation_plan_preserves_omitted_operational_story() -> None:
    import datetime as dt

    from src.editorial_models import StoryCard, StoryElement
    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import build_digest_presentation_plan

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
        for i in range(8)
    )
    rollup = CitySituationRollup(items=items)

    cards = [
        StoryCard(
            id=f"story:{i}",
            topic=f"Служба {i}",
            importance="medium",
            summary=f"Проблема {i}",
            tags=[],
            rubric_id="",
            category="utilities",
            story_kind="operational_status",
            representative_source_refs=[f"ref-{i}"],
            hard_facts=[
                StoryElement(
                    text=f"Проблема {i}",
                    source_refs=[f"ref-{i}"],
                    status="established",
                )
            ],
        )
        for i in range(8)
    ]

    plan = build_digest_presentation_plan(
        cards=cards,
        city_situation=rollup,
        evidence={},
        max_city_situation_items=7,
        max_city_situation_details=2,
    )

    assert len(plan.city_situation.groups) == 7
    assert len(plan.detail_story_ids) == 1
    assert "story:7" in plan.detail_story_ids


def test_build_digest_presentation_plan_suppresses_covered_pure_operational_card() -> None:
    import datetime as dt

    from src.editorial_models import StoryCard, StoryElement
    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import build_digest_presentation_plan

    now = dt.datetime.now(dt.timezone.utc)
    items = (
        CitySituationItem(
            subject_key="power",
            subject_label="Свет",
            dimension="availability",
            location="Город",
            entity="РЭС",
            state="UNAVAILABLE",
            detail="Отключение света",
            source_refs=("ref-power-1",),
            first_observed_at=now,
            last_observed_at=now,
            observation_count=1,
        ),
    )
    rollup = CitySituationRollup(items=items)
    card = StoryCard(
        id="story:10",
        topic="Отключение света",
        importance="medium",
        summary="Отключение света",
        tags=[],
        rubric_id="",
        category="utilities",
        story_kind="operational_status",
        representative_source_refs=["ref-power-1"],
        hard_facts=[
            StoryElement(
                text="Отключение света",
                source_refs=["ref-power-1"],
                status="established",
            )
        ],
    )

    plan = build_digest_presentation_plan(
        cards=[card],
        city_situation=rollup,
        evidence={},
        max_city_situation_items=7,
        max_city_situation_details=2,
    )

    assert len(plan.city_situation.groups) == 1
    assert "story:10" not in plan.detail_story_ids


def test_build_digest_presentation_plan_preserves_hybrid_story() -> None:
    import datetime as dt

    from src.editorial_models import StoryCard, StoryElement
    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import build_digest_presentation_plan

    now = dt.datetime.now(dt.timezone.utc)
    items = (
        CitySituationItem(
            subject_key="road_works",
            subject_label="Дорожные работы",
            dimension="availability",
            location="Центр",
            entity="ДРСУ",
            state="RESTRICTED",
            detail="Перекрытие проспекта",
            source_refs=("ref-road-1",),
            first_observed_at=now,
            last_observed_at=now,
            observation_count=1,
        ),
    )
    rollup = CitySituationRollup(items=items)
    hybrid_card = StoryCard(
        id="story:20",
        topic="Капитальный ремонт проспекта",
        importance="high",
        summary="Ремонт продлится до осени, выделено финансирование",
        tags=["ремонт", "город"],
        rubric_id="",
        category="society",
        story_kind="",  # Not operational_status
        representative_source_refs=["ref-road-1", "ref-road-2"],
        hard_facts=[
            StoryElement(
                text="Выделено финансирование",
                source_refs=["ref-road-2"],
                status="established",
            )
        ],
    )

    plan = build_digest_presentation_plan(
        cards=[hybrid_card],
        city_situation=rollup,
        evidence={},
        max_city_situation_items=7,
        max_city_situation_details=2,
    )

    assert "story:20" in plan.detail_story_ids


def test_score_digest_detail_evidence_prefers_concrete_facts() -> None:
    import datetime as dt

    from src.publication.digest_presentation import score_digest_detail_evidence
    from src.publication.evidence import PublicationEvidence

    now = dt.datetime.now(dt.timezone.utc)
    generic_evi = PublicationEvidence(
        evidence_id="evi:1",
        story_id=1,
        text="Горожане адаптируются к сложной ситуации",
        source_text="Горожане адаптируются к сложной ситуации в городе",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=1,
        source_ref="ref:1",
        source_id=1,
        source_item_id=1,
        source_role="citizen",
        observed_at=now,
    )
    concrete_evi = PublicationEvidence(
        evidence_id="evi:2",
        story_id=1,
        text="Жильцы скинулись по 300 рублей на генератор",
        source_text="Жильцы скинулись по 300 рублей на домовой генератор для насоса",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=2,
        source_ref="ref:2",
        source_id=1,
        source_item_id=1,
        source_role="citizen",
        observed_at=now,
    )
    assert score_digest_detail_evidence(concrete_evi) > score_digest_detail_evidence(generic_evi)


def test_build_digest_presentation_plan_selects_microdetail_hints() -> None:
    import datetime as dt

    from src.editorial_models import StoryCard
    from src.publication.digest_presentation import build_digest_presentation_plan
    from src.publication.evidence import PublicationEvidence

    now = dt.datetime.now(dt.timezone.utc)
    evi_generic = PublicationEvidence(
        evidence_id="story:1:evi:1",
        story_id=1,
        text="Жители адаптируются",
        source_text="Жители адаптируются",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=1,
        source_ref="ref:1",
        source_id=1,
        source_item_id=1,
        source_role="citizen",
        observed_at=now,
    )
    evi_concrete = PublicationEvidence(
        evidence_id="story:1:evi:2",
        story_id=1,
        text="Жильцы скинулись по 300 рублей на генератор",
        source_text="Жильцы скинулись по 300 рублей на генератор",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=2,
        source_ref="ref:2",
        source_id=1,
        source_item_id=1,
        source_role="citizen",
        observed_at=now,
    )
    card = StoryCard(
        id="story:1",
        topic="Генераторы",
        importance="medium",
        summary="Генераторы",
        tags=["генераторы"],
        rubric_id="society",
        category="society",
        story_kind="",
        representative_source_refs=["ref:1", "ref:2"],
    )
    plan = build_digest_presentation_plan(
        cards=[card],
        city_situation=None,
        evidence={"story:1:evi:1": evi_generic, "story:1:evi:2": evi_concrete},
        max_city_situation_items=7,
        max_city_situation_details=2,
    )
    assert len(plan.story_hints) == 1
    hint = plan.story_hints[0]
    assert hint.story_id == "story:1"
    assert "story:1:evi:2" in hint.detail_support_ids


def test_build_digest_presentation_plan_assigns_merge_groups_by_tags() -> None:
    from src.editorial_models import StoryCard
    from src.publication.digest_presentation import build_digest_presentation_plan

    # Disjoint stories in same rubric
    card_a = StoryCard(
        id="story:100",
        topic="Администрация",
        importance="low",
        summary="Назначение",
        tags=["городская администрация", "назначение"],
        rubric_id="society",
        category="society",
        story_kind="",
    )
    card_b = StoryCard(
        id="story:101",
        topic="Лаборатория",
        importance="low",
        summary="Анализы",
        tags=["лаборатория", "медицина"],
        rubric_id="society",
        category="society",
        story_kind="",
    )
    # Related stories in same rubric
    card_c = StoryCard(
        id="story:102",
        topic="Сети электроснабжения",
        importance="low",
        summary="Ремонт ЛЭП",
        tags=["электроснабжение", "лэп"],
        rubric_id="utilities",
        category="utilities",
        story_kind="",
    )
    card_d = StoryCard(
        id="story:103",
        topic="Подстанция",
        importance="low",
        summary="Трансформатор",
        tags=["электроснабжение", "подстанция"],
        rubric_id="utilities",
        category="utilities",
        story_kind="",
    )

    plan = build_digest_presentation_plan(
        cards=[card_a, card_b, card_c, card_d],
        city_situation=None,
        evidence={},
        max_city_situation_items=7,
        max_city_situation_details=2,
    )
    hints_by_id = {h.story_id: h for h in plan.story_hints}
    assert hints_by_id["story:100"].merge_group_id != hints_by_id["story:101"].merge_group_id
    assert hints_by_id["story:102"].merge_group_id == hints_by_id["story:103"].merge_group_id
