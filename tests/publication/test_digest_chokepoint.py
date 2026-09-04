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
