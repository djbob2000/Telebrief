# ruff: noqa: S101
"""Closed chat-kitchen repair: community wording is neutralised, facts stay untouched."""

from __future__ import annotations

import pytest

from src.publication.article_kitchen import (
    CHAT_KITCHEN_LEAK_PATTERN,
    repair_chat_kitchen_phrases,
)


@pytest.mark.parametrize(
    ("leaked", "expected"),
    [
        ("График, о котором спорят в чатах.", "График, о котором спорят в обсуждениях."),
        (
            "«Акз-ртс света нет» — с горечью замечали в городском чате.",
            "«Акз-ртс света нет» — с горечью замечали в обсуждениях.",
        ),
        ("В местных чатах писали о воде.", "В обсуждениях писали о воде."),
        ("Ответ в комментариях был коротким.", "Ответ в обсуждениях был коротким."),
        (
            "Новость появилась в телеграм-канале города.",
            "Новость появилась в обсуждениях города.",
        ),
        ("Участники чата сообщили о перебое.", "Жители сообщили о перебое."),
        ("Участница чата написала, что свет вернули.", "Жительница написала, что свет вернули."),
        ("По итогам переклички стало известно.", "По итогам сводки жителей стало известно."),
        ("Перекличка показала две улицы.", "Сводка жителей показала две улицы."),
    ],
)
def test_closed_forms_are_repaired_and_no_longer_match_validator(leaked: str, expected: str):
    repaired = repair_chat_kitchen_phrases(leaked)

    assert repaired == expected
    assert CHAT_KITCHEN_LEAK_PATTERN.search(repaired) is None


def test_repair_is_idempotent():
    text = "Жители сообщали в городском чате, а участники чата ждали переклички."

    once = repair_chat_kitchen_phrases(text)

    assert repair_chat_kitchen_phrases(once) == once


def test_repair_keeps_facts_numbers_names_and_quotes_untouched():
    text = "«Свет будет через 3–4 дня», — писала жительница АКЗ 2 ноября в чатах."

    repaired = repair_chat_kitchen_phrases(text)

    assert repaired == "«Свет будет через 3–4 дня», — писала жительница АКЗ 2 ноября в обсуждениях."


def test_unknown_inflection_is_left_for_the_validator_to_block():
    text = "Перекличкою занялись вечером."

    repaired = repair_chat_kitchen_phrases(text)

    assert repaired == text
    assert CHAT_KITCHEN_LEAK_PATTERN.search(repaired) is not None


def test_neutral_prose_is_not_changed():
    text = "В городе перебои с водой; ремонт в группах домов завершат к пятнице."

    assert repair_chat_kitchen_phrases(text) == text
