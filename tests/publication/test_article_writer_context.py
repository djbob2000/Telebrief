from __future__ import annotations

import datetime as dt

import pytest

import src.publication.article_writer_context as writer_context
from src.editorial_models import StoryCard
from src.publication.article_composition import build_article_composition_plan
from src.publication.article_context import (
    ArticleEditorialContext,
    ArticleSupport,
    PublicationWindow,
)
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleStoryAssignment,
    ArticleStoryCoverage,
    ArticleThematicSection,
    build_article_coverage_plan,
)
from src.publication.article_material import ArticleMaterialProjection, project_article_material
from src.publication.article_quote_allowlist import build_article_quote_allowlist
from src.publication.article_writer_context import (
    ARTICLE_WRITER_CONTEXT_MAX_CHARS,
    _render_article_story_packets,
    _render_composition_plan,
    format_article_context_time,
    render_article_writer_context,
    render_article_writer_context_with_stats,
    sanitize_writer_source_text,
)
from src.publication.story_threads import (
    StoryThread,
    ThreadEditorialWeight,
    TrajectoryKind,
    build_longitudinal_coverage_plan,
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


def test_develop_story_packet_contains_every_planned_support_above_depth_detail_limit():
    support_count = 6
    supports = tuple(
        ArticleSupport(
            support_id=f"story:power:evidence:{index}:frag:{index}",
            text=f"Электроснабжение восстановилось на улице Садовой, дом {index}.",
            source_text=f"Электроснабжение восстановилось на улице Садовой, дом {index}.",
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"ref-{index}",),
            fragment_ids=(index,),
            source_item_ids=(index,),
            observed_at=None,
            evidence_kind="community_report",
            story_id="story:power",
        )
        for index in range(1, support_count + 1)
    )
    context = ArticleEditorialContext(
        headline_candidates=("Электроснабжение",),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:power",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=tuple(support.support_id for support in supports),
            ),
        )
    )

    packets, compact_packets, stats = _render_article_story_packets(context, plan)

    assert len(packets) == 1
    assert len(compact_packets) == 1
    for support in supports:
        assert packets[0].count(f"support={support.support_id}") == 1
        assert packets[0].count(support.text) == 1
    assert stats.citable_support_count == support_count


def test_compact_story_packets_preserve_support_framing_facts_and_all_time_fields(monkeypatch):
    observed = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)
    effective_from = dt.datetime(2026, 9, 22, 18, 15, tzinfo=dt.timezone.utc)
    effective_until = dt.datetime(2026, 9, 22, 19, 0, tzinfo=dt.timezone.utc)
    supports = tuple(
        ArticleSupport(
            support_id=f"story:water:evidence:{index}:frag:{index}",
            text=f"Жители сообщили об ограничении воды на улице Садовой, дом {index}.",
            source_text=f"Жители сообщили об ограничении воды на улице Садовой, дом {index}.",
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"ref-{index}",),
            fragment_ids=(index,),
            source_item_ids=(index,),
            observed_at=observed,
            effective_from=effective_from,
            effective_until=effective_until,
            temporal_role="FUTURE_SCHEDULED",
            evidence_kind="community_report",
            story_id="story:water",
        )
        for index in range(1, 4)
    )
    context = ArticleEditorialContext(
        headline_candidates=("Вода",),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
        edition_timezone="Europe/Kyiv",
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:water",
                topic="Вода",
                rank=1,
                prominence="DEVELOP",
                support_ids=tuple(support.support_id for support in supports),
            ),
        )
    )
    full_packets, compact_packets, _stats = _render_article_story_packets(context, plan)
    prefix = "inventory"
    compact_body = "\n\n".join(compact_packets)
    assert len("\n\n".join(full_packets)) > len(compact_body)
    monkeypatch.setattr(
        writer_context,
        "ARTICLE_WRITER_CONTEXT_MAX_CHARS",
        len(prefix) + len(compact_body),
    )

    rendered, representation = writer_context._fit_story_packets(
        prefix, full_packets, compact_packets
    )

    assert representation == "compact"
    for support in supports:
        assert rendered.count(support.support_id) == 1
        assert rendered.count(support.text) == 1
    assert "framing=attributed_report" in rendered
    assert "role=FUTURE_SCHEDULED" in rendered
    assert "observed_at=2026-09-22 20:00 (Europe/Kyiv)" in rendered
    assert "effective_from=2026-09-22 21:15 (Europe/Kyiv)" in rendered
    assert "effective_until=2026-09-22 22:00 (Europe/Kyiv)" in rendered


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
    assert "SECTION:" not in rendered
    assert "NARRATIVE INTENT:" not in rendered
    assert "DEVELOP story:power: Энергетика" in rendered
    assert "WEAVE story:telecom: Связь" in rendered
    assert "BRIEF story:sport: Спорт" in rendered
    assert "DETAIL SUPPORTS:" in rendered
    assert "Света нет" in rendered
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


