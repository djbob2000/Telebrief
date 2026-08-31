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


def test_city_life_short_read_golden_fixture_has_final_polish_cases() -> None:
    data = json.loads(
        (
            Path(__file__).parents[1] / "fixtures" / "city_life_short_read_digest_golden.json"
        ).read_text(encoding="utf-8")
    )
    ids = {case["id"] for case in data["cases"]}
    assert {
        "mixed_bank_status_is_one_dashboard_group",
        "dashboard_label_is_deterministic",
        "covered_status_with_microdetail_becomes_drill_down",
        "covered_status_without_extra_detail_is_suppressed",
        "unsupported_causal_compression_is_rejected",
        "coping_behavior_is_not_dashboard_state",
        "community_service_state_is_operational",
        "seasonal_absence_requires_current_expectation",
        "positive_dashboard_groups_are_subject_coherent",
        "question_context_does_not_become_meta_news",
        "headline_body_attribution_is_not_duplicated",
    } <= ids


def test_plan_city_situation_consolidates_mixed_availability_into_conflicting_group() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime.now(dt.timezone.utc)
    rollup = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="banking_cash",
                subject_label="Банковские услуги и наличные",
                dimension="availability",
                location="Гора",
                entity="banking",
                state="UNAVAILABLE",
                detail="Нет связи с банком",
                source_refs=("ref-bank-red",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
            ),
            CitySituationItem(
                subject_key="banking_cash",
                subject_label="Банковские услуги и наличные",
                dimension="availability",
                location="Залив",
                entity="banking",
                state="AVAILABLE",
                detail="Банкомат выдает наличные",
                source_refs=("ref-bank-green",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
            ),
        )
    )

    plan = plan_city_situation_presentation(rollup, max_items=7, max_details_per_item=2)

    assert len(plan.groups) == 1
    group = plan.groups[0]
    assert group.subject_key == "banking_cash"
    assert group.state == "CONFLICTING"
    assert set(group.source_refs) == {"ref-bank-red", "ref-bank-green"}
    assert any("Гора" in line for line in group.detail_lines)
    assert any("Залив" in line for line in group.detail_lines)


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


def test_positive_subjects_are_not_collapsed_into_global_bundle() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime.now(dt.timezone.utc)
    items = (
        CitySituationItem(
            subject_key="banking",
            subject_label="Банки",
            dimension="availability",
            location="Центр",
            entity="Банк",
            state="AVAILABLE",
            detail="Отделения открыты",
            source_refs=("ref-bank",),
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
            state="AVAILABLE",
            detail="Автобусы ходят",
            source_refs=("ref-transport",),
            first_observed_at=now,
            last_observed_at=now,
            observation_count=1,
        ),
    )
    rollup = CitySituationRollup(items=items)
    plan = plan_city_situation_presentation(rollup, max_items=7, max_positive_items=2)
    assert len(plan.groups) == 2
    assert all(g.subject_key != "available_services" for g in plan.groups)
    assert {g.subject_key for g in plan.groups} == {"banking", "transport"}
    assert all(g.group_kind == "subject_status" for g in plan.groups)


def test_mixed_day_reserves_at_least_one_positive_subject() -> None:
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
    plan = plan_city_situation_presentation(
        rollup, max_items=7, max_details_per_item=2, max_positive_items=2
    )
    assert len(plan.groups) == 7
    assert all(g.subject_key != "available_services" for g in plan.groups)
    pos_groups = [g for g in plan.groups if g.state in ("AVAILABLE", "RESOLVED")]
    non_pos_groups = [g for g in plan.groups if g.state not in ("AVAILABLE", "RESOLVED")]
    assert len(pos_groups) == 1
    assert len(non_pos_groups) == 6


def test_positive_subject_budget_is_two_by_default() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime.now(dt.timezone.utc)
    items = [
        CitySituationItem(
            subject_key=f"positive_{i}",
            subject_label=f"Служба {i}",
            dimension="availability",
            location="Город",
            entity=f"entity_{i}",
            state="AVAILABLE",
            detail=f"Работает {i}",
            source_refs=(f"ref-pos-{i}",),
            first_observed_at=now,
            last_observed_at=now - dt.timedelta(minutes=i),
            observation_count=1,
        )
        for i in range(5)
    ]
    rollup = CitySituationRollup(items=tuple(items))
    plan = plan_city_situation_presentation(rollup, max_items=7)
    assert len(plan.groups) == 2
    assert all(g.subject_key != "available_services" for g in plan.groups)


