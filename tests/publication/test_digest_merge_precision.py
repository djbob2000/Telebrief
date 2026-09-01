"""Tests for digest merge precision, question leakage sanitization, and Option A City Situation admission."""

import datetime as dt

from src.domain.event_payload import (
    EventPayload,
    EvidenceItemPayload,
    ServiceStatePayload,
)
from src.editorial_models import StoryCard, StoryElement
from src.processing.operational_semantics import (
    derive_operational_observations,
    sanitize_operational_detail,
)
from src.publication.city_situation import CitySituationItem, CitySituationRollup
from src.publication.digest_presentation import (
    _canonical_city_situation_subject,
    _compute_merge_groups,
    _detail_line,
    build_digest_presentation_plan,
    plan_city_situation_presentation,
)


def _make_card(
    card_id: str,
    topic: str,
    tags: list[str],
    *,
    rubric_id: str = "infrastructure",
    summary: str = "",
    story_kind: str = "",
    source_refs: list[str] | None = None,
    areas: list[str] | None = None,
) -> StoryCard:
    elements = []
    if areas or source_refs:
        elements.append(
            StoryElement(
                text=topic,
                source_refs=source_refs or [f"ref:{card_id}"],
                status="attributed",
                areas=areas or [],
            )
        )
    return StoryCard(
        id=card_id,
        topic=topic,
        importance="medium",
        summary=summary or topic,
        tags=tags,
        rubric_id=rubric_id,
        category=rubric_id,
        story_kind=story_kind,
        representative_source_refs=source_refs or [f"ref:{card_id}"],
        hard_facts=elements,
    )


# ---------------------------------------------------------------------------
# 1. QUESTION LEAKAGE TESTS
# ---------------------------------------------------------------------------


def test_sanitize_operational_detail_removes_question_tail() -> None:
    text = "Жительница Бердянска сообщает, что сидит без воды и спрашивает, есть ли вода у других."
    cleaned = sanitize_operational_detail(text)
    assert "спрашивает" not in cleaned
    assert "есть ли вода" not in cleaned
    assert "сидит без воды" in cleaned
    assert cleaned.endswith(".")


def test_sanitize_operational_detail_drops_pure_questions() -> None:
    assert sanitize_operational_detail("Вода есть? (интересуется житель)") == ""
    assert sanitize_operational_detail("Житель спрашивает: что по свету слышно?") == ""
    assert sanitize_operational_detail("А как у вас со светом?") == ""


def test_derive_operational_observations_cleans_detail_preserving_item_text() -> None:
    raw_text = (
        "Жительница Бердянска сообщает, что сидит без воды и спрашивает, есть ли вода у других."
    )
    evidence_item = EvidenceItemPayload(
        text=raw_text,
        kind="service_access",
        publication_use="PUBLISH",
        source_fragment_ids=(1,),
        service_state=ServiceStatePayload(
            subject_key="water_supply",
            subject_label="Водоснабжение",
            dimension="availability",
            state="UNAVAILABLE",
            location="Бердянск",
            entity="",
            basis="direct_failure",
            expected_now=True,
        ),
    )
    payload = EventPayload(
        topic="Тест",
        headline="Тест",
        category="utilities",
        urgency="medium",
        evidence_items=(evidence_item,),
    )

    observations = derive_operational_observations(payload)
    assert len(observations) == 1
    obs = observations[0]
    # Projected detail must be sanitized
    assert "спрашивает" not in obs.detail
    assert "сидит без воды" in obs.detail
    # Raw item text in evidence payload must remain untouched
    assert evidence_item.text == raw_text


