import json
from pathlib import Path


def test_digest_presentation_plan_to_audit_dict() -> None:
    from src.publication.digest_presentation import (
        CitySituationPresentationGroup,
        CitySituationPresentationPlan,
        DigestPresentationPlan,
        DigestStoryPresentation,
    )

    plan = DigestPresentationPlan(
        city_situation=CitySituationPresentationPlan(
            groups=(
                CitySituationPresentationGroup(
                    group_id="situation:power:availability",
                    group_kind="subject_status",
                    subject_key="power",
                    subject_label="Свет",
                    state="UNAVAILABLE",
                    source_refs=("ref-1",),
                    detail_lines=("Света нет",),
                    covered_story_ids=("story:1",),
                    cited_support_ids=("story:1:evidence:0:frag:11",),
                ),
            ),
            covered_source_refs=("ref-1",),
        ),
        story_presentations=(
            DigestStoryPresentation(
                story_id="story:1",
                mode="DASHBOARD_AND_DRILLDOWN",
                city_situation_group_ids=("situation:power:availability",),
                detail_support_ids=("story:1:evidence:1:frag:12",),
                merge_group_id="story:1",
            ),
        ),
    )

    assert plan.to_audit_dict() == {
        "story_ids": ["story:1"],
        "stories": [
            {
                "story_id": "story:1",
                "mode": "DASHBOARD_AND_DRILLDOWN",
                "city_situation_group_ids": ["situation:power:availability"],
                "detail_support_ids": ["story:1:evidence:1:frag:12"],
                "merge_group_id": "story:1",
            }
        ],
        "city_situation_groups": [
            {
                "group_id": "situation:power:availability",
                "covered_story_ids": ["story:1"],
                "cited_support_ids": ["story:1:evidence:0:frag:11"],
            }
        ],
    }


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
                subject_key="water",
                subject_label="Водоснабжение",
                dimension="availability",
                location="Гора",
                entity="горводоканал",
                state="UNAVAILABLE",
                detail="Нет воды",
                source_refs=("ref-water-red",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
            ),
            CitySituationItem(
                subject_key="water",
                subject_label="Водоснабжение",
                dimension="availability",
                location="Залив",
                entity="горводоканал",
                state="AVAILABLE",
                detail="Вода подается",
                source_refs=("ref-water-green",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
            ),
        )
    )

    plan = plan_city_situation_presentation(rollup, max_items=7, max_details_per_item=2)

    assert len(plan.groups) == 1
    group = plan.groups[0]
    assert group.subject_key == "water"
    assert group.state == "CONFLICTING"
    assert set(group.source_refs) == {"ref-water-red", "ref-water-green"}
    assert any("Гора" in line for line in group.detail_lines)
    assert any("Залив" in line for line in group.detail_lines)


def test_city_situation_consolidates_same_service_across_dimensions() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)
    rollup = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="water_supply",
                subject_label="Water supply",
                dimension="availability",
                location="Center",
                entity="utility",
                state="UNAVAILABLE",
                detail="No water in the center",
                source_refs=("ref-water-center",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
                current_source_refs=("ref-water-center",),
            ),
            CitySituationItem(
                subject_key="water_supply",
                subject_label="Water supply",
                dimension="pressure",
                location="Pushkina street",
                entity="utility",
                state="DEGRADED",
                detail="Weak pressure on upper floors",
                source_refs=("ref-water-pressure",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
                current_source_refs=("ref-water-pressure",),
            ),
            CitySituationItem(
                subject_key="electricity",
                subject_label="Electricity",
                dimension="availability",
                location="Azmol",
                entity="grid",
                state="UNAVAILABLE",
                detail="No power",
                source_refs=("ref-power",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
                current_source_refs=("ref-power",),
            ),
        )
    )

    plan = plan_city_situation_presentation(rollup, max_items=7, max_details_per_item=4)

    assert len(plan.groups) == 2
    water = next(group for group in plan.groups if group.subject_key == "water")
    assert set(water.source_refs) == {"ref-water-center", "ref-water-pressure"}
    assert len(water.detail_lines) == 2


