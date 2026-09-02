from __future__ import annotations

import datetime as dt

from src.article_generator import ArticleGenerator
from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
)
from src.publication.article_quote_allowlist import build_article_quote_allowlist
from src.publication.article_writer_context import render_article_writer_context

_NOW = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)


def test_build_article_quote_allowlist_filters_non_verbatim_and_unsafe_supports():
    """Test 8A: build_article_quote_allowlist only admits verbatim PUBLISH non-question sanitized text."""
    s_valid = ArticleSupport(
        support_id="story:1:evidence:0:frag:1",
        text="Вода отсутствует с 9 утра",
        source_text="Вода отсутствует с 9 утра по всей улице",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=_NOW,
        evidence_kind="community_report",
        story_id="story:1",
    )
    s_not_substring = ArticleSupport(
        support_id="story:1:evidence:1:frag:2",
        text="Вода отсутствует",
        source_text="Воды нет нигде",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=_NOW,
        evidence_kind="community_report",
        story_id="story:1",
    )
    s_context = ArticleSupport(
        support_id="story:1:evidence:2:frag:3",
        text="Наблюдается слабый напор",
        source_text="Наблюдается слабый напор в кранах",
        support_kind="evidence",
        publication_use="CONTEXT",
        source_refs=("ref-3",),
        fragment_ids=(3,),
        source_item_ids=(3,),
        observed_at=_NOW,
        evidence_kind="community_report",
        story_id="story:1",
    )
    s_question = ArticleSupport(
        support_id="story:1:evidence:3:frag:4",
        text="Работает ли банк",
        source_text="Работает ли банк на Ленина?",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-4",),
        fragment_ids=(4,),
        source_item_ids=(4,),
        observed_at=_NOW,
        evidence_kind="resident_question",
        story_id="story:1",
    )
    s_sanitized = ArticleSupport(
        support_id="story:1:evidence:4:frag:5",
        text="Звоните +79901112233 для справок",
        source_text="Звоните +79901112233 для справок",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-5",),
        fragment_ids=(5,),
        source_item_ids=(5,),
        observed_at=_NOW,
        evidence_kind="established_fact",
        story_id="story:1",
    )
    s_op = ArticleSupport(
        support_id="op:water:availability",
        text="Водоснабжение: отсутствует",
        source_text="Водоснабжение: отсутствует",
        support_kind="operational",
        publication_use="PUBLISH",
        source_refs=("ref-6",),
        fragment_ids=(6,),
        source_item_ids=(6,),
        observed_at=_NOW,
        evidence_kind="operational_observation",
        story_id="story:1",
    )

    ctx = ArticleEditorialContext(
        headline_candidates=("Водоснабжение",),
        support_index=(s_valid, s_not_substring, s_context, s_question, s_sanitized, s_op),
        support_by_id={
            s.support_id: s
            for s in (s_valid, s_not_substring, s_context, s_question, s_sanitized, s_op)
        },
        recurring_topics=(),
    )

    allowlist = build_article_quote_allowlist(ctx)
    assert allowlist == ("Вода отсутствует с 9 утра",)

    # Rendered in writer context
    writer_ctx = render_article_writer_context(ctx)
    assert "QUOTE ALLOWLIST" in writer_ctx
    assert "«Вода отсутствует с 9 утра»" in writer_ctx


def test_article_writer_prompt_mandates_quote_allowlist_and_indirect_speech():
    """Test 8B: System prompt strictly prohibits quotation marks outside allowlist."""
    generator = ArticleGenerator.__new__(ArticleGenerator)
    generator.output_language = "Russian"
    prompt = generator._build_event_article_system_prompt()

    assert "QUOTE ALLOWLIST" in prompt or "Quote Allowlist" in prompt
    assert "косвенн" in prompt.lower()
    assert "«...»" in prompt or '"..."' in prompt