def test_story_packet_skips_empty_cta_support_without_losing_service_access_fact():
    fact = ArticleSupport(
        support_id="story:water-delivery:evidence:0:frag:1",
        text="Пункт подвоза воды на ул. Восточной открыт до 20:00.",
        source_text="Пункт подвоза воды на ул. Восточной открыт до 20:00.",
        support_kind="operational",
        publication_use="PUBLISH",
        source_refs=("ref-fact",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        evidence_kind="service_access",
        story_id="story:water-delivery",
    )
    cta = ArticleSupport(
        support_id="story:water-delivery:evidence:1:frag:2",
        text="Звоните диспетчеру по номеру +79900000000, подробности по ссылке.",
        source_text="Звоните диспетчеру по номеру +79900000000, подробности по ссылке.",
        support_kind="operational",
        publication_use="PUBLISH",
        source_refs=("ref-cta",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=None,
        evidence_kind="service_access",
        story_id="story:water-delivery",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Пункт подвоза воды",),
        support_index=(fact, cta),
        support_by_id={fact.support_id: fact, cta.support_id: cta},
        recurring_topics=(),
    )
    coverage = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:water-delivery",
                topic="Пункт подвоза воды",
                rank=1,
                prominence="WEAVE",
                support_ids=(fact.support_id, cta.support_id),
                detail_support_ids=(fact.support_id, cta.support_id),
            ),
        )
    )
    projection = project_article_material(context)
    composition = build_article_composition_plan(coverage, context, projection)

    rendered, stats = render_article_writer_context_with_stats(
        context,
        coverage,
        material_projection=projection,
        composition_plan=composition,
    )

    assert "подвоза воды" in rendered
    assert "ул. Восточной" in rendered
    assert "открыт до 20:00" in rendered
    assert "звоните" not in rendered.casefold()
    assert "+79900000000" not in rendered
    assert "подробности по ссылке" not in rendered.casefold()
    assert stats is not None and stats.citable_support_count == 1


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
        "narrative_line_count": 0,
        "composition_group_count": 0,
        "group_size_distribution": {},
        "rendered_packet_representation": "full",
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


def test_composition_roadmap_and_story_packets_do_not_repeat_the_inventory():
    now = dt.datetime(2026, 9, 22, 18, 2, tzinfo=dt.timezone.utc)
    specs = (
        (
            "story:water-sadovaya",
            "Вода на Садовой",
            "На улице Садовой воду восстановили.",
            "На улице Садовой воду восстановили.",
        ),
        (
            "story:water-morskaya",
            "Вода на Морской",
            "На улице Морской воды нет.",
            "На улице Морской воды нет.",
        ),
        (
            "story:short-report",
            "Состояние двора",
            "Во дворе темно.",
            "Во дворе темно.",
        ),
        (
            "story:children-enrollment",
            "Бесплатная запись детей",
            "Спортивная школа открыла бесплатную запись детей для района.",
            "Спортивная школа открыла бесплатную запись детей для района. "
            "Телефон +79901112233, подробности https://example.test/enroll",
        ),
    )
    cards = tuple(
        StoryCard(id=story_id, topic=topic, summary=topic, importance="low")
        for story_id, topic, _fact, _source in specs
    )
    supports = tuple(
        ArticleSupport(
            support_id=f"{story_id}:evidence:0:frag:{index}",
            text=fact,
            source_text=source,
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"ref-{index}",),
            fragment_ids=(index,),
            source_item_ids=(index,),
            observed_at=now,
            evidence_kind="community_report",
            story_id=story_id,
        )
        for index, (story_id, _topic, fact, source) in enumerate(specs, start=1)
    )
    context = ArticleEditorialContext(
        headline_candidates=tuple(card.topic for card in cards),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
        edition_timezone="Europe/Kyiv",
    )
    plan = build_article_coverage_plan(cards, context)
    projection = project_article_material(context)
    composition = build_article_composition_plan(plan, context, projection)

    rendered, stats = render_article_writer_context_with_stats(
        context,
        plan,
        material_projection=projection,
        composition_plan=composition,
    )

    assert rendered.count("ARTICLE COMPOSITION ROADMAP") == 1
    assert rendered.count("ARTICLE MATERIAL INVENTORY") == 1
    assert "relation=localized_contrast" in rendered
    assert "story:water-sadovaya[BRIEF]" not in rendered
    assert "story:water-morskaya[BRIEF]" not in rendered
    assert rendered.count("[ARTICLE STORY PACKET story:") == len(specs)
    assert rendered.count("line=") == len(specs)
    assert rendered.count("group=") == len(specs)
    assert "[ARTICLE COMPOSITION BUNDLE" not in rendered
    assert "members=story:" not in rendered
    assert "SUPPORTS:" not in rendered
    for support in supports:
        assert rendered.count(support.support_id) == 1
    assert "fact=Во дворе темно." in rendered
    assert "framing=attributed_report" in rendered
    assert "Спортивная школа открыла бесплатную запись детей для района." in rendered
    assert "+79901112233" not in rendered
    assert "example.test" not in rendered
    assert supports[-1].source_text.endswith("https://example.test/enroll")
    assert stats is not None
    assert stats.narrative_line_count == 3
    assert stats.composition_group_count == 3
    assert stats.group_size_distribution == ((1, 2), (2, 1))
    assert stats.rendered_packet_representation == "full"
    assert "rendered packet representation: full" in rendered


