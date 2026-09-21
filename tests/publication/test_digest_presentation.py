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


@pytest.mark.unit
def test_build_thematic_topic_bundles_city_life_compression() -> None:
    """Verify that multiple incident/brand cards collapse into single bundles and chatter is filtered."""
    from src.publication.digest_presentation import build_thematic_topic_bundles

    cards = [
        # ТРЦ «Экватор» strike incident (5 cards -> 1 bundle)
        StoryCard(
            id="story:eq:1",
            topic="Пожар на складе ТРЦ «Экватор»",
            importance="high",
            summary="В результате ночного удара горит склад ТРЦ «Экватор».",
            rubric_id="safety",
        ),
        StoryCard(
            id="story:eq:2",
            topic="Магазин «Семья» пострадал в ТРЦ «Экватор»",
            importance="medium",
            summary="Магазин «Семья» сообщил о повреждениях торгового зала в ТРЦ «Экватор».",
            rubric_id="safety",
        ),
        StoryCard(
            id="story:eq:3",
            topic="Супермаркет «Улей» переносит склад из ТРЦ «Экватор»",
            importance="medium",
            summary="Супермаркет «Улей» в «Экваторе» переносит складские запасы на резервную базу.",
            rubric_id="safety",
        ),
        StoryCard(
            id="story:eq:4",
            topic="Пожар в «Экваторе» локализован спасателями",
            importance="high",
            summary="Сотрудники МЧС локализовали возгорание на территории ТРЦ «Экватор».",
            rubric_id="safety",
        ),
        StoryCard(
            id="story:eq:5",
            topic="Повреждения конструкций ТРЦ «Экватор»",
            importance="medium",
            summary="Специалисты оценивают масштаб разрушений в здании ТРЦ «Экватор».",
            rubric_id="safety",
        ),
        # Ozon logistics & delivery (4 cards -> 1 bundle)
        StoryCard(
            id="story:ozon:1",
            topic="Задержка доставки Ozon",
            importance="medium",
            summary="Ozon предупредил о задержках доставки заказов на распределительный склад.",
            rubric_id="economy",
        ),
        StoryCard(
            id="story:ozon:2",
            topic="Пункт выдачи Ozon на Восточном",
            importance="low",
            summary="Пункт выдачи Ozon на Восточном микрорайоне работает в штатном режиме.",
            rubric_id="economy",
        ),
        StoryCard(
            id="story:ozon:3",
            topic="Режим работы складов Ozon",
            importance="low",
            summary="Склады Ozon переходят на усиленный график разгрузки товаров.",
            rubric_id="economy",
        ),
        # Banking & Cash (2 cards -> 1 bundle)
        StoryCard(
            id="story:bank:1",
            topic="Отделения Сбербанка работают штатно",
            importance="medium",
            summary="Все отделения Сбера открыты для обслуживания клиентов.",
            rubric_id="civic_services",
        ),
        StoryCard(
            id="story:bank:2",
            topic="Наличные в банкоматах ПСБ",
            importance="medium",
            summary="В банкоматах ПСБ на площади доступно снятие наличных денег.",
            rubric_id="civic_services",
        ),
        # Utilities: Power & Water (2 bundles)
        StoryCard(
            id="story:pwr:1",
            topic="Отключение электричества на Косе",
            importance="high",
            summary="На Бердянской косе отсутствует свет из-за повреждения линии.",
            rubric_id="utilities",
        ),
        StoryCard(
            id="story:pwr:2",
            topic="Низкое напряжение в Центре",
            importance="medium",
            summary="В центре города напряжение в сети упало до 160 вольт.",
            rubric_id="utilities",
        ),
        StoryCard(
            id="story:wtr:1",
            topic="Возобновление подачи воды на Слободке",
            importance="high",
            summary="Водоканал восстановил водоснабжение в районе Слободка.",
            rubric_id="utilities",
        ),
        # Transport (1 bundle)
        StoryCard(
            id="story:bus:1",
            topic="Изменение маршрута автобуса №4",
            importance="medium",
            summary="Автобус №4 временно следует по измененной схеме движения через порт.",
            rubric_id="mobility",
        ),
        # Chatter / pure questions (should not create independent bundles)
        StoryCard(
            id="story:chat:1",
            topic="Где купить книги в городе?",
            importance="low",
            summary="Жители спрашивают, где купить книги и канцтовары в городе.",
            rubric_id="economy",
        ),
        StoryCard(
            id="story:chat:2",
            topic="Реклама канала MAX",
            importance="low",
            summary="Вступайте в канал MAX для обсуждения городских тем.",
            rubric_id="other",
        ),
    ]

    bundles = build_thematic_topic_bundles(cards)

    # 1. ТРЦ «Экватор» all 5 cards in 1 bundle
    eq_bundles = [b for b in bundles if "story:eq:1" in b.story_ids]
    assert len(eq_bundles) == 1
    assert set(eq_bundles[0].story_ids) == {
        "story:eq:1",
        "story:eq:2",
        "story:eq:3",
        "story:eq:4",
        "story:eq:5",
    }

    # 2. Ozon all 3 cards in 1 bundle
    ozon_bundles = [b for b in bundles if "story:ozon:1" in b.story_ids]
    assert len(ozon_bundles) == 1
    assert set(ozon_bundles[0].story_ids) == {"story:ozon:1", "story:ozon:2", "story:ozon:3"}

    # 3. Banking in 1 bundle
    bank_bundles = [b for b in bundles if "story:bank:1" in b.story_ids]
    assert len(bank_bundles) == 1
    assert set(bank_bundles[0].story_ids) == {"story:bank:1", "story:bank:2"}

    # 4. Power in 1 bundle
    pwr_bundles = [b for b in bundles if "story:pwr:1" in b.story_ids]
    assert len(pwr_bundles) == 1
    assert set(pwr_bundles[0].story_ids) == {"story:pwr:1", "story:pwr:2"}

    # 5. Chatter/ads dropped
    assert "story:chat:1" not in {sid for b in bundles for sid in b.story_ids}
    assert "story:chat:2" not in {sid for b in bundles for sid in b.story_ids}

    assert eq_bundles[0].topic_label == "Инцидент в ТРЦ «Экватор»"
    assert eq_bundles[0].emoji == "💥"

    # Total bundles: exactly 6 substantive bundles instead of 16!
    assert len(bundles) == 6


