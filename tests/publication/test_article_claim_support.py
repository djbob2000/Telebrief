"""Tests for conservative deterministic claim-to-support assessment."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from src.publication.article_claim_support import (
    assess_claim_against_supports,
)
from src.publication.article_context import ArticleSupport
from src.publication.article_semantic_support import assess_semantic_support

_T0 = dt.datetime(2026, 8, 29, 10, 0, tzinfo=dt.timezone.utc)
_REGRESSION_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "article_semantic_support_regressions.json"
)


def _load_semantic_cases() -> dict[str, list[dict[str, object]]]:
    return json.loads(_REGRESSION_FIXTURE.read_text(encoding="utf-8"))


def make_support(
    text: str,
    source_text: str = "",
    support_id: str = "SUP-1",
    support_kind: str = "evidence",
    publication_use: str = "PUBLISH",
) -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text=text,
        source_text=source_text or text,
        support_kind="operational" if support_kind == "operational" else "evidence",
        publication_use="PUBLISH" if publication_use == "PUBLISH" else "CONTEXT",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_T0,
    )


@pytest.mark.unit
def test_assess_claim_supported_qualitative_paraphrase() -> None:
    support = make_support(
        text="В центре и Нагорной части города нет электричества",
        source_text="В центре и на Нагорной части Бердянска аварийное отключение света.",
    )

    assessment = assess_claim_against_supports(
        "В центре и Нагорной части сохраняются перебои с электричеством",
        [support],
    )
    assert assessment.supported is True
    assert assessment.content_coverage >= 0.70
    assert len(assessment.unsupported_concrete_claims) == 0


@pytest.mark.unit
def test_assess_claim_unsupported_soft_claims_rejected() -> None:
    support = make_support(
        text="В центре и Нагорной части города нет электричества",
        source_text="В центре и на Нагорной части Бердянска аварийное отключение света.",
    )

    # 1. Generator adaptation
    res1 = assess_claim_against_supports(
        "Отключения вынуждают жителей переходить на автономные генераторы",
        [support],
    )
    assert res1.supported is False
    assert any("генератор" in s for s in res1.unsupported_content_stems)

    # 2. Generator spare parts
    res2 = assess_claim_against_supports(
        "В городе отмечается дефицит запчастей для генераторов",
        [support],
    )
    assert res2.supported is False

    # 3. Water delivery
    support_water = make_support(
        text="На верхних этажах слабый напор воды",
        source_text="Жители сообщают о слабом напоре воды на верхних этажах.",
    )
    res3 = assess_claim_against_supports(
        "Жители организуют самостоятельный подвоз питьевой воды",
        [support_water],
    )
    assert res3.supported is False
    assert any("подвоз" in s for s in res3.unsupported_content_stems)

    # 4. Telecom impact on emergency requests
    support_telecom = make_support(
        text="В Черемушках нестабильна мобильная связь",
        source_text="В районе Черемушки наблюдаются перебои с мобильной связью.",
    )
    res4 = assess_claim_against_supports(
        "Нестабильная связь осложняет передачу заявок аварийным службам",
        [support_telecom],
    )
    assert res4.supported is False
    assert any(
        "заявк" in s or "аварийн" in s or "служб" in s for s in res4.unsupported_content_stems
    )


@pytest.mark.unit
def test_assess_claim_unsupported_concrete_claims_fail_fast() -> None:
    support = make_support(
        text="Авария на водоводе в центре",
        source_text="Авария на водоводе в центре города.",
    )

    # Unsupported phone, price, percent, date, duration
    res_phone = assess_claim_against_supports(
        "Звоните по телефону +7 990 123-45-67 для справок",
        [support],
    )
    assert res_phone.supported is False
    assert len(res_phone.unsupported_concrete_claims) >= 1

    res_num = assess_claim_against_supports(
        "Без воды остались 5000 жителей",
        [support],
    )
    assert res_num.supported is False
    assert len(res_num.unsupported_concrete_claims) >= 1


@pytest.mark.unit
def test_run48_faithful_paraphrases_are_not_hard_rejected() -> None:
    cases = _load_semantic_cases()["faithful_paraphrases"]
    for case in cases:
        supports = [
            make_support(str(text), support_id=f"SUP-{idx}")
            for idx, text in enumerate(case["supports"], start=1)  # type: ignore[arg-type]
        ]
        assessment = assess_claim_against_supports(str(case["claim"]), supports)
        assert assessment.supported is True, case["id"]


@pytest.mark.unit
def test_real_semantic_additions_remain_hard_rejected() -> None:
    cases = _load_semantic_cases()["real_semantic_additions"]
    for case in cases:
        supports = [
            make_support(str(text), support_id=f"SUP-{idx}")
            for idx, text in enumerate(case["supports"], start=1)  # type: ignore[arg-type]
        ]
        assessment = assess_claim_against_supports(str(case["claim"]), supports)
        assert assessment.supported is False, case["id"]


@pytest.mark.unit
def test_semantic_support_matches_russian_inflection_and_proven_paraphrases() -> None:
    signals = assess_semantic_support(
        "Часть магазинов и кафе предоставляет возможность зарядить телефон.",
        ["Есть магазины, кафе, где можно зарядить телефон."],
    )
    assert signals.blocking_terms == ()


@pytest.mark.unit
def test_semantic_support_blocks_multiple_new_content_terms() -> None:
    signals = assess_semantic_support(
        "Отключения вынуждают жителей переходить на автономные генераторы.",
        ["В центре города нет электричества."],
    )
    assert any("генератор" in term for term in signals.blocking_terms)


@pytest.mark.unit
def test_semantic_support_blocks_new_proper_name_destination() -> None:
    signals = assess_semantic_support(
        "Доступны рейсы в Москву и Воронеж.",
        ["Есть рейсы в Ростов и Таганрог."],
    )
    assert set(signals.unmatched_proper_names) >= {"москву", "воронеж"}
    assert set(signals.blocking_proper_names) >= {"москву", "воронеж"}


@pytest.mark.unit
def test_low_lexical_coverage_alone_is_diagnostic() -> None:
    support = make_support("Есть магазины, кафе, где можно зарядить телефон")
    assessment = assess_claim_against_supports(
        "Часть магазинов и кафе предоставляет возможность зарядить телефон",
        [support],
    )
    assert assessment.supported is True
    assert assessment.unsupported_concrete_claims == ()
    assert assessment.blocking_semantic_terms == ()


@pytest.mark.unit
def test_new_semantic_content_still_blocks_without_concrete_regex_claim() -> None:
    support = make_support("В центре города нет электричества")
    assessment = assess_claim_against_supports(
        "Отключения вынуждают жителей переходить на автономные генераторы",
        [support],
    )
    assert assessment.supported is False
    assert assessment.blocking_semantic_terms


_ATTEMPT74_FIXTURE = Path(__file__).parents[1] / "fixtures" / "article_attempt74_regressions.json"


def _load_attempt74_cases() -> dict[str, list[dict[str, object]]]:
    return json.loads(_ATTEMPT74_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_attempt74_fixture_contains_required_regression_groups() -> None:
    cases = _load_attempt74_cases()
    assert {"expected_pass", "expected_block", "cross_language_pass", "direct_quote_cases"} <= set(
        cases
    )
    assert {str(c["id"]) for c in cases["expected_block"]} >= {
        "unsupported-azmol-location",
        "unsupported-water-domain",
        "unsupported-street-scope",
    }


@pytest.mark.unit
def test_ru_ua_uav_paraphrase_is_semantically_supported() -> None:
    support = make_support("застосувала 626 безпілотників")
    assessment = assess_claim_against_supports("Применила 626 БПЛА", [support])
    assert assessment.supported is True
    assert assessment.unsupported_concrete_claims == ()


@pytest.mark.unit
def test_ru_ua_over_hundred_reports_is_semantically_supported() -> None:
    support = make_support("надійшло понад сотню повідомлень")
    assessment = assess_claim_against_supports("Поступило более ста сообщений", [support])
    assert assessment.supported is True
