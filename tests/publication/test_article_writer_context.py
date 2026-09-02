from __future__ import annotations

import datetime as dt

from src.editorial_models import StoryCard
from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    PublicationWindow,
)
from src.publication.article_coverage import build_article_coverage_plan
from src.publication.article_writer_context import (
    render_article_writer_context,
    sanitize_writer_source_text,
)


def test_writer_source_sanitizer_hides_phone_and_url_but_keeps_route_fact():
    raw = "Ежедневные рейсы в Краснодар и Ростов. Бронирование +79900292947, https://bus.example"
    sanitized = sanitize_writer_source_text(raw)
    assert "Краснодар" in sanitized
    assert "Ростов" in sanitized
    assert "+79900292947" not in sanitized
    assert "https://" not in sanitized
    assert "[contact omitted]" in sanitized
    assert "[link omitted]" in sanitized


def test_writer_source_sanitizer_does_not_strip_non_contact_numbers():
    raw = "Жильцы скидываются по 300 рублей, автобус ходит раз в час, скидка на проезд 8 рублей"
    sanitized = sanitize_writer_source_text(raw)
    assert "300" in sanitized
    assert "8" in sanitized


def test_render_article_writer_context_includes_plan_and_sanitizes_sources():
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    raw_source = "Рейсы в Бердянск. Телефон +79901112233"

    card1 = StoryCard(id="story:power", topic="Энергетика", importance="high", summary="Свет")
    card2 = StoryCard(id="story:telecom", topic="Связь", importance="medium", summary="Связь")
    card3 = StoryCard(id="story:sport", topic="Спорт", importance="low", summary="Спорт")

    s1 = ArticleSupport(
        support_id="story:power:evidence:0:frag:1",
        text="Света нет",
        source_text="Света нет нигде",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=now,
        evidence_kind="community_report",
        story_id="story:power",
    )
    s2 = ArticleSupport(
        support_id="story:telecom:evidence:0:frag:2",
        text="Связь плохая",
        source_text="Связь пропала",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-2",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=now,
        evidence_kind="community_report",
        story_id="story:telecom",
    )
    s3 = ArticleSupport(
        support_id="story:telecom:evidence:1:frag:3",
        text="Генератор работает",
        source_text="Генератор 5 кВт",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-3",),
        fragment_ids=(3,),
        source_item_ids=(3,),
        observed_at=now,
        evidence_kind="service_access",
        story_id="story:telecom",
    )
    s4 = ArticleSupport(
        support_id="story:sport:evidence:0:frag:4",
        text="Набор детей",
        source_text=raw_source,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-4",),
        fragment_ids=(4,),
        source_item_ids=(4,),
        observed_at=now,
        evidence_kind="service_access",
        story_id="story:sport",
    )

    ctx = ArticleEditorialContext(
        headline_candidates=("Энергетика", "Связь", "Спорт"),
        support_index=(s1, s2, s3, s4),
        support_by_id={s.support_id: s for s in (s1, s2, s3, s4)},
        recurring_topics=(),
        publication_window=PublicationWindow(
            snapshot_at=now,
            lookback_start=now - dt.timedelta(hours=24),
        ),
        edition_name="Бердянск",
    )
    plan = build_article_coverage_plan([card1, card2, card3], ctx)
    rendered = render_article_writer_context(ctx, plan)

    assert "ARTICLE COVERAGE PLAN" in rendered
    assert "SECTION:" in rendered
    assert "NARRATIVE INTENT:" in rendered
    assert "DEVELOP story:power: Энергетика" in rendered
    assert "WEAVE story:telecom: Связь" in rendered
    assert "BRIEF story:sport: Спорт" in rendered
    assert "DETAIL SUPPORTS:" in rendered
    assert "+79901112233" not in rendered
    assert "[contact omitted]" in rendered

    # Raw support source text is NOT mutated
    assert s4.source_text == raw_source
