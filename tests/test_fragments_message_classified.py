# ruff: noqa: S101
"""article-fidelity W7.4: a rental or sale ad is one message, not civic reports.

Every fragment of a message that is a classified ad drops with the message reason. A civic
message that merely contains a phone or a price is classified per fragment, as before.
"""

from __future__ import annotations

import pytest

from src.processing.fragments import is_noise_or_classified, split_into_fragments
from src.processing.noise_detection import (
    classify_message_classified,
    has_message_offer_signal,
)

RENTAL_AD = (
    "Сдается на длительный срок в городе Бердянск\n"
    "2 комнатная квартира в самом центре города напротив парка Шмидта по проспекту Труда.\n"
    "Это двор СБЕРБАНКА и Городской Администрации\n"
    "\n"
    "В этом доме свет почти всегда есть, отопление тоже есть‼️\n"
    "\n"
    "Без животных, без маленьких детей\n"
    "\n"
    "Этаж 3 ‼️\n"
    "\n"
    "Комнаты раздельные\n"
    "Балкон окнами во двор\n"
    "Отопление централизованное(независимо от света)\n"
    "\n"
    "Цена 30.000 руб+ комуслуги\n"
    "Оплата за два месяца\n"
    "Риелторские\n"
    "\n"
    "Рядом расположены: Администрация\n"
    "Центральный рынок\n"
    "\n"
    "Тел: +7990000000\n"
    "Есть другие предложения\n"
    "Присоединяйтесь к нашей группе в телеграмм: https://t.me/+example\n"
    "МАХ https://max.ru/join/example"
)

WATER_CIVIC = (
    "Водоканал: аварийная служба, звоните по телефону +7990000000, работаем круглосуточно."
)
BUS_CIVIC = "Автобус до Мелитополя 500 руб, ходит раз в час"
MULTI_PARAGRAPH_CIVIC = (
    "В Бердянске с утра нет света на улице Шевченко.\n"
    "\n"
    "Жители звонят в диспетчерскую, дозвониться удаётся с трудом.\n"
    "\n"
    "Водоканал обещает подать воду к вечеру."
)
UNSIGNALED_OFFER_VERB = "Куплю запчасти для генератора, спасибо всем, кто подскажет"


def _expected_per_fragment(text_content: str) -> tuple[bool, str | None]:
    """The pre-W7.4 verdict for one paragraph: the per-fragment detectors alone."""
    is_noise, reason = is_noise_or_classified(text_content)
    return (not is_noise, reason if is_noise else None)


def test_rental_ad_message_drops_every_fragment() -> None:
    fragments = split_into_fragments(RENTAL_AD)

    assert len(fragments) >= 3
    assert all(not f.is_candidate for f in fragments)
    assert all(f.drop_reason == "commercial_classified" for f in fragments)


def test_rental_ad_keeps_fragment_text_for_provenance() -> None:
    fragments = split_into_fragments(RENTAL_AD)

    joined = "\n".join(f.text_content for f in fragments)
    assert "В этом доме свет почти всегда есть" in joined
    assert "+7990000000" in joined
    assert "https://t.me/+example" in joined


def test_rental_ad_is_classified_as_a_whole_message() -> None:
    assert has_message_offer_signal(RENTAL_AD)
    assert classify_message_classified(RENTAL_AD) == "commercial_classified"


@pytest.mark.parametrize(
    "text",
    [
        "Здається квартира в центрі Бердянська, оплата 15000 грн, тел +380990000000",
        "Продам гараж у центрі, звоніть на номер +7990000000",
        "Аренда офиса на Морозова, 20 000 руб в месяц, риелтор на связи",
        "Сдам комнату, бронирование по ссылке https://example.org/booking/123",
    ],
)
def test_other_offer_messages_with_a_strong_signal_are_classified(text: str) -> None:
    assert classify_message_classified(text) is not None
    fragments = split_into_fragments(text)
    assert fragments
    assert all(not f.is_candidate for f in fragments)


def test_water_civic_message_with_phone_is_unchanged() -> None:
    assert classify_message_classified(WATER_CIVIC) is None
    fragments = split_into_fragments(WATER_CIVIC)
    for fragment in fragments:
        assert (fragment.is_candidate, fragment.drop_reason) == _expected_per_fragment(
            fragment.text_content
        )


def test_bus_civic_message_with_price_is_unchanged() -> None:
    assert classify_message_classified(BUS_CIVIC) is None
    fragments = split_into_fragments(BUS_CIVIC)
    for fragment in fragments:
        assert (fragment.is_candidate, fragment.drop_reason) == _expected_per_fragment(
            fragment.text_content
        )


def test_ordinary_multi_paragraph_civic_post_is_unchanged() -> None:
    assert classify_message_classified(MULTI_PARAGRAPH_CIVIC) is None
    fragments = split_into_fragments(MULTI_PARAGRAPH_CIVIC)
    assert len(fragments) == 3
    assert all(f.is_candidate for f in fragments)
    assert all(f.drop_reason is None for f in fragments)


def test_offer_verb_without_a_strong_signal_is_unchanged() -> None:
    assert classify_message_classified(UNSIGNALED_OFFER_VERB) is None
    for fragment in split_into_fragments(UNSIGNALED_OFFER_VERB):
        assert (fragment.is_candidate, fragment.drop_reason) == _expected_per_fragment(
            fragment.text_content
        )