def test_observed_report_time_does_not_stand_in_for_unknown_event_time():
    observed = dt.datetime(2026, 9, 22, 18, 2, tzinfo=dt.timezone.utc)
    support = ArticleSupport(
        support_id="story:water:evidence:0:frag:1",
        text="Жители сообщают, что на улице Садовой нет воды.",
        source_text="На улице Садовой нет воды.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=observed,
        evidence_kind="community_report",
        story_id="story:water",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Нет воды",),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
        edition_timezone="Europe/Kyiv",
    )
    plan = build_article_coverage_plan(
        (StoryCard(id="story:water", topic="Нет воды", summary="Вода", importance="high"),),
        context,
    )
    composition = build_article_composition_plan(plan, context, project_article_material(context))

    rendered = render_article_writer_context(context, plan, composition_plan=composition)

    assert "observed_at=2026-09-22 21:02 (Europe/Kyiv)" in rendered
    assert "effective_from=" not in rendered
    assert "effective_until=" not in rendered
    assert (
        "observed_at describes when a report was made and does not establish an event start"
        in rendered
    )


@pytest.mark.parametrize("edition_timezone", ["Invalid/EditionZone", None])
def test_writer_context_rejects_invalid_or_missing_edition_timezone_without_timestamps(
    edition_timezone,
):
    context = ArticleEditorialContext(
        headline_candidates=(),
        support_index=(),
        support_by_id={},
        recurring_topics=(),
        edition_timezone=edition_timezone,
    )

    with pytest.raises(ValueError, match="Invalid article context timezone"):
        render_article_writer_context(context)


def test_projected_writer_packet_preserves_short_material_source_qualifier():
    support = ArticleSupport(
        support_id="story:water-generator:evidence:0:frag:1",
        text="Житель установил генератор.",
        source_text="Житель установил генератор для подачи воды.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        evidence_kind="community_report",
        story_id="story:water-generator",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Генератор для воды",),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
    )
    card = StoryCard(
        id="story:water-generator",
        topic="Генератор для воды",
        summary="Генератор",
        importance="medium",
    )
    plan = build_article_coverage_plan((card,), context)
    projection = project_article_material(context)
    composition = build_article_composition_plan(plan, context, projection)

    rendered = render_article_writer_context(
        context,
        plan,
        material_projection=projection,
        composition_plan=composition,
    )
    packets, _compact_packets, _stats = _render_article_story_packets(
        context, plan, projection, composition
    )

    assert "Житель установил генератор для подачи воды." in rendered
    assert (
        "fact=Житель установил генератор. Житель установил генератор для подачи воды." in packets[0]
    )
    assert (
        packets[0].count("Житель установил генератор. Житель установил генератор для подачи воды.")
        == 1
    )
    assert support.source_text == "Житель установил генератор для подачи воды."


