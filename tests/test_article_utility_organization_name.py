# ruff: noqa: S101
"""Utility organization names keep their source form in typographic quotes."""

from __future__ import annotations

from types import SimpleNamespace

from src.publication.article_finalization import _wrap_supported_organization_names
from src.publication.article_quality import (
    supported_organization_name_mentions,
    utility_organization_name_spans,
)

SOURCE = "Жительнице Теплосеть ответила, что отопление запустить пока не получается."


def _context(source: str, *, evidence_kind: str = "community_report") -> SimpleNamespace:
    support = SimpleNamespace(
        text=source,
        source_text=source,
        publication_use="PUBLISH",
        evidence_kind=evidence_kind,
        temporal_role="CURRENT_WINDOW",
        reply_parent_context_text="",
    )
    return SimpleNamespace(support_by_id={"s1": support})


def test_exact_unquoted_source_name_is_wrapped_without_changing_its_form():
    text = "Горводоканал и Теплосеть не могут вместе решить вопрос."
    wrapped = _wrap_supported_organization_names(text, ["s1"], _context(SOURCE), None)

    assert wrapped == "Горводоканал и «Теплосеть» не могут вместе решить вопрос."


def test_declined_prose_form_is_reported_for_editor_repair_not_rewritten_by_code():
    text = "В Теплосетях жительнице ответили, что запустить отопление пока не получается."

    spans = utility_organization_name_spans(text, ["s1"], _context(SOURCE))

    assert [(root, word, exact) for root, word, _span, exact in spans] == [
        ("теплосет", "Теплосетях", False)
    ]
    wrapped = _wrap_supported_organization_names(text, ["s1"], _context(SOURCE), None)
    assert wrapped == text


def test_correct_quoted_source_form_is_not_reported():
    text = "В «Теплосеть» жительнице ответили, что отопление пока не запустили."

    assert utility_organization_name_spans(text, ["s1"], _context(SOURCE)) == ()
    assert supported_organization_name_mentions(text, ["s1"], _context(SOURCE), None) == ()


def test_lowercase_common_noun_is_not_an_organization_mention():
    text = "Жители обсуждают теплосети и отопительный сезон."

    assert utility_organization_name_spans(text, ["s1"], _context(SOURCE)) == ()


def test_name_absent_from_cited_sources_is_never_flagged():
    text = "В Теплосетях сообщили о плановых работах."
    unrelated = "Сообщение о плановых работах на улице без названий организаций."

    assert utility_organization_name_spans(text, ["s1"], _context(unrelated)) == ()


def test_name_inside_direct_speech_is_not_flagged():
    text = "Жительница написала: «Теплосетях ответили, что отопления не будет»."

    assert utility_organization_name_spans(text, ["s1"], _context(SOURCE)) == ()


def test_resident_question_support_does_not_ground_the_organization():
    text = "В Теплосетях жительнице ответили, что отопление пока не запустили."

    spans = utility_organization_name_spans(
        text,
        ["s1"],
        _context(SOURCE, evidence_kind="resident_question"),
    )

    assert spans == ()


RES_SOURCE = "Ремонт проводит РЭС, а подача от БГРЭС восстановлена по данным жителей."


def test_grid_company_acronyms_are_matched_as_whole_tokens():
    text = "Работы проводит РЭС, а подачу от БГРЭС ждут жители."

    spans = utility_organization_name_spans(text, ["s1"], _context(RES_SOURCE))

    assert {word for _root, word, _span, _exact in spans} == {"РЭС", "БГРЭС"}


def test_declined_acronym_is_reported_but_not_inside_longer_words():
    text = "Жители звонили в РЭСу, а ресторан и РЭСК не упоминались."

    spans = utility_organization_name_spans(text, ["s1"], _context(RES_SOURCE))

    assert [word for _root, word, _span, _exact in spans] == ["РЭСу"]
