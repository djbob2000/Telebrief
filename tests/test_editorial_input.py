"""Tests for conservative preparation of the full editorial source bundle."""

from datetime import datetime, timezone

from src.collector import Message
from src.config_loader import ChannelConfig, SourceRoleResolver
from src.editorial_input import EditorialInputBuilder


def _message(text: str, message_id: int, *, reply_to_id: int | None = None) -> Message:
    return Message(
        text=text,
        sender="Житель",
        timestamp=datetime(2026, 8, 14, 12, message_id % 60, tzinfo=timezone.utc),
        link=f"https://t.me/source/{message_id}",
        channel_name="Source",
        has_media=False,
        media_type="",
        message_id=message_id,
        reply_to_id=reply_to_id,
    )


def test_input_builder_assigns_refs_and_keeps_parent_context_for_short_reply():
    messages = [
        _message("У кого сейчас есть вода?", 123),
        _message("На Колонии тоже нет", 124, reply_to_id=123),
    ]
    builder = EditorialInputBuilder(
        SourceRoleResolver([ChannelConfig(id="@source", name="Source")])
    )

    bundle = builder.build({"Source": messages})

    assert bundle.records["S000001"].message.message_id == 123
    assert bundle.records["S000002"].parent_ref == "S000001"
    assert "У кого сейчас есть вода?" in bundle.prompt_text
    assert "На Колонии тоже нет" in bundle.prompt_text
    assert "reply_to" in bundle.prompt_text
    assert bundle.total_messages == 2
    assert bundle.candidate_count == 2


def test_input_builder_filters_explicit_noise_but_not_short_city_observation():
    messages = [
        _message("Вода пропала на Колонии", 1),
        _message("🔥🔥🔥", 2),
        _message("Курс доллара 41.20, евро 45.10", 3),
        _message("Реклама: куплю вашу квартиру", 4),
        _message("На Морской тоже", 5),
    ]
    builder = EditorialInputBuilder(
        SourceRoleResolver([ChannelConfig(id="@source", name="Source")])
    )

    bundle = builder.build({"Source": messages})

    assert bundle.candidate_count == 2
    assert {record.message.message_id for record in bundle.records.values()} == {1, 5}
    assert "На Морской тоже" in bundle.prompt_text
    assert "Курс доллара" not in bundle.prompt_text
    assert "Реклама" not in bundle.prompt_text


def test_input_builder_uses_topic_and_channel_roles_without_inferring_names():
    channels = [
        ChannelConfig(
            id="@mixed",
            name="Mixed",
            source_type="mixed",
        ),
        ChannelConfig(
            id="@forum",
            name="Forum",
            source_type="community",
        ),
    ]
    builder = EditorialInputBuilder(SourceRoleResolver(channels))
    messages = [_message("Официального сообщения нет", 1)]
    messages[0].channel_name = "Forum — Новости"

    bundle = builder.build({"Forum — Новости": messages})

    assert bundle.records["S000001"].source_type == "community"
