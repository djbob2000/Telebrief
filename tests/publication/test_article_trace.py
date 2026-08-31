"""Unit tests for article claim trace construction."""

from __future__ import annotations

import datetime as dt

import pytest

from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_models import (
    ArticleClaimAtom,
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
        temporal_role="CURRENT_WINDOW",
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
        temporal_role="CURRENT_WINDOW",
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
        title_claims=(
            ArticleClaimAtom(text="Заголовок", cited_support_ids=("story:1:evidence:0:frag:2002",)),
        ),
        lead="Лид",
        lead_support_ids=("story:1:evidence:0:frag:2002",),
        lead_claims=(
            ArticleClaimAtom(text="Лид", cited_support_ids=("story:1:evidence:0:frag:2002",)),
        ),
        sections=(
            ArticleSection(
                heading="Раздел",
                heading_support_ids=("story:1:evidence:0:frag:2002",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Раздел", cited_support_ids=("story:1:evidence:0:frag:2002",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="Параграф с двумя фактами.",
                        cited_support_ids=(
                            "story:1:evidence:0:frag:2002",
                            "story:1:evidence:1:frag:2003",
                        ),
                        claims=(
                            ArticleClaimAtom(
                                text="Факт 1",
                                cited_support_ids=("story:1:evidence:0:frag:2002",),
                            ),
                            ArticleClaimAtom(
                                text="Факт 2",
                                cited_support_ids=("story:1:evidence:1:frag:2003",),
                            ),
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
    assert p_unit.temporal_roles == ("CURRENT_WINDOW",)
    assert len(p_unit.claim_atoms) == 2
    assert p_unit.claim_atoms[0].text == "Факт 1"
    assert p_unit.claim_atoms[0].support_ids == ("story:1:evidence:0:frag:2002",)
    assert p_unit.claim_atoms[0].temporal_roles == ("CURRENT_WINDOW",)
    assert p_unit.claim_atoms[1].text == "Факт 2"
    assert p_unit.claim_atoms[1].support_ids == ("story:1:evidence:1:frag:2003",)


@pytest.mark.unit
def test_build_article_claim_trace_preserves_epistemic_metadata() -> None:
    sup = ArticleSupport(
        support_id="story:1:evidence:0:frag:2002",
        text="На Горе света нет",
        source_text="На Горе света нет",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("telegram:src:1:item:202:frag:2002",),
        fragment_ids=(2002,),
        source_item_ids=(202,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
        evidence_kind="community_report",
        source_roles=("community",),
    )

    ctx = ArticleEditorialContext(
        headline_candidates=("Заголовок",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        recurring_topics=("utilities",),
    )

    draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:2002",),
        title_claims=(
            ArticleClaimAtom(text="Заголовок", cited_support_ids=("story:1:evidence:0:frag:2002",)),
        ),
        lead="Лид",
        lead_support_ids=("story:1:evidence:0:frag:2002",),
        lead_claims=(
            ArticleClaimAtom(text="Лид", cited_support_ids=("story:1:evidence:0:frag:2002",)),
        ),
        sections=(
            ArticleSection(
                heading="Раздел",
                heading_support_ids=("story:1:evidence:0:frag:2002",),
                heading_claims=(
                    ArticleClaimAtom(
                        text="Раздел", cited_support_ids=("story:1:evidence:0:frag:2002",)
                    ),
                ),
                paragraphs=(
                    ArticleParagraph(
                        text="По сообщениям жителей, на Горе нет света.",
                        cited_support_ids=("story:1:evidence:0:frag:2002",),
                        claims=(
                            ArticleClaimAtom(
                                text="На Горе нет света",
                                cited_support_ids=("story:1:evidence:0:frag:2002",),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    trace = build_article_claim_trace(draft, ctx)
    unit = next(item for item in trace if item.unit_id == "P001")
    assert unit.evidence_kinds == ("community_report",)
    assert unit.source_roles == ("community",)
    assert unit.claim_atoms[0].evidence_kinds == ("community_report",)
    assert unit.claim_atoms[0].source_roles == ("community",)


@pytest.mark.unit
def test_build_article_claim_trace_propagates_generation_origin() -> None:
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
        temporal_role="CURRENT_WINDOW",
    )
    sup2 = ArticleSupport(
        support_id="story:2:evidence:0:frag:2003",
        text="Факт 2",
        source_text="Факт 2",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("telegram:src:1:item:203:frag:2003",),
        fragment_ids=(2003,),
        source_item_ids=(203,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    supports = (sup1, sup2)
    ctx = ArticleEditorialContext(
        headline_candidates=("Заголовок",),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
    )
    draft = StructuredArticleDraft(
        title="Заголовок",
        title_support_ids=("story:1:evidence:0:frag:2002",),
        title_generation_origin="AI",
        lead="Лид",
        lead_support_ids=("story:1:evidence:0:frag:2002",),
        lead_generation_origin="AI",
        sections=(
            ArticleSection(
                heading="Раздел AI",
                heading_support_ids=("story:1:evidence:0:frag:2002",),
                heading_generation_origin="AI",
                paragraphs=(
                    ArticleParagraph(
                        text="Параграф AI.",
                        cited_support_ids=("story:1:evidence:0:frag:2002",),
                        generation_origin="AI",
                    ),
                ),
            ),
            ArticleSection(
                heading="Коротко о других событиях города",
                heading_support_ids=("story:2:evidence:0:frag:2003",),
                heading_generation_origin="SUPPLEMENT",
                paragraphs=(
                    ArticleParagraph(
                        text="Параграф Supplement.",
                        cited_support_ids=("story:2:evidence:0:frag:2003",),
                        generation_origin="SUPPLEMENT",
                    ),
                ),
            ),
        ),
    )
    trace = build_article_claim_trace(draft, ctx)
    by_unit = {u.unit_id: u for u in trace}
    assert by_unit["TITLE"].generation_origin == "AI"
    assert by_unit["LEAD"].generation_origin == "AI"
    assert by_unit["H001"].generation_origin == "AI"
    assert by_unit["P001"].generation_origin == "AI"
    assert by_unit["H002"].generation_origin == "SUPPLEMENT"
    assert by_unit["P002"].generation_origin == "SUPPLEMENT"