def test_projected_writer_packet_preserves_source_only_state_negation():
    support = ArticleSupport(
        support_id="story:power:evidence:0:frag:1",
        text="Свет подаётся.",
        source_text="Свет не подаётся.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        evidence_kind="community_report",
        story_id="story:power",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Электроснабжение",),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
    )
    card = StoryCard(
        id="story:power",
        topic="Электроснабжение",
        summary="Электроснабжение",
        importance="medium",
    )
    plan = build_article_coverage_plan((card,), context)
    projection = project_article_material(context)
    composition = build_article_composition_plan(plan, context, projection)

    packets, _compact_packets, _stats = _render_article_story_packets(
        context, plan, projection, composition
    )

    assert projection.text_by_support_id[support.support_id] == "Свет подаётся. Свет не подаётся."
    assert "fact=Свет подаётся. Свет не подаётся." in packets[0]
    assert "Свет подаётся. Свет не подаётся." in packets[0]


@pytest.mark.parametrize(
    ("claim", "source", "qualifier"),
    (
        (
            "На улице Садовой нет света.",
            "На улице Садовой нет света во всех домах.",
            "во всех домах",
        ),
        ("Света нет.", "Света нет до утра.", "до утра"),
        (
            "Житель установил генератор.",
            "Житель установил генератор для подачи воды.",
            "для подачи воды",
        ),
        ("Свет подаётся.", "Свет не подаётся.", "не подаётся"),
    ),
)
def test_projected_material_keeps_source_only_scope_time_purpose_and_polarity(
    claim: str, source: str, qualifier: str
):
    support = ArticleSupport(
        support_id="story:local:evidence:0:frag:1",
        text=claim,
        source_text=source,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        evidence_kind="community_report",
        story_id="story:local",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Городская история",),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
    )

    projection = project_article_material(context)

    projected = projection.text_by_support_id[support.support_id]
    assert qualifier in projected
    assert source in projected


def test_story_packet_fails_closed_when_planned_support_is_missing_from_support_map():
    support = ArticleSupport(
        support_id="story:power:evidence:0:frag:1",
        text="На улице Садовой нет света.",
        source_text="На улице Садовой нет света.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        evidence_kind="community_report",
        story_id="story:power",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Электроснабжение",),
        support_index=(support,),
        support_by_id={},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:power",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(support.support_id,),
            ),
        )
    )

    with pytest.raises(ValueError, match="references missing support"):
        _render_article_story_packets(context, plan)


def test_story_packet_fails_closed_when_planned_support_has_no_projected_text():
    support = ArticleSupport(
        support_id="story:power:evidence:0:frag:1",
        text="На улице Садовой нет света.",
        source_text="На улице Садовой нет света.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        evidence_kind="community_report",
        story_id="story:power",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Электроснабжение",),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:power",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(support.support_id,),
            ),
        )
    )
    projection = ArticleMaterialProjection(
        text_by_support_id={support.support_id: ""},
        actions_by_support_id={support.support_id: "KEEP"},
        reasons_by_support_id={support.support_id: "supported_material_retained"},
    )

    with pytest.raises(ValueError, match="has no projected citable text"):
        _render_article_story_packets(context, plan, projection)


def test_story_packet_fails_closed_when_support_id_and_owner_conflict():
    support = ArticleSupport(
        support_id="story:power:evidence:0:frag:1",
        text="На улице Садовой нет света.",
        source_text="На улице Садовой нет света.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=None,
        evidence_kind="community_report",
        story_id="story:water",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Электроснабжение", "Водоснабжение"),
        support_index=(support,),
        support_by_id={support.support_id: support},
        recurring_topics=(),
    )
    plan = ArticleCoveragePlan(
        stories=(
            ArticleStoryCoverage(
                story_id="story:power",
                topic="Электроснабжение",
                rank=1,
                prominence="DEVELOP",
                support_ids=(support.support_id,),
            ),
            ArticleStoryCoverage(
                story_id="story:water",
                topic="Водоснабжение",
                rank=2,
                prominence="BRIEF",
                support_ids=(),
            ),
        )
    )

    with pytest.raises(ValueError, match="encodes owner 'story:power'.*belongs to 'story:water'"):
        _render_article_story_packets(context, plan)


