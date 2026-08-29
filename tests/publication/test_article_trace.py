"""Unit tests for article claim trace construction."""

from __future__ import annotations

import datetime as dt

import pytest

from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_models import (
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_trace import build_article_claim_trace

_NOW = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)


@pytest.mark.unit
def test_build_article_claim_trace_unions_provenance() -> None:
    sup1 = ArticleSupport(
        support_id="story:1:evidence:0:frag:2002",
        text="Факт 1",
        source_text="Факт 1",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("telegram:src:1:item:202:frag:2002",),
        fragment_ids=(2002,),
        source_item_ids=(202,),
        observed_at=_NOW,
    )
    sup2 = ArticleSupport(
        support_id="story:1:evidence:1:frag:2003",
        text="Факт 2",
        source_text="Факт 2",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("telegram:src:1:item:203:frag:2003",),
        fragment_ids=(2003,),
        source_item_ids=(203,),
        observed_at=_NOW,
    )

    supports = (sup1, sup2)
    ctx = ArticleEditorialContext(
        headline_candidates=("Заголовок",),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=("utilities",),
    )

    draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:2002",),
        lead="Лид",
        lead_support_ids=("story:1:evidence:0:frag:2002",),
        sections=(
            ArticleSection(
                heading="Раздел",
                heading_support_ids=("story:1:evidence:0:frag:2002",),
                paragraphs=(
                    ArticleParagraph(
                        text="Параграф с двумя фактами.",
                        cited_support_ids=(
                            "story:1:evidence:0:frag:2002",
                            "story:1:evidence:1:frag:2003",
                        ),
                    ),
                ),
            ),
        ),
    )

    trace = build_article_claim_trace(draft, ctx)
    assert len(trace) == 4  # TITLE, LEAD, H001, P001

    p_unit = next(u for u in trace if u.unit_id == "P001")
    assert p_unit.support_ids == (
        "story:1:evidence:0:frag:2002",
        "story:1:evidence:1:frag:2003",
    )
    assert p_unit.source_refs == (
        "telegram:src:1:item:202:frag:2002",
        "telegram:src:1:item:203:frag:2003",
    )
    assert p_unit.fragment_ids == (2002, 2003)
    assert p_unit.source_item_ids == (202, 203)