def test_city_situation_consolidates_cross_dimension_mixed_states_into_conflicting() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)
    rollup = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="water_supply",
                subject_label="Water supply",
                dimension="availability",
                location="Center",
                entity="utility",
                state="AVAILABLE",
                detail="Water restored in center",
                source_refs=("ref-water-ok",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
                current_source_refs=("ref-water-ok",),
            ),
            CitySituationItem(
                subject_key="water_supply",
                subject_label="Water supply",
                dimension="pressure",
                location="Gora",
                entity="utility",
                state="UNAVAILABLE",
                detail="No water on Gora",
                source_refs=("ref-water-bad",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
                current_source_refs=("ref-water-bad",),
            ),
        )
    )

    plan = plan_city_situation_presentation(rollup, max_items=7, max_details_per_item=2)

    assert len(plan.groups) == 1
    water = plan.groups[0]
    assert water.subject_key == "water"
    assert water.state == "CONFLICTING"
    assert set(water.source_refs) == {"ref-water-ok", "ref-water-bad"}
    assert len(water.detail_lines) == 2


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
    assert grp.subject_key == "water"
    assert grp.state == "UNAVAILABLE"
    assert set(grp.source_refs) == {"ref-water-1", "ref-water-2"}
    assert len(grp.detail_lines) == 2
    assert set(plan.covered_source_refs) == {"ref-water-1", "ref-water-2"}