def test_omitted_positive_refs_are_not_marked_dashboard_covered() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime.now(dt.timezone.utc)
    items = [
        CitySituationItem(
            subject_key=f"positive_{i}",
            subject_label=f"Служба {i}",
            dimension="availability",
            location="Город",
            entity=f"entity_{i}",
            state="AVAILABLE",
            detail=f"Работает {i}",
            source_refs=(f"ref-pos-{i}",),
            first_observed_at=now,
            last_observed_at=now - dt.timedelta(minutes=i),
            observation_count=1,
        )
        for i in range(4)
    ]
    rollup = CitySituationRollup(items=tuple(items))
    plan = plan_city_situation_presentation(rollup, max_items=7, max_positive_items=2)
    assert len(plan.groups) == 2
    assert "ref-pos-0" in plan.covered_source_refs
    assert "ref-pos-1" in plan.covered_source_refs
    assert "ref-pos-2" not in plan.covered_source_refs
    assert "ref-pos-3" not in plan.covered_source_refs


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


def test_build_digest_presentation_plan_detail_roles() -> None:
    import datetime as dt

    from src.editorial_models import StoryCard
    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import build_digest_presentation_plan
    from src.publication.evidence import PublicationEvidence

    now = dt.datetime.now(dt.timezone.utc)
    rollup = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="water_supply",
                subject_label="Водоснабжение",
                dimension="availability",
                location="Центр",
                entity="горводоканал",
                state="UNAVAILABLE",
                detail="Нет воды",
                source_refs=("ref-water-1",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
            ),
            CitySituationItem(
                subject_key="electricity",
                subject_label="Электроснабжение",
                dimension="availability",
                location="Гора",
                entity="рэс",
                state="UNAVAILABLE",
                detail="Нет света",
                source_refs=("ref-elec-1",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
            ),
        )
    )

    # 1. Pure duplicate of water outage with no extra microdetails -> SUPPRESS
    card_water = StoryCard(
        id="story:water",
        topic="Вода",
        importance="medium",
        summary="Воды нет в центре",
        tags=["вода", "жкх"],
        rubric_id="utilities",
        category="utilities",
        story_kind="operational_status",
        representative_source_refs=["ref-water-1"],
    )
    evi_water_generic = PublicationEvidence(
        evidence_id="story:water:evi:1",
        story_id=1,
        text="В центре нет воды",
        source_text="В центре нет воды",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=1,
        source_ref="ref-water-1",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=now,
    )

    # 2. Electricity outage card that HAS distinct microdetail (resident generator workaround) -> DRILL_DOWN
    card_elec = StoryCard(
        id="story:elec",
        topic="Свет",
        importance="high",
        summary="Нет света, жильцы запустили генератор",
        tags=["свет", "генератор"],
        rubric_id="utilities",
        category="utilities",
        story_kind="operational_status",
        representative_source_refs=["ref-elec-1"],
    )
    evi_elec_status = PublicationEvidence(
        evidence_id="story:elec:evi:status",
        story_id=2,
        text="На Горе нет света",
        source_text="На Горе нет света",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=2,
        source_ref="ref-elec-1",
        source_id=2,
        source_item_id=2,
        source_role="official",
        observed_at=now,
    )
    evi_elec_workaround = PublicationEvidence(
        evidence_id="story:elec:evi:workaround",
        story_id=2,
        text="Жильцы скинулись по 300 рублей на генератор для подачи воды",
        source_text="Жильцы дома 12 скинулись по 300 рублей на домовой генератор",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=3,
        source_ref="ref-elec-2",
        source_id=2,
        source_item_id=3,
        source_role="citizen",
        observed_at=now,
    )

    # 3. Non-operational story -> NORMAL
    card_sport = StoryCard(
        id="story:sport",
        topic="Спорт",
        importance="low",
        summary="Открыта запись в секции",
        tags=["спорт", "дети"],
        rubric_id="society",
        category="society",
        story_kind="",
        representative_source_refs=["ref-sport-1"],
    )
    evi_sport = PublicationEvidence(
        evidence_id="story:sport:evi:1",
        story_id=3,
        text="Спортшкола открыла бесплатный набор детей",
        source_text="Спортивная школа открыла бесплатный набор детей на новый учебный год",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=4,
        source_ref="ref-sport-1",
        source_id=3,
        source_item_id=4,
        source_role="official",
        observed_at=now,
    )

    evidence_dict = {
        "story:water:evi:1": evi_water_generic,
        "story:elec:evi:status": evi_elec_status,
        "story:elec:evi:workaround": evi_elec_workaround,
        "story:sport:evi:1": evi_sport,
    }

    plan = build_digest_presentation_plan(
        cards=[card_water, card_elec, card_sport],
        city_situation=rollup,
        evidence=evidence_dict,
        max_city_situation_items=7,
        max_city_situation_details=2,
    )

    hints_by_id = {h.story_id: h for h in plan.story_hints}

    # Verify detail_role on all stories
    assert hints_by_id["story:water"].detail_role == "SUPPRESS"
    assert hints_by_id["story:elec"].detail_role == "DRILL_DOWN"
    assert hints_by_id["story:sport"].detail_role == "NORMAL"

    # Detail story ids only contains DRILL_DOWN and NORMAL
    assert "story:water" not in plan.detail_story_ids
    assert "story:elec" in plan.detail_story_ids
    assert "story:sport" in plan.detail_story_ids

    # DRILL_DOWN story has detail_support_ids focused on the non-dashboard / rich detail
    assert "story:elec:evi:workaround" in hints_by_id["story:elec"].detail_support_ids