def test_detail_line_defensively_cleans_persisted_question_leakage() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    item_with_leak = CitySituationItem(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Бердянск (адрес не указан)",
        entity="",
        state="UNAVAILABLE",
        detail="Жительница Бердянска сообщает, что сидит без воды и спрашивает, есть ли вода у других.",
        source_refs=("ref:1",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    rendered = _detail_line(item_with_leak)
    assert "спрашивает" not in rendered
    assert "есть ли вода у других" not in rendered
    assert "сидит без воды" in rendered


# ---------------------------------------------------------------------------
# 2. OPTION A CITY SITUATION ADMISSION TESTS
# ---------------------------------------------------------------------------


def test_option_a_canonical_city_situation_subjects() -> None:
    now = dt.datetime.now(dt.timezone.utc)

    # Allowed infrastructure roots
    water_item = CitySituationItem(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Бердянск",
        entity="",
        state="UNAVAILABLE",
        detail="Нет воды",
        source_refs=("ref:w",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    power_item = CitySituationItem(
        subject_key="electricity",
        subject_label="Электроснабжение",
        dimension="availability",
        location="Бердянск",
        entity="",
        state="UNAVAILABLE",
        detail="Нет света",
        source_refs=("ref:p",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    assert _canonical_city_situation_subject(water_item) == "water"
    assert _canonical_city_situation_subject(power_item) == "electricity"

    # Forbidden subjects for Option A
    mokrany_item = CitySituationItem(
        subject_key="border_crossing_mokrany",
        subject_label="КПП Мокраны",
        dimension="availability",
        location="Мокраны",
        entity="ПП Мокраны",
        state="RESTRICTED",
        detail="Пеший переход",
        source_refs=("ref:m",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    aid_item = CitySituationItem(
        subject_key="humanitarian_aid",
        subject_label="Гуманитарная помощь",
        dimension="availability",
        location="Бердянск",
        entity="Выплата 10800",
        state="AVAILABLE",
        detail="Помощь 10800 грн",
        source_refs=("ref:a",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    intercity_bus_item = CitySituationItem(
        subject_key="transport",
        subject_label="Междугородние перевозки",
        dimension="availability",
        location="Бердянск - Тбилиси",
        entity="Автобус",
        state="AVAILABLE",
        detail="Рейсы в Грузию",
        source_refs=("ref:b",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    assert _canonical_city_situation_subject(mokrany_item) is None
    assert _canonical_city_situation_subject(aid_item) is None
    assert _canonical_city_situation_subject(intercity_bus_item) is None


def test_option_a_plan_city_situation_excludes_non_infrastructure_losslessly() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    water_item = CitySituationItem(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Центр",
        entity="",
        state="UNAVAILABLE",
        detail="Ремонт трубы",
        source_refs=("ref:w1",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    mokrany_item = CitySituationItem(
        subject_key="border_crossing_mokrany",
        subject_label="КПП Мокраны",
        dimension="availability",
        location="Мокраны",
        entity="",
        state="RESTRICTED",
        detail="Пеший переход",
        source_refs=("ref:m1",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    aid_item = CitySituationItem(
        subject_key="humanitarian_aid",
        subject_label="Гуманитарная помощь",
        dimension="availability",
        location="Бердянск",
        entity="",
        state="AVAILABLE",
        detail="Денежная помощь",
        source_refs=("ref:a1",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    rollup = CitySituationRollup(items=(water_item, mokrany_item, aid_item))

    plan = plan_city_situation_presentation(rollup)
    # Only water must be in city situation groups
    group_subjects = [g.subject_key for g in plan.groups]
    assert group_subjects == ["water"]
    assert "border_crossing_mokrany" not in group_subjects
    assert "humanitarian_aid" not in group_subjects


def test_option_a_build_presentation_plan_preserves_non_dashboard_stories() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    water_item = CitySituationItem(
        subject_key="water_supply",
        subject_label="Водоснабжение",
        dimension="availability",
        location="Центр",
        entity="",
        state="UNAVAILABLE",
        detail="Ремонт трубы",
        source_refs=("ref:w1",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    mokrany_item = CitySituationItem(
        subject_key="border_crossing_mokrany",
        subject_label="КПП Мокраны",
        dimension="availability",
        location="Мокраны",
        entity="",
        state="RESTRICTED",
        detail="Пеший переход",
        source_refs=("ref:m1",),
        first_observed_at=now,
        last_observed_at=now,
        observation_count=1,
    )
    rollup = CitySituationRollup(items=(water_item, mokrany_item))

    card_water = _make_card("story:1", "Вода в центре", ["водоснабжение"], source_refs=["ref:w1"])
    card_mokrany = _make_card(
        "story:2", "Перевозки в Европу", ["перевозки"], source_refs=["ref:m1"]
    )

    plan = build_digest_presentation_plan(
        cards=[card_water, card_mokrany],
        city_situation=rollup,
        evidence={},
    )
    # Both stories must be in plan
    assert "story:1" in plan.story_ids
    assert "story:2" in plan.story_ids

    hints = {h.story_id: h for h in plan.story_hints}
    # Water overlaps dashboard
    assert hints["story:1"].mode == "DASHBOARD_ONLY"
    # Mokrany is NOT in dashboard, so it MUST fall back to DETAIL_ONLY
    assert hints["story:2"].mode == "DETAIL_ONLY"
    assert "situation:border_crossing_mokrany" not in hints["story:2"].city_situation_group_ids


# ---------------------------------------------------------------------------
# 3. MERGE PRECISION & COMPLETE-LINK CLUSTERING TESTS
# ---------------------------------------------------------------------------


def test_merge_forbidden_for_case_1_water_light_traffic() -> None:
    # Story 78: Water in center
    c78 = _make_card(
        "story:78",
        "В центре Бердянска дали воду",
        ["Бердянск", "водоснабжение", "центр", "коммунальные_услуги"],
        rubric_id="infrastructure",
    )
    # Story 498: Water and light by schedule
    c498 = _make_card(
        "story:498",
        "Житель Бердянска: вода и свет — по графику, свет только днём",
        [
            "Бердянск",
            "водоснабжение",
            "электроснабжение",
            "график",
            "отключения",
            "сообщество",
            "ЖКХ",
        ],
        rubric_id="infrastructure",
    )
    # Story 441: Month without power and broken traffic lights
    c441 = _make_card(
        "story:441",
        "Жители Бердянска сообщают о месячном отсутствии электричества и неработающих светофорах",
        ["Бердянск", "электроснабжение", "светофоры", "ГАИ", "благоустройство", "жалоба жителей"],
        rubric_id="infrastructure",
    )

    groups = _compute_merge_groups([c78, c498, c441])
    # Must NOT all merge into one group!
    assert groups[c78.id] != groups[c441.id], "Water and power must not merge"
    assert (
        groups[c78.id] != groups[c498.id]
    ), "Unambiguous water must not merge with multi-service card"
    assert groups[c498.id] != groups[c441.id], "Multi-service card must not merge with power"


def test_merge_forbidden_for_case_2_power_vs_water_tanks() -> None:
    # Story 646: Give us power, not generators
    c646 = _make_card(
        "story:646",
        "Бердянцы просят восстановить электроснабжение вместо генераторов",
        ["электроснабжение", "отключение", "генератор", "жалоба", "бердянск"],
        rubric_id="infrastructure",
    )
    # Story 603: Curfew, without light and water, drones
    c603 = _make_card(
        "story:603",
        "Житель Бердянска пожаловался на комендантский час и отсутствие света и воды",
        ["Бердянск", "комендантский_час", "свет", "вода", "дроны", "безопасность"],
        rubric_id="infrastructure",
    )
    # Story 573: Water storage tanks installation
    c573 = _make_card(
        "story:573",
        "В Бердянске востребованы услуги по установке накопительных баков на фоне перебоев воды",
        ["вода", "сантехника", "Бердянск", "водоснабжение", "перебои", "накопительные баки"],
        rubric_id="infrastructure",
    )

    groups = _compute_merge_groups([c646, c603, c573])
    assert groups[c646.id] != groups[c573.id], "Power complaint and water tank ad must not merge"
    assert groups[c646.id] != groups[c603.id]
    assert groups[c603.id] != groups[c573.id]


def test_merge_forbidden_for_case_3_water_delivery_vs_jupiter_ticket() -> None:
    # Story 672: Drinking water delivery
    c672 = _make_card(
        "story:672",
        "По Бердянску и району доступна доставка питьевой воды",
        ["вода", "доставка", "Бердянск", "водоснабжение"],
        rubric_id="city_services",
    )
    # Story 531: Called Jupiter, accepted ticket
    c531 = _make_card(
        "story:531",
        "Жительница Бердянска дозвонилась в «Юпитер» и приняли заявку",
        ["Юпитер", "Бердянск", "заявка", "сервис", "телефон"],
        rubric_id="city_services",
    )

    groups = _compute_merge_groups([c672, c531])
    assert (
        groups[c672.id] != groups[c531.id]
    ), "Drinking water delivery and telecom ticket must not merge"


def test_complete_link_prevents_transitive_bridging() -> None:
    # A is pure water outage
    cA = _make_card(
        "story:A", "Отключение воды на Горе", ["водоснабжение", "гора"], rubric_id="infrastructure"
    )
    # B mentions both water and power
    cB = _make_card(
        "story:B",
        "Графики воды и света",
        ["водоснабжение", "электроснабжение"],
        rubric_id="infrastructure",
    )
    # C is pure power outage
    cC = _make_card(
        "story:C",
        "Отключение света на Косе",
        ["электроснабжение", "коса"],
        rubric_id="infrastructure",
    )

    groups = _compute_merge_groups([cA, cB, cC])
    # Complete-link guarantees {A, B, C} cannot be one component
    assert len(set(groups.values())) >= 2
    assert groups[cA.id] != groups[cC.id]
