"""Tests for the deterministic thematic emergency editorial path."""

from datetime import datetime, timezone

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
            timestamp=datetime(2026, 8, 14, 12, index, tzinfo=timezone.utc),
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
