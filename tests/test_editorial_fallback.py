"""Tests for the deterministic thematic emergency editorial path."""

from datetime import datetime, timedelta, timezone

import pytest

from src.collector import Message
from src.editorial_fallback import (
    DeterministicStoryCardBuilder,
    NoSubstantiveMaterialError,
    StoryCardRenderer,
)
from src.editorial_models import PreparedBundle, SourceRecord


def _bundle(texts: list[tuple[str, str]]) -> PreparedBundle:
    records = {}
    for index, (text, source_type) in enumerate(texts, start=1):
        ref = f"S{index:06d}"
        message = Message(
            text=text,
            sender="Источник",
            timestamp=datetime(2026, 8, 14, 12, tzinfo=timezone.utc) + timedelta(minutes=index),
            link=f"https://t.me/source/{index}",
            channel_name="Source",
            has_media=False,
            media_type="",
            message_id=index,
        )
        records[ref] = SourceRecord(ref, message, source_type)
    return PreparedBundle(
        records=records,
        prompt_text="",
        total_messages=len(records),
        candidate_count=len(records),
    )


def test_fallback_groups_topics_and_keeps_news_and_community_distinct():
    cards = DeterministicStoryCardBuilder().build(
        _bundle(
            [
                ("Коммунальное предприятие сообщило: воду отключат до 15:00", "official"),
                ("На Колонии тоже нет воды", "community"),
                ("Жители услышали громкий взрыв", "community"),
                ("На Колонии нет света", "community"),
            ]
        )
    )

    topics = {card.topic for card in cards}
    assert "water" in topics
    assert "incidents" in topics
    assert "electricity" in topics
    water = next(card for card in cards if card.topic == "water")
    assert water.hard_facts
    assert water.community_observations
    assert all(ref.startswith("S") for ref in water.all_source_refs())


def test_fallback_does_not_infer_causality_between_broad_topics():
    bundle = _bundle(
        [("Жители услышали громкий взрыв", "community"), ("На Колонии нет света", "community")]
    )
    cards = DeterministicStoryCardBuilder().build(bundle)
    article = StoryCardRenderer().render(cards)

    assert "после взрыва" not in article.to_markdown().lower()
    assert len(cards) == 2


def test_fallback_skips_empty_or_noise_only_bundle():
    with pytest.raises(NoSubstantiveMaterialError):
        DeterministicStoryCardBuilder().build(PreparedBundle({}, "", 3, 0))


def test_fallback_synthesizes_connectivity_observations_instead_of_raw_messages():
    cards = DeterministicStoryCardBuilder().build(
        _bundle(
            [
                ("Что со связью?", "community"),
                ("Есть у кого связь?!", "community"),
                ("Я целый день без связи", "community"),
                ("Ни Onet, ни Юпитер не работают", "community"),
                ("Почему на АКЗ плохо работает +7телеком?", "community"),
            ]
        )
    )

    card = next(card for card in cards if card.topic == "communications")
    texts = [element.text for element in card.community_observations]
    assert len(texts) <= 2
    assert all(text not in {"Что со связью?", "Есть у кого связь?!"} for text in texts)
    assert all(";" not in text for text in texts)
    assert "Жители сообщали о перебоях" in card.summary

    article = StoryCardRenderer().render(cards).to_markdown()
    assert "АКЗ" in article
    assert "Что со связью?" not in article
    assert ";" not in article


def test_fallback_remains_compact_for_many_repeated_candidates():
    cards = DeterministicStoryCardBuilder().build(
        _bundle(
            [
                (f"На Колонии снова нет связи, сообщение {index}", "community")
                for index in range(600)
            ]
        )
    )

    article = StoryCardRenderer().render(cards).to_markdown()
    assert len(cards) == 1
    assert len(article.splitlines()) <= 8
    assert len(article) < 1200
    assert "сообщение 599" not in article


def test_renderer_never_concatenates_raw_story_elements():
    cards = DeterministicStoryCardBuilder().build(
        _bundle(
            [
                ("На Колонии нет света", "community"),
                ("На Морской тоже нет света", "community"),
                ("Курс доллара 41.20", "community"),
                (
                    "Заправка автокондиционеров, запись по телефону +7 990 123-45-67 "
                    "https://t.me/ac_service",
                    "community",
                ),
            ]
        )
    )

    rendered = StoryCardRenderer().render(cards).to_markdown()

    assert "; " not in rendered
    assert "Курс доллара" not in rendered
    assert "автокондиционеров" not in rendered
    assert "+7 990" not in rendered
