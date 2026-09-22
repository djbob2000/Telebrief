from __future__ import annotations

import datetime as dt

import pytest

from src.editorial_models import StoryCard
from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    PublicationWindow,
)
from src.publication.article_coverage import build_article_coverage_plan
from src.publication.article_material import project_article_material
from src.publication.article_quote_allowlist import build_article_quote_allowlist
from src.publication.article_writer_context import (
    ARTICLE_WRITER_CONTEXT_MAX_CHARS,
    _render_article_story_packets,
    format_article_context_time,
    render_article_writer_context,
    render_article_writer_context_with_stats,
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


def test_compact_packet_uses_edition_local_as_of_and_support_times():
    snapshot = dt.datetime(2026, 9, 22, 18, 2, tzinfo=dt.timezone.utc)
    observed = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    support = ArticleSupport(
        support_id="story:water:evidence:1:frag:7",
        text="Жители сообщили об ограничении подачи воды.",
        source_text="На улице Садовой подача воды ограничена.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-7",),
        fragment_ids=(7,),
        source_item_ids=(7,),
        observed_at=observed,
        effective_from=dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc),
        effective_until=dt.datetime(2026, 9, 22, 19, 0, tzinfo=dt.timezone.utc),
        temporal_role="FUTURE_SCHEDULED",
        evidence_kind="service_access",
        story_id="story:water",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Подача воды",),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
        publication_window=PublicationWindow(
            snapshot_at=snapshot,
            lookback_start=snapshot - dt.timedelta(hours=24),
        ),
        edition_name="Test edition",
        edition_timezone="Europe/Kyiv",
    )
    card = StoryCard(
        id="story:water", topic="Подача воды", summary="Подача воды", importance="high"
    )
    plan = build_article_coverage_plan((card,), context)

    rendered = render_article_writer_context(context, plan, include_coverage_plan=False)
    full_packets, compact_packets, _stats = _render_article_story_packets(context, plan)

    assert "PUBLICATION AS OF: 2026-09-22 21:02 (Europe/Kyiv)" in rendered
    for packet in (*full_packets, *compact_packets):
        assert "role=FUTURE_SCHEDULED" in packet
        assert "observed_at=2026-09-22 20:00 (Europe/Kyiv)" in packet
        assert "effective_from=2026-09-22 21:15 (Europe/Kyiv)" in packet
        assert "effective_until=2026-09-22 22:00 (Europe/Kyiv)" in packet


def test_format_article_context_time_rejects_invalid_timezone():
    with pytest.raises(ValueError):
        format_article_context_time(
            dt.datetime(2026, 9, 22, 18, 2, tzinfo=dt.timezone.utc),
            "Invalid/EditionZone",
        )


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


def test_render_article_writer_context_uses_material_projection_for_packets_and_no_plan():
    ad = ArticleSupport(
        support_id="story:ad:evidence:0:frag:1",
        text="Продаётся квартира. Цена 100 рублей, звоните +79900000000.",
        source_text="Продаётся квартира. Цена 100 рублей, звоните +79900000000.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-ad",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        evidence_kind="community_report",
        story_id="story:ad",
    )
    report = ArticleSupport(
        support_id="story:outage:evidence:0:frag:2",
        text="На улице Садовой нет света.",
        source_text="На улице Садовой нет света.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-outage",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=None,
        evidence_kind="community_report",
        story_id="story:outage",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Квартира", "Электричество"),
        support_index=(ad, report),
        support_by_id={ad.support_id: ad, report.support_id: report},
        recurring_topics=(),
        edition_name="Бердянск",
    )
    cards = (
        StoryCard(id="story:ad", topic="Квартира", importance="low", summary="Квартира"),
        StoryCard(id="story:outage", topic="Электричество", importance="high", summary="Свет"),
    )
    plan = build_article_coverage_plan(cards, context)
    projection = project_article_material(context)

    packet_context = render_article_writer_context(
        context, plan, include_coverage_plan=False, material_projection=projection
    )
    no_plan_context = render_article_writer_context(context, material_projection=projection)

    for rendered in (packet_context, no_plan_context):
        assert "+79900000000" not in rendered
        assert "story:ad" not in rendered
        assert "На улице Садовой нет света" in rendered


def test_projected_writer_quote_allowlist_excludes_suppressed_promotion_support():
    support = ArticleSupport(
        support_id="story:ad:evidence:0:frag:1",
        text="Продаётся квартира по цене 100 рублей.",
        source_text="Продаётся квартира по цене 100 рублей.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-ad",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        evidence_kind="community_report",
        story_id="story:ad",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Квартира",),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
    )
    projection = project_article_material(context)

    assert build_article_quote_allowlist(context) == (support.text,)
    rendered = render_article_writer_context(context, material_projection=projection)
    assert support.text not in rendered