def test_composition_plan_renders_each_member_owned_support_line():
    now = dt.datetime(2026, 9, 22, 18, 2, tzinfo=dt.timezone.utc)
    cards = (
        StoryCard(id="story:water-a", topic="Вода на Садовой", summary="Вода", importance="medium"),
        StoryCard(id="story:power", topic="Свет на Садовой", summary="Свет", importance="medium"),
        StoryCard(id="story:water-b", topic="Вода на Морской", summary="Вода", importance="medium"),
    )
    supports = tuple(
        ArticleSupport(
            support_id=support_id,
            text=text,
            source_text=text,
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(support_id,),
            fragment_ids=(index,),
            source_item_ids=(index,),
            observed_at=now,
            evidence_kind="community_report",
            story_id=story_id,
        )
        for index, (story_id, support_id, text) in enumerate(
            (
                (
                    "story:water-a",
                    "story:water-a:evidence:0:frag:1",
                    "На улице Садовой воду восстановили.",
                ),
                (
                    "story:power",
                    "story:power:evidence:0:frag:2",
                    "На улице Садовой света нет.",
                ),
                (
                    "story:water-b",
                    "story:water-b:evidence:0:frag:3",
                    "На улице Морской подача воды ограничена.",
                ),
            ),
            start=1,
        )
    )
    context = ArticleEditorialContext(
        headline_candidates=tuple(card.topic for card in cards),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
    )
    plan = build_article_coverage_plan(cards, context)
    composition = build_article_composition_plan(plan, context, project_article_material(context))

    rendered_plan = _render_composition_plan(plan, composition, context=context)

    assert "ARTICLE COMPOSITION ROADMAP" in rendered_plan
    assert "relation=localized_contrast" in rendered_plan
    assert "story:water-a" not in rendered_plan
    assert "story:power" not in rendered_plan
    assert "story:water-b" not in rendered_plan
    assert "LINE line:" in rendered_plan
    assert "SUPPORTS:" not in rendered_plan


def test_interleaved_story_packets_repeat_bundle_context_for_each_member():
    now = dt.datetime(2026, 9, 22, 18, 2, tzinfo=dt.timezone.utc)
    cards = (
        StoryCard(id="story:water-a", topic="Вода на Садовой", summary="Вода", importance="medium"),
        StoryCard(id="story:power", topic="Свет на Садовой", summary="Свет", importance="medium"),
        StoryCard(id="story:water-b", topic="Вода на Морской", summary="Вода", importance="medium"),
    )
    supports = tuple(
        ArticleSupport(
            support_id=f"{story_id}:evidence:0:frag:{index}",
            text=text,
            source_text=text,
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"ref-{index}",),
            fragment_ids=(index,),
            source_item_ids=(index,),
            observed_at=now,
            evidence_kind="community_report",
            story_id=story_id,
        )
        for index, (story_id, text) in enumerate(
            (
                ("story:water-a", "На улице Садовой воду восстановили."),
                ("story:power", "На улице Садовой света нет."),
                ("story:water-b", "На улице Морской подача воды ограничена."),
            ),
            start=1,
        )
    )
    context = ArticleEditorialContext(
        headline_candidates=tuple(card.topic for card in cards),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
    )
    coverages = tuple(
        ArticleStoryCoverage(
            story_id=card.id,
            topic=card.topic,
            rank=index,
            prominence="BRIEF",
            support_ids=(support.support_id,),
        )
        for index, (card, support) in enumerate(zip(cards, supports), start=1)
    )
    assignments = tuple(
        ArticleStoryAssignment(
            story_id=coverage.story_id,
            section_id="infrastructure",
            depth=coverage.prominence,
            rank=coverage.rank,
            primary_evidence_ids=coverage.support_ids,
        )
        for coverage in coverages
    )
    plan = ArticleCoveragePlan(
        stories=coverages,
        sections=(
            ArticleThematicSection(
                section_id="infrastructure",
                title="Коммунальная обстановка",
                lead_story_id=coverages[0].story_id,
                story_assignments=assignments,
                narrative_intent="Состояние городских сетей.",
            ),
        ),
    )
    composition = build_article_composition_plan(plan, context, project_article_material(context))

    packets, _compact_packets, _stats = _render_article_story_packets(
        context, plan, composition_plan=composition
    )

    packets_by_story = {
        next(
            line for line in packet.splitlines() if line.startswith("[ARTICLE STORY PACKET")
        ): packet
        for packet in packets
    }
    group_by_story = composition.group_by_story_id
    water_a = packets_by_story[
        f"[ARTICLE STORY PACKET story:water-a] depth=BRIEF "
        f"line={group_by_story['story:water-a'].narrative_line_id} "
        f"group={group_by_story['story:water-a'].group_id} topic=Вода на Садовой"
    ]
    power = packets_by_story[
        f"[ARTICLE STORY PACKET story:power] depth=BRIEF "
        f"line={group_by_story['story:power'].narrative_line_id} "
        f"group={group_by_story['story:power'].group_id} topic=Свет на Садовой"
    ]
    water_b = packets_by_story[
        f"[ARTICLE STORY PACKET story:water-b] depth=BRIEF "
        f"line={group_by_story['story:water-b'].narrative_line_id} "
        f"group={group_by_story['story:water-b'].group_id} topic=Вода на Морской"
    ]

    assert "[ARTICLE COMPOSITION BUNDLE" not in "\n".join(packets)
    assert "members=" not in "\n".join(packets)
    assert "support=story:water-a:evidence:0:frag:1" in water_a
    assert "support=story:water-b:evidence:0:frag:3" in water_b
    assert "support=story:power:evidence:0:frag:2" in power


