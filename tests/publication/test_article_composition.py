from __future__ import annotations

import datetime as dt

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
    ARTICLE_WRITER_CONTEXT_MAX_CHARS,
    _fit_story_packets,
    render_article_writer_context,
)


def _support(story_id: str, support_id: str, text: str) -> ArticleSupport:
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
        evidence_kind="community_report",
        story_id=story_id,
    )


def _context(
    cards: tuple[StoryCard, ...], supports: tuple[ArticleSupport, ...]
) -> ArticleEditorialContext:
    return ArticleEditorialContext(
        headline_candidates=tuple(card.topic for card in cards),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
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

    water_bundle = next(
        bundle for bundle in composition.bundles if "story:water-a" in bundle.story_ids
    )
    assert set(water_bundle.story_ids) == {"story:water-a", "story:water-b"}
    assert set(water_bundle.support_ids) == {
        "story:water-a:evidence:0:frag:1",
        "story:water-b:evidence:0:frag:2",
    }
    power_bundle = composition.bundle_by_story_id["story:power"]
    assert power_bundle.bundle_id != water_bundle.bundle_id
    assert set(composition.bundle_by_story_id) == set(plan.story_ids)
    all_bundle_stories = [sid for bundle in composition.bundles for sid in bundle.story_ids]
    assert len(all_bundle_stories) == len(set(all_bundle_stories))

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
    assert "story:ad" not in composition.bundle_by_story_id
    assert "story:useful" in composition.bundle_by_story_id
    metadata = composition.to_metadata()
    assert metadata["bundle_count"] == len(composition.bundles)
    assert metadata["suppressed_story_ids"] == ["story:ad"]


def test_packet_budget_fallback_keeps_bundle_and_member_story_ids():
    bundle_header = (
        "[ARTICLE COMPOSITION BUNDLE bundle:1:infrastructure:water] "
        "section=infrastructure theme=water lead=story:water-a"
    )
    full = [
        bundle_header
        + "\n[ARTICLE STORY PACKET story:water-a] depth=BRIEF topic=Вода\n"
        + "  support=water-a fact=...",
        "[ARTICLE STORY PACKET story:water-b] depth=BRIEF topic=Вода\n"
        + "  support=water-b fact=...",
    ]
    prefix = "x" * (ARTICLE_WRITER_CONTEXT_MAX_CHARS - 300)

    rendered = _fit_story_packets(prefix, full, full)

    assert "ARTICLE COMPOSITION BUNDLE bundle:1:infrastructure:water" in rendered
    assert "ARTICLE STORY PACKET story:water-a" in rendered
    assert "ARTICLE STORY PACKET story:water-b" in rendered
