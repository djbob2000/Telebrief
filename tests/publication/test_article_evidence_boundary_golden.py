"""Golden regression test suite for article evidence boundary and hallucination detection."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_models import (
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

        draft = StructuredArticleDraft(
            title="Заголовок статьи о событиях",
            title_support_ids=support_ids,
            lead="Вводный лид статьи с кратким описанием событий дня.",
            lead_support_ids=support_ids,
            sections=(
                ArticleSection(
                    heading="Основной раздел",
                    heading_support_ids=support_ids,
                    paragraphs=(
                        ArticleParagraph(
                            text=draft_text,
                            cited_support_ids=support_ids,
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