def test_positive_story_drill_down_and_positive_budget_omitted_coverage() -> None:
    import datetime as dt

    from src.editorial_models import StoryCard, StoryElement
    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import build_digest_presentation_plan
    from src.publication.evidence import PublicationEvidence

    now = dt.datetime.now(dt.timezone.utc)

    # 3 positive subjects: banking, transport, documents
    items = (
        CitySituationItem(
            subject_key="banking",
            subject_label="Банки",
            dimension="availability",
            location="Центр",
            entity="Банк",
            state="AVAILABLE",
            detail="Отделения открыты",
            source_refs=("ref-bank",),
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
            state="AVAILABLE",
            detail="Автобусы ходят",
            source_refs=("ref-transport",),
            first_observed_at=now,
            last_observed_at=now - dt.timedelta(minutes=1),
            observation_count=1,
        ),
        CitySituationItem(
            subject_key="documents",
            subject_label="Паспортный стол",
            dimension="availability",
            location="Город",
            entity="МФЦ",
            state="AVAILABLE",
            detail="Выдача паспортов",
            source_refs=("ref-docs",),
            first_observed_at=now,
            last_observed_at=now - dt.timedelta(minutes=2),
            observation_count=1,
        ),
    )
    rollup = CitySituationRollup(items=items)

    # Card for banking with extra microdetail (terminal battery workaround)
    card_bank = StoryCard(
        id="story:bank",
        topic="Работа отделений банка",
        importance="medium",
        summary="Отделения открыты, терминалы запитаны от 9V батареек",
        tags=["банки"],
        rubric_id="services",
        category="services",
        story_kind="operational_status",
        representative_source_refs=["ref-bank", "ref-bank-battery"],
        hard_facts=[
            StoryElement(text="Отделения открыты", source_refs=["ref-bank"], status="established"),
            StoryElement(
                text="Терминалы оплаты запитаны от 9V батареек",
                source_refs=["ref-bank-battery"],
                status="established",
            ),
        ],
    )
    # Card for documents (omitted from dashboard due to max_positive_items=2)
    card_docs = StoryCard(
        id="story:docs",
        topic="Выдача паспортов",
        importance="low",
        summary="Паспортный стол принимает посетителей",
        tags=["документы"],
        rubric_id="services",
        category="services",
        story_kind="operational_status",
        representative_source_refs=["ref-docs"],
        hard_facts=[
            StoryElement(
                text="Паспортный стол принимает посетителей",
                source_refs=["ref-docs"],
                status="established",
            )
        ],
    )

    evi_bank_status = PublicationEvidence(
        evidence_id="story:bank:evi:status",
        story_id=1,
        text="Отделения открыты",
        source_text="Отделения открыты",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="ref-bank",
        source_id=1,
        source_item_id=1,
        source_role="community",
        observed_at=now,
    )
    evi_bank_detail = PublicationEvidence(
        evidence_id="story:bank:evi:detail",
        story_id=1,
        text="Терминалы оплаты запитаны от 9V батареек",
        source_text="Терминалы оплаты запитаны от 9V батареек",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=102,
        source_ref="ref-bank-battery",
        source_id=1,
        source_item_id=1,
        source_role="community",
        observed_at=now,
    )
    evi_docs_status = PublicationEvidence(
        evidence_id="story:docs:evi:status",
        story_id=2,
        text="Паспортный стол принимает посетителей",
        source_text="Паспортный стол принимает посетителей",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=103,
        source_ref="ref-docs",
        source_id=2,
        source_item_id=2,
        source_role="official",
        observed_at=now,
    )

    evidence_dict = {
        "story:bank:evi:status": evi_bank_status,
        "story:bank:evi:detail": evi_bank_detail,
        "story:docs:evi:status": evi_docs_status,
    }

    plan = build_digest_presentation_plan(
        cards=[card_bank, card_docs],
        city_situation=rollup,
        evidence=evidence_dict,
        max_city_situation_items=7,
        max_city_situation_details=2,
        max_city_situation_positive_items=2,
    )

    hints_by_id = {h.story_id: h for h in plan.story_hints}

    # Selected positive story with microdetail becomes DRILL_DOWN
    assert hints_by_id["story:bank"].detail_role == "DRILL_DOWN"
    assert "story:bank:evi:detail" in hints_by_id["story:bank"].detail_support_ids
    assert "story:bank" in plan.detail_story_ids

    # Omitted positive story from dashboard budget remains in thematic layer as NORMAL
    assert hints_by_id["story:docs"].detail_role == "NORMAL"
    assert "story:docs" in plan.detail_story_ids


