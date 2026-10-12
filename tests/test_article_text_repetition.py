"""Near-identical retelling is a repeat even without shared Story linkage."""

# ruff: noqa: S101
import pytest

from src.publication.article_quality import _units_retell_same_sentence


@pytest.mark.parametrize(
    ("first", "later"),
    [
        (
            "Жительница, чей дом оставался без электричества, сообщила, что ей пришлось временно переехать.",
            "По словам жительницы, её дом оставался без света, и ей пришлось временно переехать.",
        ),
        (
            "Одна горожанка рассказала, что дома у неё отключили свет, а в центре города, куда она пришла, не было воды.",
            "Одна жительница сообщала, что в центре города не было воды.",
        ),
    ],
)
def test_retold_sentence_is_a_repeat(first: str, later: str) -> None:
    assert _units_retell_same_sentence(first, later)


@pytest.mark.parametrize(
    ("first", "later"),
    [
        (
            "На улице Морозова свет включили на 20 минут и снова отключили.",
            "На улице Гайдара свет включили на 20 минут и снова отключили.",
        ),
        (
            "На Косе электричества нет со 2 августа, на подстанции никого не видели.",
            "В медицинском центре проводился консультативный приём врачей.",
        ),
    ],
)
def test_different_report_is_not_a_repeat(first: str, later: str) -> None:
    assert not _units_retell_same_sentence(first, later)
