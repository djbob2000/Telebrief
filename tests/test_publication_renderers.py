"""Tests for PublicationDigestRenderer rubric classification, custom rubrics and Telegram formatting."""

import datetime as dt

from src.config_loader import DigestGroupConfig
from src.editorial_models import EditorialAnalysis, PreparedBundle, StoryCard, StoryElement
from src.publication.editorial_adapter import FrozenEditorialInput
from src.publication.renderers import (
    PublicationDigestRenderer,
    classify_card_rubric,
    split_into_telegram_chunks,
)


def test_classify_card_rubric():
    card_util = StoryCard(
        id="SC001",
        topic="Авария на водоводе",
        importance="high",
        summary="В Бердянске отключили воду на АКЗ",
        category="utilities",
    )
    assert classify_card_rubric(card_util) == "utilities"

    card_sec = StoryCard(
        id="SC002",
        topic="Звуки взрывов ночью",
        importance="medium",
        summary="Жители сообщили о пролете БПЛА",
    )
    assert classify_card_rubric(card_sec) == "security"

    card_trans = StoryCard(
        id="SC003",
        topic="Перекрытие улицы",
        importance="high",
        summary="Автобусы изменили маршруты движения",
    )
    assert classify_card_rubric(card_trans) == "transport"

    card_health = StoryCard(
        id="SC004",
        topic="Сдача донорской крови",
        importance="medium",
        summary="Центр крови просит сдать 3 группу",
    )
    assert classify_card_rubric(card_health) == "health"


def test_render_grouped_digest_formatting_and_custom_rubrics():
    card1 = StoryCard(
        id="SC001",
        topic="Перекрытие улицы Тверской",
        importance="high",
        summary="С 26 августа по 26 сентября перекрывается движение по улице Тверской",
        category="transport",
        hard_facts=[
            StoryElement(
                text="Автобусы № 5, 8, 13 следуют в объезд по проспекту Победы",
                source_refs=["S000001"],
            )
        ],
    )
    card2 = StoryCard(
        id="SC002",
        topic="Потребность в донорской крови",
        importance="medium",
        summary="Городской центр крови обратился с просьбой о сдаче крови",
        category="health",
        useful_details=[
            StoryElement(
                text="Прием доноров с 07:00 до 13:00 при наличии паспорта",
                source_refs=["S000002"],
            )
        ],
    )

    analysis = EditorialAnalysis(cards=[card1, card2])
    bundle = PreparedBundle(records={}, total_messages=10, candidate_count=2, prompt_text="")
    frozen_input = FrozenEditorialInput(analysis=analysis, writer_bundle=bundle)

    custom_groups = [
        DigestGroupConfig(name="Транспорт и дороги", description="Автобусы и дороги"),
        DigestGroupConfig(name="Медицина и здоровье", description="Больницы и врачи"),
    ]

    renderer = PublicationDigestRenderer(
        output_language="Russian",
        use_emojis=True,
        include_statistics=True,
        custom_rubrics=custom_groups,
    )
    title, lead, body = renderer.render_grouped_digest(
        frozen_input,
        edition_name="Бердянск",
        snapshot_at=dt.datetime(2026, 8, 27, tzinfo=dt.timezone.utc),
    )

    assert "Дайджест · 27.08.2026" in body
    assert "🚌 Транспорт и дороги" in body
    assert "Перекрытие улицы Тверской" in body
    assert "Автобусы № 5, 8, 13" in body
    assert "🩸 Медицина и здоровье" in body
    assert "Потребность в донорской крови" in body
    assert "07:00 до 13:00" in body
    assert "S000001" not in body  # no raw source refs


def test_split_into_telegram_chunks():
    # Long text exceeding chunk size
    sec1 = "*⚡️ Коммуналка*\n• Пункт 1 " + ("текст " * 50)
    sec2 = "*🚌 Транспорт*\n• Пункт 2 " + ("текст " * 50)
    full_text = f"*Дайджест*\n\n{sec1}\n\n{sec2}"

    chunks = split_into_telegram_chunks(full_text, max_chars=350)
    assert len(chunks) >= 2
    assert "Часть 1/" in chunks[0]
    assert "Часть 2/" in chunks[1]
