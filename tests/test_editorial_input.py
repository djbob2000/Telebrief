"""Tests for conservative preparation of the full editorial source bundle."""

from datetime import datetime, timezone

from src.collector import Message
from src.config_loader import ChannelConfig, SourceRoleResolver
from src.editorial_input import EditorialInputBuilder
from src.editorial_models import PreparedBundle, SourceRecord


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


def test_input_builder_removes_long_commercial_and_currency_messages():
    messages = [
        _message(
            "Бердянск. Заправка автокондиционеров, диагностика и ремонт. "
            "Работаем ежедневно, запись по телефону +7 (990) 123-45-67. "
            "Адрес: улица Морская, 10. Подробности: https://t.me/ac_service",
            1,
        ),
        _message(
            "Курс доллара и евро на сегодня: обмен валюты, выгодные условия. "
            "Телефон +7 990 111-22-33, подробности в канале https://t.me/currency",
            2,
        ),
        _message(
            "Помощь с банковскими картами и оформлением пенсий. "
            "Консультация по телефону +7 990 333-44-55, пишите в личные сообщения.",
            3,
        ),
        _message("Вода пропала на Колонии", 4),
    ]
    builder = EditorialInputBuilder(
        SourceRoleResolver([ChannelConfig(id="@source", name="Source")])
    )

    bundle = builder.build({"Source": messages})

    assert [record.message.message_id for record in bundle.records.values()] == [4]
    assert "автокондиционеров" not in bundle.prompt_text
    assert "Курс доллара" not in bundle.prompt_text
    assert "банковскими картами" not in bundle.prompt_text


def test_input_builder_keeps_amounts_and_official_dispatch_phone():
    payout = _message("Единовременная выплата составит 1000 гривен", 1)
    utility = _message(
        "Коммунальная служба сообщила о ремонте сети. Телефон диспетчерской: +7 (990) 123-45-67",
        2,
    )
    utility.channel_name = "Utility"
    builder = EditorialInputBuilder(
        SourceRoleResolver(
            [
                ChannelConfig(id="@source", name="Source", source_type="mixed"),
                ChannelConfig(id="@utility", name="Utility", source_type="official"),
            ]
        )
    )

    bundle = builder.build({"Source": [payout], "Utility": [utility]})

    assert {record.message.message_id for record in bundle.records.values()} == {1, 2}


def test_input_builder_filters_financial_rate_spam_but_keeps_ticket_notice():
    messages = [
        _message(
            "О Б Н А Л И Ч И В А Н И Е. Продажа. #Покупка. синий 85, белый 83, евро 99",
            1,
        ),
        _message("Продажа билетов временно приостановлена", 2),
    ]
    builder = EditorialInputBuilder(
        SourceRoleResolver([ChannelConfig(id="@source", name="Source")])
    )

    bundle = builder.build({"Source": messages})

    assert [record.message.message_id for record in bundle.records.values()] == [2]


def test_input_builder_selects_story_refs_and_reply_parent_without_reprocessing():
    parent = _message("У кого сейчас есть вода?", 13)
    reply = _message("На Колонии тоже нет", 14, reply_to_id=13)
    unrelated = _message("В городе обсуждали транспорт", 15)
    builder = EditorialInputBuilder(
        SourceRoleResolver(
            [
                ChannelConfig(id="@source", name="Source", source_type="community"),
            ]
        )
    )
    bundle = builder.build({"Source": [parent, reply, unrelated]})

    selected = builder.select_records(bundle, ["S000002"])

    assert set(selected.records) == {"S000001", "S000002"}
    assert selected.records["S000002"].source_type == "community"
    assert 'reply_to: "У кого сейчас есть вода?"' in selected.prompt_text
    assert "В городе обсуждали транспорт" not in selected.prompt_text


def test_input_builder_limits_selected_evidence_in_requested_order():
    messages = [_message(f"Наблюдение {index}", index) for index in range(1, 4)]
    records = {
        f"S{index:06d}": SourceRecord(
            ref=f"S{index:06d}",
            message=message,
            source_type="community",
        )
        for index, message in enumerate(messages, start=1)
    }
    bundle = PreparedBundle(
        records=records,
        prompt_text="original",
        total_messages=3,
        candidate_count=3,
    )
    builder = EditorialInputBuilder(SourceRoleResolver([]))

    selected = builder.select_records(bundle, ["S000003", "S000001", "S000002"], max_refs=2)

    assert list(selected.records) == ["S000003", "S000001"]
    assert selected.records["S000003"].message.text == "Наблюдение 3"


def test_looks_commercial_preserves_mutual_aid_pairs():
    # Mutual aid / assistance must be KEPT (return False from _looks_commercial)
    assert not EditorialInputBuilder._looks_commercial(
        "В кафе сегодня можно бесплатно зарядить телефон, работаем, адрес: ул. Морская 10, звоните +79901234567",
        source_type="community",
    )
    assert not EditorialInputBuilder._looks_commercial(
        "Подвоз воды жителям АКЗ бесплатно во двор, звоните +79901234567",
        source_type="community",
    )
    assert not EditorialInputBuilder._looks_commercial(
        "В ДК открыт пункт обогрева, можно набрать воды и зарядить гаджеты",
        source_type="community",
    )


def test_looks_commercial_still_filters_commercial_pairs():
    # Commercial advertisements must be DROPPED (return True from _looks_commercial)
    assert EditorialInputBuilder._looks_commercial(
        "Бесплатная консультация, звоните +79901234567, запись по телефону",
        source_type="community",
    )
    assert EditorialInputBuilder._looks_commercial(
        "Доставка воды, выгодные цены, звоните +79901234567", source_type="community"
    )
    assert EditorialInputBuilder._looks_commercial(
        "Обмен валют по лучшему курсу, обналичивание карт в центре",
        source_type="community",
    )


def test_editorial_input_builder_never_mutates_source_type():
    resolver = SourceRoleResolver(
        [ChannelConfig(id="@test_channel", name="test_channel", source_type="community")]
    )
    builder = EditorialInputBuilder(resolver)
    msg = _message("В кафе можно бесплатно зарядить телефон", 1)
    msg.channel_name = "test_channel"
    bundle = builder.build({"test_channel": [msg]})
    assert "S000001" in bundle.records
    assert bundle.records["S000001"].source_type == "community"


def test_editorial_input_builder_with_city_context_resolver():
    from src.city_context import CityContextResolver

    resolver = CityContextResolver.from_yaml("data/city_profiles/berdyansk.yaml")
    role_resolver = SourceRoleResolver(
        [ChannelConfig(id="@source", name="Source", source_type="community")]
    )
    builder = EditorialInputBuilder(role_resolver, city_context_resolver=resolver)

    messages = [
        _message("На ул. Шевченко нет света", 1),
        _message("В центре дали воду", 2),
    ]
    bundle = builder.build({"Source": messages})

    assert bundle.records["S000001"].city_context is not None
    assert any(
        e.entity_id == "street:Шевченка" for e in bundle.records["S000001"].city_context.entities
    )
    assert bundle.records["S000002"].city_context is not None
    assert any(e.entity_id == "center" for e in bundle.records["S000002"].city_context.entities)

    # Verify rendered local_context line in prompt_text
    assert "local_context: street:Шевченка" in bundle.prompt_text
    assert "local_context: area:center" in bundle.prompt_text