def test_projected_writer_quote_allowlist_uses_trimmed_support_text():
    support = ArticleSupport(
        support_id="story:enrollment:evidence:0:frag:1",
        text="Спортивная школа открыла бесплатную запись детей.",
        source_text="Спортивная школа открыла бесплатную запись детей. Обращайтесь для записи.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-enrollment",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        evidence_kind="community_report",
        story_id="story:enrollment",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Спорт",),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
    )
    projection = project_article_material(context)

    assert projection.actions_by_support_id[support.support_id] == "TRIM_DIRECTORY"
    assert build_article_quote_allowlist(context) == (support.text,)
    assert build_article_quote_allowlist(
        context,
        candidate_text_by_support_id=projection.text_by_support_id,
    ) == (support.text,)
    rendered = render_article_writer_context(context, material_projection=projection)
    assert "Спортивная школа открыла бесплатную запись детей." in rendered
    assert "Обращайтесь для записи." not in rendered


def test_render_article_writer_context_includes_material_inventory_for_story_packets():
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    cards = [
        StoryCard(id="story:power", topic="Энергетика", importance="high", summary="Свет"),
        StoryCard(id="story:water", topic="Вода", importance="medium", summary="Вода"),
    ]
    supports = tuple(
        ArticleSupport(
            support_id=f"{card.id}:evidence:0:frag:{index}",
            text=f"Подтверждённый факт для {card.topic}.",
            source_text=f"Сообщение жителей о теме {card.topic}.",
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"ref-{index}",),
            fragment_ids=(index,),
            source_item_ids=(index,),
            observed_at=now,
            evidence_kind="community_report",
            story_id=card.id,
        )
        for index, card in enumerate(cards, start=1)
    )
    context = ArticleEditorialContext(
        headline_candidates=tuple(card.topic for card in cards),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
        edition_name="Бердянск",
    )
    plan = build_article_coverage_plan(cards, context)

    rendered = render_article_writer_context(context, plan, include_coverage_plan=False)

    assert "ARTICLE MATERIAL INVENTORY" in rendered
    assert "coverage stories: 2" in rendered
    assert "story packets: 2" in rendered
    assert "packets with citable support: 2" in rendered


def test_render_article_writer_context_with_stats_returns_materialization_counts():
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    card = StoryCard(id="story:power", topic="Энергетика", importance="high", summary="Свет")
    support = ArticleSupport(
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
    context = ArticleEditorialContext(
        headline_candidates=("Энергетика",),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
        publication_window=PublicationWindow(snapshot_at=now, lookback_start=now),
        edition_name="Бердянск",
    )
    plan = build_article_coverage_plan([card], context)

    rendered, stats = render_article_writer_context_with_stats(
        context, plan, include_coverage_plan=False
    )

    assert "ARTICLE MATERIAL INVENTORY" in rendered
    assert stats.to_metadata() == {
        "coverage_story_count": 1,
        "story_packet_count": 1,
        "bundle_count": 0,
        "packets_with_citable_support": 1,
        "citable_support_count": 1,
    }


def test_render_article_writer_context_is_bounded_and_deduplicates_repeated_support():
    repeated_fact = "Жители сообщили о длительном отключении света в нескольких домах."
    repeated_source = (
        "Источник сообщает о длительном отключении света в нескольких домах. "
        "Подробности наблюдения повторяются в связанных сообщениях. " * 20
    )
    supports = tuple(
        ArticleSupport(
            support_id=f"support-{index}",
            text=repeated_fact,
            source_text=repeated_source,
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"ref-{index}",),
            fragment_ids=(index,),
            source_item_ids=(index,),
            observed_at=None,
            evidence_kind="community_report",
            story_id=f"story-{index}",
        )
        for index in range(100)
    )
    ctx = ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
        publication_window=None,
        edition_name="Бердянск",
    )

    rendered = render_article_writer_context(ctx)

    assert len(rendered) <= ARTICLE_WRITER_CONTEXT_MAX_CHARS
    assert rendered.count(repeated_fact) < len(supports)
    assert "support-99" in rendered


def test_render_article_writer_context_compacts_large_corpus_to_writer_budget():
    supports = tuple(
        ArticleSupport(
            support_id=f"support-{index}",
            text=(
                f"Сюжет {index}: жители сообщили о конкретном городском событии, "
                "его месте, времени и практических последствиях для жителей."
            ),
            source_text=(
                f"Источник для сюжета {index}. "
                "Подробное первичное сообщение с повторяющимися пояснениями. " * 20
            ),
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"ref-{index}",),
            fragment_ids=(index,),
            source_item_ids=(index,),
            observed_at=None,
            evidence_kind="community_report",
            story_id=f"story-{index}",
        )
        for index in range(1000)
    )
    ctx = ArticleEditorialContext(
        headline_candidates=(),
        support_index=supports,
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
        edition_name="Бердянск",
    )

    rendered = render_article_writer_context(ctx)

    assert len(rendered) <= 320_000
