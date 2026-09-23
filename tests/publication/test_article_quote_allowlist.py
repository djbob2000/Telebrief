from __future__ import annotations

import datetime as dt

from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_material import project_article_material
from src.publication.article_quote_allowlist import build_article_quote_allowlist


def test_quote_allowlist_accepts_source_detail_retained_in_material_projection():
    support = ArticleSupport(
        support_id="story:generator:evidence:0:frag:1",
        text="Жильцы подключили генератор.",
        source_text="Жильцы подключили генератор. Wi-Fi появился в доме.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:generator",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=dt.datetime(2026, 9, 22, 18, 0, tzinfo=dt.timezone.utc),
        evidence_kind="community_report",
        story_id="story:generator",
    )
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
    )
    projection = project_article_material(context)
    assert "Wi-Fi появился в доме." in projection.text_by_support_id[support.support_id]

    allowlist = build_article_quote_allowlist(
        context,
        candidate_text_by_support_id=projection.text_by_support_id,
    )

    assert "Wi-Fi появился в доме." in allowlist


def test_quote_allowlist_keeps_suppressed_story_ids_out_even_if_action_map_disagrees():
    support = ArticleSupport(
        support_id="story:ad:evidence:0:frag:2",
        text="Объявление о продаже товара.",
        source_text="Объявление о продаже товара.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref:ad",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=dt.datetime(2026, 9, 22, 18, 0, tzinfo=dt.timezone.utc),
        evidence_kind="community_report",
        story_id="story:ad",
    )
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
    )

    allowlist = build_article_quote_allowlist(context, excluded_story_ids=("story:ad",))

    assert not allowlist