def test_longitudinal_pooled_supports_stay_with_their_own_story_packet():
    now = dt.datetime(2026, 9, 22, 18, 2, tzinfo=dt.timezone.utc)
    support_a = ArticleSupport(
        support_id="story:water-a:evidence:0:frag:1",
        text="На Садовой воду восстановили.",
        source_text="На Садовой воду восстановили.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-a",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=now,
        evidence_kind="community_report",
        story_id="story:water-a",
    )
    support_b = ArticleSupport(
        support_id="story:water-b:evidence:0:frag:2",
        text="На Морской вода остаётся ограниченной.",
        source_text="На Морской вода остаётся ограниченной.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-b",),
        fragment_ids=(2,),
        source_item_ids=(2,),
        observed_at=now,
        evidence_kind="community_report",
        story_id="story:water-b",
    )
    support_context = ArticleSupport(
        support_id="story:water-a:context:0:frag:3",
        text="Жители спрашивали, когда дадут воду.",
        source_text="Когда дадут воду?",
        support_kind="evidence",
        publication_use="CONTEXT",
        source_refs=("ref-context",),
        fragment_ids=(3,),
        source_item_ids=(3,),
        observed_at=now,
        evidence_kind="community_report",
        story_id="story:water-a",
    )
    context = ArticleEditorialContext(
        headline_candidates=("Вода на Садовой", "Вода на Морской"),
        support_index=(support_a, support_b, support_context),
        support_by_id={
            support_a.support_id: support_a,
            support_b.support_id: support_b,
            support_context.support_id: support_context,
        },
        recurring_topics=(),
    )
    thread = StoryThread(
        id="thread:water",
        title="Подача воды",
        rubric="ЖКХ",
        story_ids=("story:water-a", "story:water-b"),
        trajectory=TrajectoryKind.ACUTE_PIVOTAL,
        weight=ThreadEditorialWeight.WEAVE_THREAD,
        support_ids=(support_a.support_id, support_b.support_id, support_context.support_id),
    )
    coverage_plan = build_longitudinal_coverage_plan((thread,))
    composition = build_article_composition_plan(
        coverage_plan, context, project_article_material(context)
    )

    packets, _compact_packets, _stats = _render_article_story_packets(
        context, coverage_plan, composition_plan=composition
    )
    packets_by_story = {
        next(
            line for line in packet.splitlines() if line.startswith("[ARTICLE STORY PACKET")
        ): packet
        for packet in packets
    }
    packet_a_header = next(
        header
        for header in packets_by_story
        if header.startswith("[ARTICLE STORY PACKET story:water-a]")
    )
    packet_b_header = next(
        header
        for header in packets_by_story
        if header.startswith("[ARTICLE STORY PACKET story:water-b]")
    )
    packet_a = packets_by_story[packet_a_header]
    packet_b = packets_by_story[packet_b_header]

    assert support_a.support_id in packet_a
    assert support_a.support_id not in packet_b
    assert support_b.support_id in packet_b
    assert support_b.support_id not in packet_a
    assert support_context.support_id not in packet_a
    assert support_context.text not in packet_a
    assert support_context.source_text not in packet_a