def test_digest_presentation_requires_exact_service_access_provenance() -> None:
    import datetime as dt

    from src.editorial_models import StoryCard
    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import build_digest_presentation_plan
    from src.publication.evidence import PublicationEvidence

    now = dt.datetime.now(dt.timezone.utc)
    rollup = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="power",
                subject_label="Электросеть",
                dimension="availability",
                location="",
                entity="",
                state="UNAVAILABLE",
                detail="Нет света",
                source_refs=("ref-shared",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
                current_source_refs=("ref-shared",),
            ),
        )
    )

    card_a = StoryCard(
        id="story:1",
        topic="Отключение света",
        importance="high",
        summary="Нет света",
        representative_source_refs=["ref-shared"],
        story_kind="operational_status",
    )
    card_b = StoryCard(
        id="story:2",
        topic="Работа магазинов",
        importance="medium",
        summary="Магазины открыты",
        representative_source_refs=["ref-shared"],
        story_kind="community_report",
    )

    evi_a = PublicationEvidence(
        evidence_id="story:1:evi:1",
        story_id=1,
        text="Света нет в центре",
        source_text="Света нет в центре",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="ref-shared",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=now,
    )
    evi_b = PublicationEvidence(
        evidence_id="story:2:evi:1",
        story_id=2,
        text="Магазины работают без перебоев",
        source_text="Магазины работают без перебоев",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=102,
        source_ref="ref-shared",
        source_id=1,
        source_item_id=1,
        source_role="community",
        observed_at=now,
    )

    evidence_dict = {
        "story:1:evi:1": evi_a,
        "story:2:evi:1": evi_b,
    }

    plan = build_digest_presentation_plan(
        cards=[card_a, card_b],
        city_situation=rollup,
        evidence=evidence_dict,
    )

    by_story = {p.story_id: p for p in plan.story_presentations}
    assert by_story["story:1"].mode == "DASHBOARD_ONLY"
    assert by_story["story:2"].mode == "DETAIL_ONLY"
    assert "story:1" not in plan.detail_story_ids
    assert "story:2" in plan.detail_story_ids


def test_digest_presentation_dashboard_and_drilldown_mode() -> None:
    import datetime as dt

    from src.editorial_models import StoryCard
    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import build_digest_presentation_plan
    from src.publication.evidence import PublicationEvidence

    now = dt.datetime.now(dt.timezone.utc)
    rollup = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="water",
                subject_label="Водоснабжение",
                dimension="availability",
                location="",
                entity="",
                state="UNAVAILABLE",
                detail="Воды нет",
                source_refs=("ref-water",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
                current_source_refs=("ref-water",),
            ),
        )
    )

    card = StoryCard(
        id="story:1",
        topic="Отключение воды",
        importance="high",
        summary="Воды нет, жильцы скидываются на подвоз",
        representative_source_refs=["ref-water"],
        story_kind="operational_status",
    )

    evi_status = PublicationEvidence(
        evidence_id="story:1:evi:dash",
        story_id=1,
        text="Воды нет в районе",
        source_text="Воды нет в районе",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="ref-water",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=now,
    )
    evi_detail = PublicationEvidence(
        evidence_id="story:1:evi:extra",
        story_id=1,
        text="Жильцы дома скинулись по 300 рублей на подвоз воды",
        source_text="Жильцы дома скинулись по 300 рублей на подвоз воды",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=102,
        source_ref="ref-community",
        source_id=1,
        source_item_id=1,
        source_role="community",
        observed_at=now,
    )

    plan = build_digest_presentation_plan(
        cards=[card],
        city_situation=rollup,
        evidence={
            "story:1:evi:dash": evi_status,
            "story:1:evi:extra": evi_detail,
        },
    )

    pres = plan.story_presentations[0]
    assert pres.mode == "DASHBOARD_AND_DRILLDOWN"
    assert "story:1:evi:extra" in pres.detail_support_ids
    assert "story:1:evi:dash" not in pres.detail_support_ids
    assert "story:1" in plan.detail_story_ids


