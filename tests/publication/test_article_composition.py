from __future__ import annotations

import datetime as dt

import pytest

import src.publication.article_writer_context as writer_context
from src.editorial_models import StoryCard
from src.publication.article_composition import build_article_composition_plan
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleStoryAssignment,
    ArticleStoryCoverage,
    ArticleThematicSection,
    build_article_coverage_plan,
)
from src.publication.article_material import project_article_material
from src.publication.article_writer_context import (
    _fit_story_packets,
    render_article_writer_context,
)


def _support(
    story_id: str,
    support_id: str,
    text: str,
    *,
    effective_from: dt.datetime | None = None,
    effective_until: dt.datetime | None = None,
) -> ArticleSupport:
    now = dt.datetime(2026, 9, 22, 18, 2, tzinfo=dt.timezone.utc)
    return ArticleSupport(
        support_id=support_id,
        text=text,
        source_text=text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=(f"ref:{story_id}",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=now,
        effective_from=effective_from,
        effective_until=effective_until,
        evidence_kind="community_report",
        story_id=story_id,
    )


def _context(
    cards: tuple[StoryCard, ...], supports: tuple[ArticleSupport, ...], *, edition_slug: str = ""
) -> ArticleEditorialContext:
    return ArticleEditorialContext(
        headline_candidates=tuple(card.topic for card in cards),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
        edition_slug=edition_slug,
    )


def _manual_plan(
    cards: tuple[StoryCard, ...], supports: tuple[ArticleSupport, ...]
) -> ArticleCoveragePlan:
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
    return ArticleCoveragePlan(
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


def test_composition_bundle_groups_related_reports_without_merging_local_evidence():
    cards = (
        StoryCard(id="story:water-a", topic="Вода на Садовой", summary="Вода", importance="medium"),
        StoryCard(
            id="story:power", topic="Электричество на Садовой", summary="Свет", importance="medium"
        ),
        StoryCard(id="story:water-b", topic="Вода на Морской", summary="Вода", importance="medium"),
    )
    supports = (
        _support(
            "story:water-a",
            "story:water-a:evidence:0:frag:1",
            "На улице Садовой воду восстановили.",
        ),
        _support(
            "story:power",
            "story:power:evidence:0:frag:3",
            "На улице Садовой электричество подаётся.",
        ),
        _support(
            "story:water-b",
            "story:water-b:evidence:0:frag:2",
            "На улице Морской подача воды остаётся ограниченной.",
        ),
    )
    context = _context(cards, supports)
    plan = _manual_plan(cards, supports)

    composition = build_article_composition_plan(plan, context, project_article_material(context))

    water_group = next(group for group in composition.groups if "story:water-a" in group.story_ids)
    assert set(water_group.story_ids) == {"story:water-a", "story:water-b"}
    assert water_group.relation == "localized_contrast"
    assert {member.story_id: member.support_ids for member in water_group.members} == {
        "story:water-a": ("story:water-a:evidence:0:frag:1",),
        "story:water-b": ("story:water-b:evidence:0:frag:2",),
    }
    power_group = composition.group_by_story_id["story:power"]
    assert power_group.group_id != water_group.group_id
    assert set(composition.group_by_story_id) == set(plan.story_ids)
    all_group_stories = [sid for group in composition.groups for sid in group.story_ids]
    assert len(all_group_stories) == len(set(all_group_stories))

    rendered = render_article_writer_context(
        context,
        plan,
        composition_plan=composition,
        include_coverage_plan=False,
    )
    assert "Садовой" in rendered
    assert "Морской" in rendered
    assert "воду восстановили" in rendered
    assert "остаётся ограниченной" in rendered
    assert "story:water-a" in rendered
    assert "story:water-b" in rendered


def test_composition_separates_same_place_different_service_domains():
    cards = (
        StoryCard(id="story:water", topic="Вода на Садовой", summary="Вода", importance="medium"),
        StoryCard(id="story:power", topic="Свет на Садовой", summary="Свет", importance="medium"),
    )
    supports = (
        _support("story:water", "story:water:evidence:0:frag:1", "На Садовой воду восстановили."),
        _support("story:power", "story:power:evidence:0:frag:2", "На Садовой света нет."),
    )
    context = _context(cards, supports)
    plan = _manual_plan(cards, supports)
    composition = build_article_composition_plan(plan, context, project_article_material(context))

    assert (
        composition.bundle_by_story_id["story:water"].bundle_id
        != composition.bundle_by_story_id["story:power"].bundle_id
    )


def test_composition_suppresses_only_projection_marked_stories_and_keeps_plan_immutable():
    cards = (
        StoryCard(
            id="story:useful", topic="Бесплатный набор детей", summary="Набор", importance="medium"
        ),
        StoryCard(id="story:ad", topic="Продажа квартиры", summary="Продажа", importance="low"),
    )
    useful = _support(
        "story:useful",
        "story:useful:evidence:0:frag:1",
        "Спортивная школа открыла бесплатную запись детей.",
    )
    ad = _support(
        "story:ad",
        "story:ad:evidence:0:frag:2",
        "Продам квартиру, цена 100 рублей, звоните +79900000000.",
    )
    context = _context(cards, (useful, ad))
    plan = build_article_coverage_plan(cards, context)
    projection = project_article_material(context)
    composition = build_article_composition_plan(plan, context, projection)

    assert "story:ad" in composition.suppressed_story_ids
    assert "story:ad" in plan.story_ids
    assert "story:ad" not in composition.group_by_story_id
    assert "story:useful" in composition.group_by_story_id
    metadata = composition.to_metadata()
    assert metadata["group_count"] == len(composition.groups)
    assert metadata["line_count"] == len(composition.narrative_lines)
    assert metadata["suppressed_story_ids"] == ["story:ad"]


def test_composition_groups_only_supported_service_place_and_time_relations():
    cards = (
        StoryCard(
            id="story:outage",
            topic="Электричество на Морозова",
            summary="Отключение",
            importance="medium",
        ),
        StoryCard(
            id="story:voltage",
            topic="Электричество на Пионерской",
            summary="Напряжение",
            importance="medium",
        ),
        StoryCard(
            id="story:solar", topic="Электроснабжение", summary="Панель", importance="medium"
        ),
        StoryCard(
            id="story:water-old", topic="Вода на Центральной", summary="Утром", importance="medium"
        ),
        StoryCard(
            id="story:water-new",
            topic="Вода на Карла Маркса",
            summary="Вечером",
            importance="medium",
        ),
    )
    supports = (
        _support("story:outage", "story:outage:s1", "На улице Морозова нет электричества."),
        _support("story:voltage", "story:voltage:s1", "На улице Пионерской пониженное напряжение."),
        _support("story:solar", "story:solar:s1", "В доме установили солнечную панель."),
        _support(
            "story:water-old",
            "story:water-old:s1",
            "На улице Тверская вода подавалась утром.",
            effective_from=dt.datetime(2026, 9, 22, 6, tzinfo=dt.timezone.utc),
            effective_until=dt.datetime(2026, 9, 22, 12, tzinfo=dt.timezone.utc),
        ),
        _support(
            "story:water-new",
            "story:water-new:s1",
            "На улице Карла Маркса воды нет вечером.",
            effective_from=dt.datetime(2026, 9, 22, 18, tzinfo=dt.timezone.utc),
            effective_until=dt.datetime(2026, 9, 22, 23, tzinfo=dt.timezone.utc),
        ),
    )
    context = _context(cards, supports, edition_slug="berdyansk")
    plan = _manual_plan(cards, supports)
    composition = build_article_composition_plan(plan, context, project_article_material(context))

    by_story = composition.group_by_story_id
    assert by_story["story:outage"].relation == "localized_contrast"
    assert by_story["story:outage"].group_id == by_story["story:voltage"].group_id
    assert by_story["story:solar"].relation == "independent"
    assert by_story["story:solar"].group_id not in {
        by_story["story:outage"].group_id,
        by_story["story:voltage"].group_id,
    }
    assert by_story["story:water-old"].relation == "temporal_progression"
    assert by_story["story:water-old"].group_id == by_story["story:water-new"].group_id
    visible_story_ids = set(plan.story_ids)
    memberships = [member.story_id for group in composition.groups for member in group.members]
    assert len(memberships) == len(set(memberships))
    assert set(memberships) == visible_story_ids
    for group in composition.groups:
        for member in group.members:
            assert member.prominence == plan.by_story_id[member.story_id].prominence
            assert all(
                context.support_by_id[support_id].story_id == member.story_id
                for support_id in member.support_ids
            )
    metadata = composition.to_metadata()
    assert metadata["line_count"] == len(composition.narrative_lines)
    assert metadata["group_count"] == len(composition.groups)
    line_ids = {line.line_id for line in composition.narrative_lines}
    assert {group.narrative_line_id for group in composition.groups} == line_ids
    assert {
        member["story_id"] for group in metadata["groups"] for member in group["members"]
    } == visible_story_ids
    solar_line = next(
        line
        for line in composition.narrative_lines
        if line.line_id == by_story["story:solar"].narrative_line_id
    )
    assert solar_line.heading_hint == "Коммунальная обстановка"
    assert len(solar_line.group_ids) == 3

    overlapping_supports = (
        supports[0],
        supports[1],
        supports[2],
        _support(
            "story:water-old",
            "story:water-old:s1",
            "На улице Тверская вода подавалась утром.",
            effective_from=dt.datetime(2026, 9, 22, 6, tzinfo=dt.timezone.utc),
            effective_until=dt.datetime(2026, 9, 22, 18, tzinfo=dt.timezone.utc),
        ),
        _support(
            "story:water-new",
            "story:water-new:s1",
            "На улице Карла Маркса воды нет вечером.",
            effective_from=dt.datetime(2026, 9, 22, 12, tzinfo=dt.timezone.utc),
            effective_until=dt.datetime(2026, 9, 22, 23, tzinfo=dt.timezone.utc),
        ),
    )
    overlapping_context = _context(cards, overlapping_supports, edition_slug="berdyansk")
    overlapping_plan = _manual_plan(cards, overlapping_supports)
    overlapping_composition = build_article_composition_plan(
        overlapping_plan,
        overlapping_context,
        project_article_material(overlapping_context),
    )
    assert overlapping_composition.group_by_story_id["story:water-old"].relation == "independent"
    assert (
        overlapping_composition.group_by_story_id["story:water-old"].group_id
        != overlapping_composition.group_by_story_id["story:water-new"].group_id
    )

    incomplete_supports = (
        supports[0],
        supports[1],
        supports[2],
        _support(
            "story:water-old",
            "story:water-old:s1",
            "На улице Тверская вода подавалась утром.",
            effective_from=dt.datetime(2026, 9, 22, 6, tzinfo=dt.timezone.utc),
        ),
        supports[4],
    )
    incomplete_context = _context(cards, incomplete_supports, edition_slug="berdyansk")
    incomplete_plan = _manual_plan(cards, incomplete_supports)
    incomplete_composition = build_article_composition_plan(
        incomplete_plan,
        incomplete_context,
        project_article_material(incomplete_context),
    )
    assert incomplete_composition.group_by_story_id["story:water-old"].relation == "independent"


def test_thematic_lines_can_hold_distinct_evidence_groups_without_a_catch_all_line():
    cards = (
        StoryCard(
            id="story:social-help",
            topic="Помощь пожилым жителям",
            summary="Помощь пожилым",
            importance="medium",
            rubric_id="помощь",
        ),
        StoryCard(
            id="story:clinic",
            topic="Приём в поликлинике",
            summary="Приём врачей",
            importance="medium",
            rubric_id="медицина",
        ),
        StoryCard(
            id="story:lost-cat",
            topic="Ищут пропавшую кошку",
            summary="Поиск кошки",
            importance="medium",
            rubric_id="питомцы",
        ),
        StoryCard(
            id="story:dog-shelter",
            topic="Помощь собаке",
            summary="Помощь собаке",
            importance="medium",
            rubric_id="питомцы",
        ),
    )
    supports = tuple(
        _support(card.id, f"{card.id}:evidence:0:frag:{index}", text)
        for index, (card, text) in enumerate(
            zip(
                cards,
                (
                    "Жителям старшего возраста помогают оформить выплату.",
                    "В поликлинике открыли запись к терапевту.",
                    "На улице нашли пропавшую кошку.",
                    "Жители ищут хозяев найденной собаки.",
                ),
                strict=True,
            ),
            start=1,
        )
    )
    context = _context(cards, supports)
    coverage = build_article_coverage_plan(cards, context)

    composition = build_article_composition_plan(
        coverage, context, project_article_material(context)
    )

    groups = composition.group_by_story_id
    assert groups["story:social-help"].relation == "independent"
    assert groups["story:clinic"].relation == "independent"
    assert groups["story:social-help"].group_id != groups["story:clinic"].group_id
    assert groups["story:social-help"].narrative_line_id == groups["story:clinic"].narrative_line_id
    society_line = next(
        line
        for line in composition.narrative_lines
        if line.line_id == groups["story:social-help"].narrative_line_id
    )
    assert len(society_line.group_ids) == 2
    assert (
        groups["story:lost-cat"].narrative_line_id != groups["story:dog-shelter"].narrative_line_id
    )


def test_practical_consequence_requires_one_support_to_explicitly_link_services():
    cards = (
        StoryCard(
            id="story:power",
            topic="Электроснабжение на Садовой",
            summary="Отключение",
            importance="medium",
        ),
        StoryCard(
            id="story:internet",
            topic="Связь на Садовой",
            summary="Интернет",
            importance="medium",
        ),
    )
    power = _support("story:power", "story:power:s1", "На улице Садовая света нет.")
    linked_internet = _support(
        "story:internet",
        "story:internet:s1",
        "На улице Садовая после отключения электричества оборудование подключили к генератору, поэтому появился интернет.",
    )
    context = _context(cards, (power, linked_internet))
    plan = _manual_plan(cards, (power, linked_internet))
    composition = build_article_composition_plan(plan, context, project_article_material(context))
    linked_groups = composition.group_by_story_id

    assert linked_groups["story:power"].relation == "practical_consequence"
    assert linked_groups["story:power"].group_id == linked_groups["story:internet"].group_id

    chronology_only = _support(
        "story:internet",
        "story:internet:s1",
        "На улице Садовая после отключения электричества интернет восстановился.",
    )
    chronology_context = _context(cards, (power, chronology_only))
    chronology_plan = _manual_plan(cards, (power, chronology_only))
    chronology_composition = build_article_composition_plan(
        chronology_plan, chronology_context, project_article_material(chronology_context)
    )
    chronology_groups = chronology_composition.group_by_story_id
    assert chronology_groups["story:power"].relation == "independent"
    assert chronology_groups["story:power"].group_id != chronology_groups["story:internet"].group_id

    unlinked_internet = _support(
        "story:internet",
        "story:internet:s1",
        "На улице Садовая интернет работает.",
    )
    unlinked_context = _context(cards, (power, unlinked_internet))
    unlinked_plan = _manual_plan(cards, (power, unlinked_internet))
    unlinked_composition = build_article_composition_plan(
        unlinked_plan, unlinked_context, project_article_material(unlinked_context)
    )
    unlinked_groups = unlinked_composition.group_by_story_id

    assert unlinked_groups["story:power"].relation == "independent"
    assert unlinked_groups["story:power"].group_id != unlinked_groups["story:internet"].group_id


def test_independent_develop_line_keeps_its_depth_and_neutral_intent():
    card = StoryCard(
        id="story:standalone",
        topic="Новая городская инициатива",
        summary="Инициатива",
        importance="high",
    )
    support = _support(
        card.id,
        f"{card.id}:evidence:0:frag:1",
        "Городская мастерская открыла бесплатную запись на занятия.",
    )
    context = _context((card,), (support,))
    coverage = build_article_coverage_plan((card,), context)
    composition = build_article_composition_plan(
        coverage, context, project_article_material(context)
    )

    line = composition.narrative_lines[0]
    assert composition.groups[0].relation == "independent"
    assert line.prominence == "DEVELOP"
    assert "brief" not in line.narrative_intent.casefold()
    assert line.heading_hint is None


def test_state_detection_distinguishes_negated_and_positive_availability():
    from src.publication.article_composition import _state_key

    unavailable_card = StoryCard(
        id="story:offline", topic="Связь", summary="Интернет", importance="medium"
    )
    available_card = StoryCard(
        id="story:online", topic="Связь", summary="Интернет", importance="medium"
    )
    unavailable = _support(
        unavailable_card.id,
        f"{unavailable_card.id}:evidence:0:frag:1",
        "На улице Садовая интернет не работает.",
    )
    available = _support(
        available_card.id,
        f"{available_card.id}:evidence:0:frag:2",
        "На улице Садовая интернет работает.",
    )
    unavailable_context = _context((unavailable_card,), (unavailable,))
    available_context = _context((available_card,), (available,))
    unavailable_coverage = build_article_coverage_plan((unavailable_card,), unavailable_context)
    available_coverage = build_article_coverage_plan((available_card,), available_context)

    assert _state_key(unavailable_coverage.stories[0], unavailable_context) == "unavailable"
    assert _state_key(available_coverage.stories[0], available_context) == "available"


def test_state_detection_does_not_promote_negated_outage_or_restoration_markers():
    from src.publication.article_composition import _state_key

    cases = (
        ("story:power", "Электроснабжение", "На улице Садовая свет не отключали."),
        ("story:internet", "Связь", "На улице Садовая интернет не восстановился."),
        ("story:water", "Водоснабжение", "На улице Садовая воду не отключали."),
    )
    for story_id, topic, text in cases:
        card = StoryCard(id=story_id, topic=topic, summary=text, importance="medium")
        support = _support(story_id, f"{story_id}:evidence:0:frag:1", text)
        context = _context((card,), (support,))
        coverage = build_article_coverage_plan((card,), context)

        assert _state_key(coverage.stories[0], context) is None


def test_composition_group_uses_shared_public_relation_alias():
    from typing import get_type_hints

    from src.publication.article_composition import (
        ArticleCompositionGroup,
        ArticleCompositionRelation,
        CompositionRelation,
    )

    assert CompositionRelation is ArticleCompositionRelation
    assert get_type_hints(ArticleCompositionGroup)["relation"] is ArticleCompositionRelation


def test_packet_budget_overflow_fails_closed_after_compact_representation(monkeypatch):
    full = [
        "[ARTICLE STORY PACKET story:water-a] depth=BRIEF topic=Вода\n"
        + "  support=water-a fact=...",
        "[ARTICLE STORY PACKET story:water-b] depth=BRIEF topic=Вода\n"
        + "  support=water-b fact=...",
    ]
    monkeypatch.setattr(writer_context, "ARTICLE_WRITER_CONTEXT_MAX_CHARS", 80)

    with pytest.raises(
        ValueError, match="exceed writer context budget after compact materialization"
    ):
        _fit_story_packets("prefix", full, full)
