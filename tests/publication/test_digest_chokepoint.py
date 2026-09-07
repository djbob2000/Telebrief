import pytest

from src.editorial_models import StoryCard
from src.publication.digest_chokepoint import filter_digest_candidate_cards

pytestmark = pytest.mark.unit


def test_filter_drops_commercial_transport_ads():
    ad_card = StoryCard(
        id="story_1",
        topic="Рейсы в Грузию и Польшу",
        summary="Пассажирские перевозки через Мелитополь и Бердянск в Грузию, цена 450$. Бронирование по телефону.",
        importance="medium",
        rubric_id="transport",
    )
    real_card = StoryCard(
        id="story_2",
        topic="Маршрут №4",
        summary="На маршрут №4 в Бердянске вышел новый автобус, стоимость проезда со скидкой 17 рублей.",
        importance="high",
        rubric_id="transport",
    )
    filtered = filter_digest_candidate_cards([ad_card, real_card])
    assert len(filtered) == 1
    assert filtered[0].id == "story_2"


def test_filter_drops_service_ads_and_phone_spam():
    spam_card = StoryCard(
        id="story_spam",
        topic="Помощь с верификацией и пенсиями",
        summary="Разблокировка банковских карт, оформление ЕЦП. Обращаться в telegram: @endofmee_13 или +79900236421.",
        importance="medium",
        rubric_id="civic_services",
    )
    filtered = filter_digest_candidate_cards([spam_card])
    assert len(filtered) == 0


def test_filter_drops_clinic_price_lists():
    clinic_card = StoryCard(
        id="story_clinic",
        topic="Услуги клиники",
        summary="Список доступных услуг: УЗИ, массаж, ЭКГ, ЛОР, кардиолог. Прием с 8:00 до 16:00.",
        importance="medium",
        rubric_id="health",
    )
    filtered = filter_digest_candidate_cards([clinic_card])
    assert len(filtered) == 0


def test_filter_drops_chat_chatter_without_facts():
    chatter_card = StoryCard(
        id="story_chatter",
        topic="Живые люди на АКЗ",
        summary="На АКЗ все живые.",
        importance="low",
        rubric_id="other",
    )
    filtered = filter_digest_candidate_cards([chatter_card])
    assert len(filtered) == 0


def test_filter_drops_dining_hall_chili():
    canteen_card = StoryCard(
        id="story_chili",
        topic="Столовая Чили",
        summary="Столовая «Чили» открыта, режим работы и забор анализов. Телефон +79902856702.",
        importance="low",
        rubric_id="other",
    )
    filtered = filter_digest_candidate_cards([canteen_card])
    assert len(filtered) == 0


def test_filter_balances_topics_and_prevents_single_topic_monopoly():
    # 15 high-urgency gas cards
    gas_cards = [
        StoryCard(
            id=f"gas_{i}",
            topic=f"Запах газа в районе {i}",
            summary=f"Жители района {i} жалуются на сильный запах газа на улице и в домах.",
            importance="high",
            rubric_id="utilities",
        )
        for i in range(15)
    ]
    # 3 medium-urgency electricity cards
    electricity_cards = [
        StoryCard(
            id=f"power_{i}",
            topic=f"Отключение света {i}",
            summary=f"В Бердянске больше месяца нет электричества на улице {i}, жители питаются от генераторов.",
            importance="medium",
            rubric_id="utilities",
        )
        for i in range(3)
    ]
    # 2 medium-urgency water cards
    water_cards = [
        StoryCard(
            id=f"water_{i}",
            topic=f"Подача воды {i}",
            summary=f"Водоснабжение на Шаумяна {i} работает по часам, вечером давления нет.",
            importance="medium",
            rubric_id="utilities",
        )
        for i in range(2)
    ]
    # 2 high-urgency security cards
    security_cards = [
        StoryCard(
            id=f"security_{i}",
            topic=f"Взрывы в городе {i}",
            summary=f"Жители сообщают о серии громких взрывов и работе ПВО в небе {i}.",
            importance="high",
            rubric_id="security",
        )
        for i in range(2)
    ]

    all_cards = gas_cards + electricity_cards + water_cards + security_cards
    filtered = filter_digest_candidate_cards(all_cards, max_cards=12, max_per_topic=3)

    # Total must not exceed max_cards
    assert len(filtered) <= 12

    # Gas must be capped so it does not crowd out other critical topics
    gas_results = [c for c in filtered if "газ" in (c.topic + c.summary).lower()]
    assert len(gas_results) <= 3

    # Electricity MUST be present despite being "medium" importance
    power_results = [
        c
        for c in filtered
        if "электричеств" in (c.topic + c.summary).lower()
        or "свет" in (c.topic + c.summary).lower()
    ]
    assert len(power_results) >= 2

    # Water MUST be present
    water_results = [c for c in filtered if "вод" in (c.topic + c.summary).lower()]
    assert len(water_results) >= 1

    # Security MUST be present
    sec_results = [c for c in filtered if "взрыв" in (c.topic + c.summary).lower()]
    assert len(sec_results) >= 2


def test_filter_drops_commercial_intercity_bus_and_booking_sites():
    bus_card = StoryCard(
        id="story_bus",
        topic="Бердянск — Ростов: 4 рейса в день",
        summary="По маршруту ежедневно выполняется 4 автобусных рейса, перевозчик забирает пассажиров по городским остановкам. Запись ведётся через ресурс BUSKING.PRO.",
        importance="medium",
        rubric_id="transport",
    )
    filtered = filter_digest_candidate_cards([bus_card])
    assert len(filtered) == 0


def test_filter_drops_grey_market_banking_services():
    bank_card = StoryCard(
        id="story_bank",
        topic="Восстановление доступа к онлайн-банкингу",
        summary="В Бердянске доступны услуги по восстановлению доступа к личным кабинетам ПриватБанка, А-Банка, Sense Bank.",
        importance="medium",
        rubric_id="civic_services",
    )
    filtered = filter_digest_candidate_cards([bank_card])
    assert len(filtered) == 0


def test_filter_drops_sports_section_recruitments():
    football_card = StoryCard(
        id="story_football",
        topic="Набор детей на футбол в спортшколу им. Назарова",
        summary="Бесплатные тренировки для мальчиков 2017–2018 г.р. и девочек 2016–2017 г.р. Ведется набор в секцию.",
        importance="medium",
        rubric_id="other",
    )
    filtered = filter_digest_candidate_cards([football_card])
    assert len(filtered) == 0
