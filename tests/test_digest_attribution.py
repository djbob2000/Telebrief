from __future__ import annotations

# ruff: noqa: S101
import pytest

from src.publication.digest_narrative import (
    DigestEditorialItemDraft,
    DigestNarrativeBlockDraft,
    DigestNarrativeDraft,
    _fix_duplicated_attribution,
    sanitize_digest_narrative_draft,
)


@pytest.mark.parametrize(
    "quote_marks",
    (("«", "»"), ('"', '"'), ("“", "”"), ("„", "“"), ("‘", "’"), ("'", "'")),
    ids=(
        "guillemets",
        "straight-double",
        "curly-double",
        "low-high-double",
        "curly-single",
        "straight-single",
    ),
)
def test_duplicate_cleanup_preserves_attributed_words_inside_quotes(
    quote_marks: tuple[str, str],
) -> None:
    opening, closing = quote_marks
    quoted_headline = f"{opening}По словам жителей, на Горе нет света{closing}"

    clean_headline, clean_body = _fix_duplicated_attribution(
        quoted_headline,
        "Жители сообщают, что на Горе нет света.",
    )

    assert clean_headline == quoted_headline
    assert clean_body == "На Горе нет света."
    assert "По словам жителей" in clean_headline
    assert "Жители сообщают" not in clean_body


@pytest.mark.parametrize(
    "quote_marks",
    (("«", "»"), ('"', '"'), ("“", "”"), ("„", "“"), ("‘", "’"), ("'", "'")),
    ids=(
        "guillemets",
        "straight-double",
        "curly-double",
        "low-high-double",
        "curly-single",
        "straight-single",
    ),
)
def test_full_narrative_sanitizer_preserves_attributed_quote_contents(
    quote_marks: tuple[str, str],
) -> None:
    opening, closing = quote_marks
    headline = f"{opening}По словам жителей, на Горе нет света{closing}"
    draft = DigestNarrativeDraft(
        blocks=(
            DigestNarrativeBlockDraft(
                block_id="utilities",
                items=(
                    DigestEditorialItemDraft(
                        headline=headline,
                        body="Жители сообщают, что на Горе нет света.",
                        covered_story_ids=("story:power",),
                        cited_support_ids=("support:resident-report",),
                    ),
                ),
            ),
        ),
    )

    sanitized = sanitize_digest_narrative_draft(draft)
    item = sanitized.blocks[0].items[0]

    assert item.headline == headline
    assert item.body == "На Горе нет света."
    assert "По словам жителей" in item.headline


def test_duplicate_cleanup_preserves_qualified_headline_source() -> None:
    headline = "В районе РТС, по словам жителя с улицы Шевченко, неделю нет света"

    clean_headline, clean_body = _fix_duplicated_attribution(
        headline,
        "Житель сообщает, что в районе РТС уже неделю нет света.",
    )

    assert clean_headline == headline
    assert clean_body == "В районе РТС уже неделю нет света."
    assert "по словам жителя с улицы Шевченко" in clean_headline
    assert "Житель сообщает" not in clean_body


def test_duplicate_cleanup_preserves_qualified_attribution_at_headline_start() -> None:
    headline = "По словам жителя с улицы Шевченко, в РТС нет света"

    clean_headline, clean_body = _fix_duplicated_attribution(
        headline,
        "Житель сообщает, что в РТС нет света.",
    )

    assert clean_headline == headline
    assert clean_body == "В РТС нет света."
    assert "По словам жителя с улицы Шевченко" in clean_headline


def test_duplicate_cleanup_preserves_quote_wrapped_qualified_attribution() -> None:
    headline = "«По словам жителя с улицы Шевченко, в РТС нет света»"

    clean_headline, clean_body = _fix_duplicated_attribution(
        headline,
        "Житель сообщает, что в РТС нет света.",
    )

    assert clean_headline == headline
    assert clean_body == "В РТС нет света."
    assert "По словам жителя с улицы Шевченко" in clean_headline


def test_duplicate_cleanup_preserves_qualified_body_attribution_prefix() -> None:
    headline = "«По словам жителя, в РТС нет света»"
    body = "По словам жителя с улицы Шевченко, в РТС уже неделю нет света."

    clean_headline, clean_body = _fix_duplicated_attribution(headline, body)

    assert clean_headline == headline
    assert clean_body == body
    assert "с улицы Шевченко" in clean_body


def test_duplicate_cleanup_preserves_unbounded_headline_attribution_with_place() -> None:
    headline = "По словам жителя Горы, на Горе нет света"

    clean_headline, clean_body = _fix_duplicated_attribution(
        headline,
        "Житель сообщает, что на Горе нет света.",
    )

    assert clean_headline == headline
    assert clean_body == "На Горе нет света."


def test_duplicate_cleanup_preserves_unbounded_body_attribution_with_place() -> None:
    headline = "«По словам жителя, на Горе нет света»"
    body = "По словам жителя Горы, на Горе нет света."

    clean_headline, clean_body = _fix_duplicated_attribution(headline, body)

    assert clean_headline == headline
    assert clean_body == body


def test_duplicate_cleanup_still_removes_simple_infix_attribution() -> None:
    clean_headline, clean_body = _fix_duplicated_attribution(
        "В районе РТС, по словам жителя, неделю нет света",
        "Житель сообщает, что электроснабжение в районе РТС отсутствует уже неделю.",
    )

    assert clean_headline == "В районе РТС неделю нет света"
    assert (
        clean_body == "Житель сообщает, что электроснабжение в районе РТС отсутствует уже неделю."
    )
    assert "по словам жителя" not in clean_headline
    assert "Житель сообщает" in clean_body


def test_chat_normalization_retains_exact_quote_and_repairs_surrounding_prose() -> None:
    from src.publication.digest_narrative import _fix_chat_leaks

    quote = "«В бердянском чате сообщили, что ранее также света не было»"
    text = f"{quote}. В сообщении в бердянском чате сообщили, что заполняют отопление."
    assert _fix_chat_leaks(text) == f"{quote}. Сообщается, что заполняют отопление."