def test_plan_city_situation_presentation_caps_at_max_items_and_tracks_covered_refs() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime.now(dt.timezone.utc)
    valid_subjects = ["water", "electricity", "gas", "heating", "connectivity", "urban_transport"]
    items = tuple(
        CitySituationItem(
            subject_key=subj,
            subject_label=subj.replace("_", " ").title(),
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
        for i, subj in enumerate(valid_subjects)
    )
    rollup = CitySituationRollup(items=items)
    plan = plan_city_situation_presentation(rollup, max_items=4, max_details_per_item=2)
    assert len(plan.groups) == 4
    # Only the 4 selected items' source refs are in covered_source_refs
    assert len(plan.covered_source_refs) == 4
    for i in range(4):
        assert f"ref-{i}" in plan.covered_source_refs
    assert "ref-4" not in plan.covered_source_refs
    assert "ref-5" not in plan.covered_source_refs


def test_positive_subjects_are_not_collapsed_into_global_bundle() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime.now(dt.timezone.utc)
    items = (
        CitySituationItem(
            subject_key="electricity",
            subject_label="Электроснабжение",
            dimension="availability",
            location="Центр",
            entity="РЭС",
            state="AVAILABLE",
            detail="Свет есть",
            source_refs=("ref-elec",),
            first_observed_at=now,
            last_observed_at=now,
            observation_count=1,
        ),
        CitySituationItem(
            subject_key="urban_transport",
            subject_label="Городской транспорт",
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
    assert {g.subject_key for g in plan.groups} == {"electricity", "urban_transport"}
    assert all(g.group_kind == "subject_status" for g in plan.groups)


def test_mixed_day_reserves_at_least_one_positive_subject() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime.now(dt.timezone.utc)
    non_pos_subjects = ["water", "electricity", "gas", "heating"]
    pos_subjects = ["connectivity", "urban_transport"]
    items = [
        CitySituationItem(
            subject_key=subj,
            subject_label=subj.title(),
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
        for i, subj in enumerate(non_pos_subjects)
    ]
    items.extend(
        [
            CitySituationItem(
                subject_key=subj,
                subject_label=subj.title(),
                dimension="availability",
                location="Центр",
                entity=f"pos_entity_{i}",
                state="AVAILABLE" if i == 0 else "RESOLVED",
                detail=f"Работает штатно {i}",
                source_refs=(f"ref-avail-{i}",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
            )
            for i, subj in enumerate(pos_subjects)
        ]
    )
    rollup = CitySituationRollup(items=tuple(items))
    plan = plan_city_situation_presentation(
        rollup, max_items=4, max_details_per_item=2, max_positive_items=2
    )
    assert len(plan.groups) == 4
    assert all(g.subject_key != "available_services" for g in plan.groups)
    pos_groups = [g for g in plan.groups if g.state in ("AVAILABLE", "RESOLVED")]
    non_pos_groups = [g for g in plan.groups if g.state not in ("AVAILABLE", "RESOLVED")]
    assert len(pos_groups) == 1
    assert len(non_pos_groups) == 3


def test_positive_subject_budget_is_two_by_default() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import plan_city_situation_presentation

    now = dt.datetime.now(dt.timezone.utc)
    pos_subjects = ["water", "electricity", "gas", "heating"]
    items = [
        CitySituationItem(
            subject_key=subj,
            subject_label=subj.title(),
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
        for i, subj in enumerate(pos_subjects)
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
    pos_subjects = ["water", "electricity", "gas", "heating"]
    items = [
        CitySituationItem(
            subject_key=subj,
            subject_label=subj.title(),
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
        for i, subj in enumerate(pos_subjects)
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
    valid_subjects = ["water", "electricity", "gas", "heating", "connectivity", "urban_transport"]
    items = tuple(
        CitySituationItem(
            subject_key=subj,
            subject_label=subj.title(),
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
        for i, subj in enumerate(valid_subjects)
    )
    rollup = CitySituationRollup(items=items)

    cards = [
        StoryCard(
            id=f"story:{i}",
            topic=f"Служба {subj}",
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
        for i, subj in enumerate(valid_subjects)
    ]

    plan = build_digest_presentation_plan(
        cards=cards,
        city_situation=rollup,
        evidence={},
        max_city_situation_items=4,
        max_city_situation_details=2,
    )

    assert len(plan.city_situation.groups) == 4
    assert len(plan.detail_story_ids) == 2
    assert "story:4" in plan.detail_story_ids
    assert "story:5" in plan.detail_story_ids


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
        tags=["таврия-энерго", "лэп"],
        rubric_id="utilities",
        category="utilities",
        story_kind="",
    )
    card_d = StoryCard(
        id="story:103",
        topic="Подстанция",
        importance="low",
        summary="Трансформатор",
        tags=["таврия-энерго", "подстанция"],
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

    # 3 positive subjects: urban_transport, connectivity, gas
    items = (
        CitySituationItem(
            subject_key="urban_transport",
            subject_label="Городской транспорт",
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
        CitySituationItem(
            subject_key="connectivity",
            subject_label="Связь",
            dimension="availability",
            location="Город",
            entity="Оператор",
            state="AVAILABLE",
            detail="Связь работает",
            source_refs=("ref-conn",),
            first_observed_at=now,
            last_observed_at=now - dt.timedelta(minutes=1),
            observation_count=1,
        ),
        CitySituationItem(
            subject_key="gas",
            subject_label="Газоснабжение",
            dimension="availability",
            location="Город",
            entity="Горгаз",
            state="AVAILABLE",
            detail="Газ подается",
            source_refs=("ref-gas",),
            first_observed_at=now,
            last_observed_at=now - dt.timedelta(minutes=2),
            observation_count=1,
        ),
    )
    rollup = CitySituationRollup(items=items)

    # Card for transport with extra microdetail (terminal battery workaround)
    card_transport = StoryCard(
        id="story:transport",
        topic="Работа городского транспорта",
        importance="medium",
        summary="Автобусы ходят, терминалы запитаны от 9V батареек",
        tags=["транспорт"],
        rubric_id="transport",
        category="transport",
        story_kind="operational_status",
        representative_source_refs=["ref-transport", "ref-transport-battery"],
        hard_facts=[
            StoryElement(
                text="Автобусы ходят", source_refs=["ref-transport"], status="established"
            ),
            StoryElement(
                text="Терминалы оплаты запитаны от 9V батареек",
                source_refs=["ref-transport-battery"],
                status="established",
            ),
        ],
    )
    # Card for gas (omitted from dashboard due to max_positive_items=2)
    card_gas = StoryCard(
        id="story:gas",
        topic="Подача газа",
        importance="low",
        summary="Газовая сеть работает в штатном режиме",
        tags=["газ"],
        rubric_id="utilities",
        category="utilities",
        story_kind="operational_status",
        representative_source_refs=["ref-gas"],
        hard_facts=[
            StoryElement(
                text="Газовая сеть работает в штатном режиме",
                source_refs=["ref-gas"],
                status="established",
            )
        ],
    )

    evi_transport_status = PublicationEvidence(
        evidence_id="story:transport:evi:status",
        story_id=1,
        text="Автобусы ходят",
        source_text="Автобусы ходят",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="ref-transport",
        source_id=1,
        source_item_id=1,
        source_role="community",
        observed_at=now,
    )
    evi_transport_detail = PublicationEvidence(
        evidence_id="story:transport:evi:detail",
        story_id=1,
        text="Терминалы оплаты запитаны от 9V батареек",
        source_text="Терминалы оплаты запитаны от 9V батареек",
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=102,
        source_ref="ref-transport-battery",
        source_id=1,
        source_item_id=1,
        source_role="community",
        observed_at=now,
    )
    evi_gas_status = PublicationEvidence(
        evidence_id="story:gas:evi:status",
        story_id=2,
        text="Газовая сеть работает в штатном режиме",
        source_text="Газовая сеть работает в штатном режиме",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=103,
        source_ref="ref-gas",
        source_id=2,
        source_item_id=2,
        source_role="official",
        observed_at=now,
    )

    evidence_dict = {
        "story:transport:evi:status": evi_transport_status,
        "story:transport:evi:detail": evi_transport_detail,
        "story:gas:evi:status": evi_gas_status,
    }

    plan = build_digest_presentation_plan(
        cards=[card_transport, card_gas],
        city_situation=rollup,
        evidence=evidence_dict,
        max_city_situation_items=7,
        max_city_situation_details=2,
        max_city_situation_positive_items=2,
    )

    hints_by_id = {h.story_id: h for h in plan.story_hints}

    # Selected positive story with microdetail becomes DRILL_DOWN
    assert hints_by_id["story:transport"].detail_role == "DRILL_DOWN"
    assert "story:transport:evi:detail" in hints_by_id["story:transport"].detail_support_ids
    assert "story:transport" in plan.detail_story_ids

    # Omitted positive story from dashboard budget remains in thematic layer as NORMAL
    assert hints_by_id["story:gas"].detail_role == "NORMAL"
    assert "story:gas" in plan.detail_story_ids


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
        ("electricity", "Электроснабжение", "ref-elec", 1),
        ("urban_transport", "Городской транспорт", "ref-transport", 2),
        ("connectivity", "Связь", "ref-conn", 3),
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


def _build_power_novelty_plan(non_dashboard_text: str):
    import datetime as dt

    from src.editorial_models import StoryCard
    from src.publication.city_situation import CitySituationItem, CitySituationRollup
    from src.publication.digest_presentation import build_digest_presentation_plan
    from src.publication.evidence import PublicationEvidence

    now = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)
    rollup = CitySituationRollup(
        items=(
            CitySituationItem(
                subject_key="electricity",
                subject_label="Electricity",
                dimension="availability",
                location="Azmol",
                entity="grid",
                state="UNAVAILABLE",
                detail="No power in Azmol",
                source_refs=("ref-status",),
                first_observed_at=now,
                last_observed_at=now,
                observation_count=1,
                current_source_refs=("ref-status",),
            ),
        )
    )
    card = StoryCard(
        id="story:1",
        topic="Power outage",
        importance="high",
        summary="Power is unavailable in Azmol",
        tags=["electricity"],
        rubric_id="utilities",
        category="utilities",
        story_kind="operational_status",
        representative_source_refs=["ref-status", "ref-detail"],
    )
    status = PublicationEvidence(
        evidence_id="story:1:evi:status",
        story_id=1,
        text="No power in Azmol",
        source_text="No power in Azmol",
        kind="service_access",
        publication_use="PUBLISH",
        fragment_id=1,
        source_ref="ref-status",
        source_id=1,
        source_item_id=1,
        source_role="community",
        observed_at=now,
    )
    detail = PublicationEvidence(
        evidence_id="story:1:evi:detail",
        story_id=1,
        text=non_dashboard_text,
        source_text=non_dashboard_text,
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=2,
        source_ref="ref-detail",
        source_id=1,
        source_item_id=2,
        source_role="community",
        observed_at=now,
    )
    return build_digest_presentation_plan(
        cards=[card],
        city_situation=rollup,
        evidence={status.evidence_id: status, detail.evidence_id: detail},
    )


def test_dashboard_status_repeat_without_new_fact_is_dashboard_only() -> None:
    plan = _build_power_novelty_plan("Residents report that power is still absent in Azmol")
    pres = plan.story_presentations[0]
    assert pres.mode == "DASHBOARD_ONLY"
    assert pres.detail_support_ids == ()


def test_dashboard_story_with_new_duration_is_drilldown() -> None:
    plan = _build_power_novelty_plan("Power has been absent for 30 days")
    pres = plan.story_presentations[0]
    assert pres.mode == "DASHBOARD_AND_DRILLDOWN"
    assert pres.detail_support_ids == ("story:1:evi:detail",)


def test_dashboard_story_with_workaround_is_drilldown() -> None:
    plan = _build_power_novelty_plan("A 400 kW generator is being used for the boiler house")
    pres = plan.story_presentations[0]
    assert pres.mode == "DASHBOARD_AND_DRILLDOWN"
    assert pres.detail_support_ids == ("story:1:evi:detail",)


def test_phone_only_novelty_does_not_force_drilldown() -> None:
    plan = _build_power_novelty_plan("No power in Azmol. Call +7 999 111 22 33")
    pres = plan.story_presentations[0]
    assert pres.mode == "DASHBOARD_ONLY"
    assert pres.detail_support_ids == ()


def test_intercity_transport_excluded_from_city_situation_dashboard() -> None:
    import datetime as dt

    from src.publication.city_situation import CitySituationItem
    from src.publication.digest_presentation import _canonical_city_situation_subject

    now = dt.datetime.now(dt.timezone.utc)
    item = CitySituationItem(
        subject_key="transport",
        subject_label="Автобус Бердянск — Ростов",
        dimension="availability",
        location="",
        entity="Автовокзал",
        state="AVAILABLE",
        detail="Ежедневные межгород рейсы в Ростов",
        source_refs=("r1",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    subject = _canonical_city_situation_subject(item)
    assert subject is None


def test_workaround_plumber_tank_does_not_merge_with_operational_water_status() -> None:
    from src.editorial_models import StoryCard
    from src.publication.digest_presentation import _are_cards_merge_compatible

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
    compat = _are_cards_merge_compatible(card_status, card_workaround)
    assert compat is False


def test_homogeneous_power_status_merges_up_to_6() -> None:
    from src.editorial_models import StoryCard
    from src.publication.digest_presentation import _compute_merge_groups

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
    # The first 6 cards merge into one group; the 7th starts a new group
    mg1 = groups["story:p1"]
    for i in range(2, 7):
        assert groups[f"story:p{i}"] == mg1
    assert groups["story:p7"] != mg1


def test_digest_compression_units_power_cluster_becomes_synthesis_unit() -> None:
    from src.editorial_models import StoryCard
    from src.publication.digest_presentation import (
        DigestPresentationUnit,
        build_digest_presentation_units,
    )

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
