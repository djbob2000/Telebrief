# ruff: noqa: S101
"""Chat-roll detection: grounded place names in quotes are not speech; real speech chains still block."""

from __future__ import annotations

from types import SimpleNamespace

from src.publication.article_quality import (
    _has_consecutive_direct_speech_roll,
    _supported_non_speech_name_quote_spans,
)

SOURCE = (
    "Вокруг вас везде Зирочка Дом, Арле Дом, 14 этажка, Пионерская нечетная сторона вся, "
    "Гайдара всё, Грация, Восьмуха, Кирова"
)
PLACE_LIST_PARAGRAPH = (
    "В нагорной части города на улицах Гайдара, Кирова и Пионерской, а также возле "
    "«Зирочки Дом», «Арле Дом», 14-этажки, «Грации» и «Восьмухи» окна светились подолгу. "
    "Один из жителей перечислял эти адреса как карту освещённости, другой уточнял: "
    "«Не начало Гайдара а все Гайдара, и четные и нечетные горят». Возле «Черепахи» свет был."
)


def _context(source: str) -> SimpleNamespace:
    support = SimpleNamespace(
        text=source,
        source_text=source,
        publication_use="PUBLISH",
        evidence_kind="community_report",
        temporal_role="CURRENT_WINDOW",
        reply_parent_context_text="",
    )
    return SimpleNamespace(support_by_id={"s1": support})


def _spans_for(text: str, source: str) -> tuple[tuple[int, int], ...]:
    return _supported_non_speech_name_quote_spans(text, ["s1"], _context(source), None)


def test_grounded_place_list_after_preposition_is_not_a_speech_roll():
    spans = _spans_for(PLACE_LIST_PARAGRAPH, SOURCE)

    names = {PLACE_LIST_PARAGRAPH[start:end] for start, end in spans}

    assert {"Зирочки Дом", "Арле Дом"} <= names
    assert _has_consecutive_direct_speech_roll(PLACE_LIST_PARAGRAPH, spans) is False


def test_same_list_without_source_support_still_counts_as_speech_roll():
    unsupported_source = "Сообщение о перебоях со светом в районе."

    spans = _spans_for(PLACE_LIST_PARAGRAPH, unsupported_source)

    assert _has_consecutive_direct_speech_roll(PLACE_LIST_PARAGRAPH, spans) is True


def test_real_speech_chain_after_preposition_still_blocks_even_if_phrases_appear_in_source():
    source = "Жители писали: нет света и свет есть, люди ждут."
    text = "В районе, возле «нет света», «свет есть», жители ждали ответа."

    spans = _spans_for(text, source)

    assert spans == ()
    assert _has_consecutive_direct_speech_roll(text, spans) is True


def test_single_grounded_name_is_not_excluded_as_a_list():
    text = "Свет дали возле «Арле Дом», а дальше без изменений."

    spans = _spans_for(text, SOURCE)

    assert spans == ()


def test_inflected_name_matches_grounded_source_form():
    text = "Жители писали возле «Зирочки Дом» и «Арле Дом» вечером."

    spans = _spans_for(text, SOURCE)

    assert [text[start:end] for start, end in spans] == ["Зирочки Дом", "Арле Дом"]