def test_extract_distinctive_entities_equator_and_brands() -> None:
    """Verify that Ukrainian spelling, tenant stores, and co-occurrences map to equator."""
    from src.publication.digest_presentation import _extract_distinctive_entities

    # Ukrainian spelling
    assert "экватор" in _extract_distinctive_entities("Влучання дрона в ТРЦ «Екватор»")
    # Quoted tenant stores
    assert "экватор" in _extract_distinctive_entities("Супермаркет «Семья» переезжает")
    assert "экватор" in _extract_distinctive_entities("Переезд магазина в помещение «Улей»")
    # Store with склад/магазин
    assert "экватор" in _extract_distinctive_entities("Пожар на складе магазина Семья")
    # Co-occurrence
    assert "экватор" in _extract_distinctive_entities(
        "Ночной прилёт по супермаркету возле переезда"
    )


def test_build_thematic_topic_bundles_economy_general_consolidation() -> None:
    from src.publication.digest_presentation import build_thematic_topic_bundles

    cards = [
        StoryCard(
            id="story:16219",
            topic="Магазин «Амстор» в Бердянске закрыт с июля",
            importance="medium",
            summary="Житель сообщает, что продуктовый магазин «Амстор» в Бердянске закрыт с июля, завтра начнётся ремонт.",
            rubric_id="economy",
        ),
        StoryCard(
            id="story:16230",
            topic="Пункт выдачи заказов в Бердянске закрыт",
            importance="medium",
            summary="Жительница сообщает, что при попытке заказать товар пункт выдачи заказов в Бердянске закрыт, система предлагает ввести другой адрес.",
            rubric_id="economy",
        ),
    ]
    bundles = build_thematic_topic_bundles(cards, rubric_id="economy")
    assert len(bundles) == 1
    assert bundles[0].rubric_id == "economy"
    assert bundles[0].topic_key == "economy"
    assert set(bundles[0].story_ids) == {"story:16219", "story:16230"}


def test_build_thematic_topic_bundles_preserves_topic_when_summary_filtered():
    from src.editorial_models import StoryCard
    from src.publication.digest_presentation import build_thematic_topic_bundles

    card = StoryCard(
        id="story:461",
        topic="Пожар из-за генератора в Бердянске",
        importance="medium",
        summary="Обсуждение пожара и проблем с электричеством в Бердянске, но без конкретных деталей.",
        rubric_id="safety",
    )
    bundles = build_thematic_topic_bundles([card], rubric_id="safety")
    assert len(bundles) == 1
    assert "story:461" in bundles[0].story_ids
    assert "Пожар из-за генератора в Бердянске." in bundles[0].fact_ledger


def test_build_required_digest_facts_filters_chat_sarcasm():
    from types import SimpleNamespace

    from src.editorial_models import StoryCard
    from src.publication.digest_presentation import build_required_digest_facts

    card = StoryCard(
        id="story:16960",
        topic="Отключение электроэнергии",
        importance="high",
        summary="Житель сообщает, что электричество снова отключили.",
        rubric_id="infrastructure",
    )
    card.operational_observations = (
        SimpleNamespace(
            location="Бердянск",
            detail="Всеее, недолго музыка играла. Света ушла",
            subject_key="electricity",
            subject_label="Электроснабжение",
            source_refs=(),
            source_fragment_ids=(),
        ),
    )
    facts = build_required_digest_facts(cards=[card])
    assert len(facts) == 0