def test_digest_presentation_dashboard_only_mode() -> None:
    import datetime as dt

    from src.editorial_models import StoryCard
    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import build_digest_presentation_plan
    from src.publication.evidence import PublicationEvidence

    now = dt.datetime.now(dt.timezone.utc)
    rollup = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="power",
                subject_label="Электросеть",
                dimension="availability",
                location="",
                entity="",
                state="UNAVAILABLE",
                detail="Нет света",
                source_refs=("ref-power",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
                current_source_refs=("ref-power",),
            ),
        )
    )

    card = StoryCard(
        id="story:1",
        topic="Свет",
        importance="high",
        summary="Нет света",
        representative_source_refs=["ref-power"],
        story_kind="operational_status",
    )

    evi = PublicationEvidence(
        evidence_id="story:1:evi:dash",
        story_id=1,
        text="Света нет",
        source_text="Света нет",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="ref-power",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=now,
    )

    plan = build_digest_presentation_plan(
        cards=[card],
        city_situation=rollup,
        evidence={"story:1:evi:dash": evi},
    )

    pres = plan.story_presentations[0]
    assert pres.mode == "DASHBOARD_ONLY"
    assert pres.detail_support_ids == ()
    assert "story:1" not in plan.detail_story_ids


def test_digest_presentation_caps_preserve_all_substantive_stories_as_detail_only() -> None:
    import datetime as dt

    from src.editorial_models import StoryCard
    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import build_digest_presentation_plan
    from src.publication.evidence import PublicationEvidence

    now = dt.datetime.now(dt.timezone.utc)
    # Create 3 distinct positive subjects
    items = []
    cards = []
    evidence_dict = {}

    subjects = [
        ("bank", "Банки", "ref-bank", 1),
        ("transport", "Транспорт", "ref-transport", 2),
        ("docs", "Документы", "ref-docs", 3),
    ]

    for subj_key, subj_label, ref, sid in subjects:
        items.append(
            CitySituationItem(
                subject_key=subj_key,
                subject_label=subj_label,
                dimension="availability",
                location="",
                entity="",
                state="AVAILABLE",
                detail=f"{subj_label} работают",
                source_refs=(ref,),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
                current_source_refs=(ref,),
            )
        )
        cards.append(
            StoryCard(
                id=f"story:{sid}",
                topic=subj_label,
                importance="medium",
                summary=f"{subj_label} работают",
                story_kind="operational_status",
            )
        )
        evidence_dict[f"story:{sid}:evi:1"] = PublicationEvidence(
            evidence_id=f"story:{sid}:evi:1",
            story_id=sid,
            text=f"{subj_label} работают в штатном режиме",
            source_text=f"{subj_label} работают в штатном режиме",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=100 + sid,
            source_ref=ref,
            source_id=sid,
            source_item_id=sid,
            source_role="official",
            observed_at=now,
        )

    rollup = CitySituationRollup(items=tuple(items))

    # Cap positive items to 2
    plan = build_digest_presentation_plan(
        cards=cards,
        city_situation=rollup,
        evidence=evidence_dict,
        max_city_situation_positive_items=2,
    )

    assert len(plan.city_situation.groups) == 2
    assert len(plan.story_presentations) == 3
    assert set(plan.story_ids) == {"story:1", "story:2", "story:3"}

    by_story = {p.story_id: p for p in plan.story_presentations}
    # Two are DASHBOARD_ONLY (or DASHBOARD_AND_DRILLDOWN), and the capped third is DETAIL_ONLY
    modes = {sid: p.mode for sid, p in by_story.items()}
    assert sum(1 for m in modes.values() if m == "DETAIL_ONLY") == 1
    assert sum(1 for m in modes.values() if m == "DASHBOARD_ONLY") == 2
