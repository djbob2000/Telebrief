"""Digits that restate a number word or decimal are supported; new numbers are not."""

# ruff: noqa: S101
import pytest

from src.publication.article_claims import find_unsupported_claims


@pytest.mark.parametrize(
    ("claim", "source"),
    [
        (
            "Дети проучились 1 день за полтора месяца.",
            "Дети в школе один день проучились со светом за 1.5 месяца !",
        ),
        ("Света нет возле 1-й школы.", "Нет света возле первой школы"),
        ("Свет был 1,5 суток.", "свет был 1.5 суток"),
        ("Половина дома (1 подъезд) со светом.", "одна половина со светом"),
    ],
)
def test_restated_number_is_supported(claim: str, source: str) -> None:
    assert find_unsupported_claims(claim, [source]) == ()


@pytest.mark.parametrize(
    ("claim", "source"),
    [
        ("Света нет 7 дней.", "Света нет один день"),
        ("Света нет 3 дня.", "Света нет два дня"),
        ("Свет был 2,5 суток.", "свет был 1.5 суток"),
    ],
)
def test_invented_number_is_still_rejected(claim: str, source: str) -> None:
    assert find_unsupported_claims(claim, [source])
