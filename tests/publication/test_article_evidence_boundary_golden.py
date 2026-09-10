"""Golden regression test suite for article evidence boundary and hallucination detection."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_validator import validate_article_draft

_FIXTURE_PATH = (
    Path(__file__).parent.parent / "fixtures" / "berdyansk_article_evidence_boundary_golden.json"
)
_NOW = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)


@pytest.mark.unit
def test_article_evidence_boundary_golden_suite() -> None:
    assert _FIXTURE_PATH.exists(), f"Missing fixture at {_FIXTURE_PATH}"
    with _FIXTURE_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    cases = data.get("cases", [])
    assert len(cases) >= 6

    config = PublicationEditorialConfig(
        article_min_words=5,
        article_max_words=500,
        article_min_sections=1,
        article_max_sections=10,
    )

    for case in cases:
        case_id = case["id"]
        support_texts = case["support"]
        draft_text = case["draft"]
        expected_valid = case["expected_valid"]
        expected_code = case.get("expected_code")

        supports = [
            ArticleSupport(
                support_id=f"sup:{idx}",
                text=st,
                source_text=st,
                support_kind="evidence",
                publication_use="PUBLISH",
                source_refs=(f"ref:{idx}",),
                fragment_ids=(idx + 100,),
                source_item_ids=(idx + 1,),
                observed_at=_NOW,
            )
            for idx, st in enumerate(support_texts)
        ]

        support_ids = tuple(s.support_id for s in supports)
        ctx = ArticleEditorialContext(
            headline_candidates=("Заголовок",),
            support_index=tuple(supports),
            support_by_id={s.support_id: s for s in supports},
            recurring_topics=("utilities",),
        )

        claim_atom = ArticleClaimAtom(text=draft_text, cited_support_ids=support_ids)
        sup_claim = ArticleClaimAtom(text=support_texts[0], cited_support_ids=support_ids)

        draft = StructuredArticleDraft(
            title=support_texts[0],
            title_support_ids=support_ids,
            title_claims=(sup_claim,),
            lead=support_texts[0],
            lead_support_ids=support_ids,
            lead_claims=(sup_claim,),
            sections=(
                ArticleSection(
                    heading=support_texts[0],
                    heading_support_ids=support_ids,
                    heading_claims=(sup_claim,),
                    paragraphs=(
                        ArticleParagraph(
                            text=draft_text,
                            cited_support_ids=support_ids,
                            claims=(claim_atom,),
                        ),
                    ),
                ),
            ),
            word_count=len(draft_text.split()) + 15,
        )

        result = validate_article_draft(draft, ctx, config)
        assert (
            result.is_valid == expected_valid
        ), f"Case '{case_id}' expected is_valid={expected_valid}, got {result.is_valid}. Issues: {result.issues}"

        if not expected_valid and expected_code:
            issue_codes = [iss.code for iss in result.issues]
            assert (
                expected_code in issue_codes
            ), f"Case '{case_id}' expected code '{expected_code}' in {issue_codes}"


@pytest.mark.unit
def test_run48_aggregate_mixed_paraphrases_and_unsupported_destination() -> None:
    sup1 = ArticleSupport(
        support_id="sup:1",
        text="Есть магазины, кафе, где можно зарядить телефон.",
        source_text="Есть магазины, кафе, где можно зарядить телефон.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:1",),
        fragment_ids=(101,),
        source_item_ids=(1,),
        observed_at=_NOW,
    )
    sup2 = ArticleSupport(
        support_id="sup:2",
        text="Есть рейсы в Ростов и Таганрог.",
        source_text="Есть рейсы в Ростов и Таганрог.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:2",),
        fragment_ids=(102,),
        source_item_ids=(2,),
        observed_at=_NOW,
    )
    ctx = ArticleEditorialContext(
        headline_candidates=("Заголовок",),
        support_index=(sup1, sup2),
        support_by_id={"sup:1": sup1, "sup:2": sup2},
        recurring_topics=(),
    )
    config = PublicationEditorialConfig(
        article_min_words=5,
        article_max_words=500,
        article_min_sections=1,
        article_max_sections=10,
    )
    draft = StructuredArticleDraft(
        title="Городская обстановка и транспорт",
        title_support_ids=("sup:1",),
        title_claims=(
            ArticleClaimAtom(
                text="Городская обстановка и транспорт",
                cited_support_ids=("sup:1",),
            ),
        ),
        lead="В городе работают пункты подзарядки и курсируют междугородние автобусы.",
        lead_support_ids=("sup:1", "sup:2"),
        lead_claims=(
            ArticleClaimAtom(
                text="В городе работают пункты подзарядки и курсируют междугородние автобусы",
                cited_support_ids=("sup:1", "sup:2"),
            ),
        ),
        sections=(
            ArticleSection(
                heading="Обслуживание и рейсы",
                heading_support_ids=("sup:1", "sup:2"),
                heading_claims=(),
                paragraphs=(
                    ArticleParagraph(
                        text="Часть магазинов и кафе предоставляет возможность зарядить телефон.",
                        cited_support_ids=("sup:1",),
                        claims=(
                            ArticleClaimAtom(
                                text="Часть магазинов и кафе предоставляет возможность зарядить телефон",
                                cited_support_ids=("sup:1",),
                            ),
                        ),
                    ),
                    ArticleParagraph(
                        text="Доступны рейсы в Москву и Воронеж.",
                        cited_support_ids=("sup:2",),
                        claims=(
                            ArticleClaimAtom(
                                text="Доступны рейсы в Москву и Воронеж",
                                cited_support_ids=("sup:2",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        word_count=40,
    )

    result = validate_article_draft(draft, ctx, config)
    assert result.is_valid is False
    assert sum(v.startswith("CLAIM_LEXICAL_DIVERGENCE") for v in result.all_violations) >= 1
    assert any(v.startswith("UNSUPPORTED_PROPER_NAME") for v in result.violations)
    assert not any(
        v.startswith("UNSUPPORTED_CLAIM_ATOM") for v in result.violations if "charging" in v.lower()
    )
