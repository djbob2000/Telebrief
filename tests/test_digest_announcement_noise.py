# ruff: noqa: S101

from types import SimpleNamespace

import pytest

from src.publication.digest_presentation import _is_usable_fact_line
from src.publication.story_quality import (
    is_non_editorial_fact,
    validate_story_publication_eligibility,
)


def _payload(*texts: str) -> SimpleNamespace:
    return SimpleNamespace(
        headline=texts[0],
        digest_summary=texts[0],
        category="",
        evidence_items=[
            SimpleNamespace(text=text, kind="community_report", publication_use="PUBLISH")
            for text in texts
        ],
    )


@pytest.mark.parametrize(
    "text",
    [
        "В Бердянске в торговом центре Улей открытие новой аптеки назначено на 10 октября; "
        "работать она будет с 9:00 до 18:00.",
        "Магазин Улей открывается с 10-го числа, по сообщению жителя.",
        "Расписание отделения банка в Бердянске написано на двери: банк работает с 9:00 до 13:00.",
        "Информация на двери поликлиники в Бердянске: врач принимает с 9:00 до 12:00.",
        "На двери написано: пенсионный фонд в Бердянске принимает посетителей до 15:00.",
        "На двери библиотеки написано: читальный зал открылся после ремонта.",
    ],
)
def test_announcements_and_posted_schedules_remain_publishable(text: str) -> None:
    assert validate_story_publication_eligibility(_payload(text)) == (True, None)
    assert _is_usable_fact_line(text)


@pytest.mark.parametrize(
    "text",
    [
        "С 10го числа в Улей открытие.",
        "С 10-го числа в Комете открытие.",
        "Информация об открытии заведения, может на двери пишут.",
        "Может на двери пишут.",
        "Посмотреть на двери.",
    ],
)
def test_empty_announcement_and_signpost_context_is_filtered(text: str) -> None:
    assert not validate_story_publication_eligibility(_payload(text))[0]
    assert not _is_usable_fact_line(text)


def test_context_reply_does_not_veto_another_grounded_support() -> None:
    report = "В Бердянске банк работает с 9:00 до 13:00."
    assert validate_story_publication_eligibility(
        _payload(report, "Информация об открытии заведения, может на двери пишут.")
    ) == (True, None)


@pytest.mark.parametrize(
    "text",
    [
        "На двери написано: у банка по понедельникам выходной.",
        "Информация на двери поликлиники: вход со двора.",
        "Информация на двери банка: часы работы с 9:00 до 13:00.",
    ],
)
def test_posted_practical_details_do_not_require_an_explicit_verb(text: str) -> None:
    assert not is_non_editorial_fact(text)
    assert _is_usable_fact_line(text)
