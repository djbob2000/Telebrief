from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from src.editorial_models import StoryCard
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import (
    build_article_coverage_plan,
    score_detail_support,
)

_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "article_city_life_coverage_cases.json"


def _load_cases() -> dict:
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _make_simple_support(
    text: str,
    evidence_kind: str = "community_report",
    source_text: str = "",
    story_id: str = "story:test",
    support_id: str = "story:test:evidence:0:frag:1",
) -> ArticleSupport:
    return ArticleSupport(
        support_id=support_id,
        text=text,
        source_text=source_text or text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc),
        evidence_kind=evidence_kind,
        story_id=story_id,
    )


def _build_test_context(data: dict) -> tuple[list[StoryCard], ArticleEditorialContext]:
    cards = []
    supports = []
    now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
    for story in data["stories"]:
        cards.append(
            StoryCard(
                id=story["story_id"],
                topic=story["topic"],
                importance=story.get("importance", "medium"),
                summary=story["topic"],
            )
        )
        for s in story["supports"]:
            supports.append(
                ArticleSupport(
                    support_id=s["id"],
                    text=s["text"],
                    source_text=s.get("source_text", s["text"]),
                    support_kind="evidence",
                    publication_use=s.get("publication_use", "PUBLISH"),
                    source_refs=("ref-1",),
                    fragment_ids=(1,),
                    source_item_ids=(1,),
                    observed_at=now,
                    evidence_kind=s.get("kind", "established_fact"),
                    story_id=story["story_id"],
                )
            )
    ctx = ArticleEditorialContext(
        headline_candidates=tuple(c.topic for c in cards),
        support_index=tuple(supports),
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
    )
    return cards, ctx


def test_city_life_fixture_contains_major_supporting_brief_and_question_cases():
    data = _load_cases()
    ids = {story["story_id"] for story in data["stories"]}
    assert ids >= {
        "story:power",
        "story:safety",
        "story:telecom",
        "story:sport",
        "story:route",
        "story:question",
    }


def test_score_detail_support():
    assert score_detail_support(
        _make_simple_support("Жильцы скидываются по 300 рублей на генератор")
    ) > score_detail_support(_make_simple_support("Жители обсуждают ситуацию"))

    assert score_detail_support(_make_simple_support("Автобус ходит примерно раз в час")) > 0

    assert (
        score_detail_support(
            _make_simple_support(
                "Житель запитал оборудование провайдера от своего генератора",
                evidence_kind="service_access",
            )
        )
        > 0
    )


def test_build_article_coverage_plan_prominence_and_context_only():
    data = _load_cases()
    cards, context = _build_test_context(data)
    plan = build_article_coverage_plan(cards, context)
    by_id = {item.story_id: item for item in plan.stories}

    assert by_id["story:power"].prominence == "DEVELOP"  # high / 4 supports
    assert by_id["story:safety"].prominence == "DEVELOP"  # high
    assert by_id["story:telecom"].prominence == "WEAVE"  # 2 supports
    assert by_id["story:sport"].prominence == "BRIEF"  # 1 support
    assert by_id["story:route"].prominence == "BRIEF"  # 1 support
    assert "story:question" not in by_id  # CONTEXT only

    # Ordering follows input card rank
    assert [item.story_id for item in plan.stories] == [
        "story:power",
        "story:safety",
        "story:telecom",
        "story:sport",
        "story:route",
    ]

    # Detail support budgets
    assert len(by_id["story:power"].detail_support_ids) == 3
    assert len(by_id["story:telecom"].detail_support_ids) == 2
    assert len(by_id["story:sport"].detail_support_ids) == 1


def test_city_life_coverage_golden_expectations():
    from src.publication.article_writer_context import render_article_writer_context

    data = _load_cases()
    expectations = data.get("expectations", {})
    cards, context = _build_test_context(data)
    plan = build_article_coverage_plan(cards, context)

    # 1. Broad plan coverage without over-pruning
    assert set(plan.story_ids) == set(expectations["must_plan_story_ids"])
    for forbidden_id in expectations.get("must_not_plan_story_ids", []):
        assert forbidden_id not in plan.story_ids

    # 2. Rendered writer context contains microdetail anchors
    rendered = render_article_writer_context(context, plan)
    for detail_id in expectations.get("must_preserve_detail_support_ids", []):
        assert detail_id in rendered

    # 3. Directory payload is absent from writer context
    for forbidden_payload in expectations.get("writer_source_must_not_contain", []):
        assert forbidden_payload not in rendered


def test_article_coverage_plan_exposes_canonical_indexes():
    data = _load_cases()
    cards, context = _build_test_context(data)
    plan = build_article_coverage_plan(cards, context)

    assert plan.story_ids
    assert set(plan.by_story_id) == set(plan.story_ids)
    assert set(plan.support_ids_by_story) == set(plan.story_ids)
    for story_id in plan.story_ids:
        assert plan.by_story_id[story_id].support_ids
        assert plan.support_ids_by_story[story_id] == plan.by_story_id[story_id].support_ids


def test_prominence_from_selection_signals():
    """Tests 6A, 6B, 6C, 6D: Selection intent mapping into article prominence."""
    from src.publication.article_context import ArticleSelectionSignal

    cards = [
        StoryCard(id="story:1", topic="Topic 1", importance="high", summary="Lead 1"),
        StoryCard(id="story:2", topic="Topic 2", importance="low", summary="Lead 2"),
        StoryCard(id="story:3", topic="Topic 3", importance="low", summary="Normal 1"),
        StoryCard(id="story:4", topic="Topic 4", importance="low", summary="Normal 2"),
        StoryCard(id="story:5", topic="Topic 5", importance="high", summary="Normal 3"),
        StoryCard(id="story:6", topic="Topic 6", importance="high", summary="Follow up"),
        StoryCard(id="story:7", topic="Topic 7", importance="high", summary="Unverified op"),
        StoryCard(id="story:8", topic="Topic 8", importance="high", summary="Brief"),
        StoryCard(id="story:9", topic="Topic 9", importance="high", summary="No signal"),
    ]

    supports = [
        _make_simple_support("Fact", story_id=c.id, support_id=f"{c.id}:sup:1") for c in cards
    ]

    selection_by_story = {
        "story:1": ArticleSelectionSignal(story_id="story:1", intent="lead", rank=1),
        "story:2": ArticleSelectionSignal(
            story_id="story:2", intent="lead", rank=2
        ),  # extra lead -> normal
        "story:3": ArticleSelectionSignal(story_id="story:3", intent="normal", rank=3),
        "story:4": ArticleSelectionSignal(story_id="story:4", intent="normal", rank=4),
        "story:5": ArticleSelectionSignal(story_id="story:5", intent="normal", rank=5),
        "story:6": ArticleSelectionSignal(story_id="story:6", intent="follow_up", rank=6),
        "story:7": ArticleSelectionSignal(
            story_id="story:7", intent="unverified_operational", rank=7
        ),
        "story:8": ArticleSelectionSignal(story_id="story:8", intent="brief", rank=8),
        # story:9 has no selection signal
    }

    ctx = ArticleEditorialContext(
        headline_candidates=tuple(c.topic for c in cards),
        support_index=tuple(supports),
        support_by_id={s.support_id: s for s in supports},
        recurring_topics=(),
        selection_by_story=selection_by_story,
    )

    # With develop_story_budget = 2:
    # 1. story:1 (first lead) -> DEVELOP
    # 2. story:2 (extra lead -> normal) and story:3 (normal, rank 3) are top 2 normals -> DEVELOP
    # 3. story:4 and story:5 (remaining normals) -> WEAVE
    # 4. story:6 (follow_up) -> WEAVE (even though importance="high")
    # 5. story:7 (unverified_operational) -> BRIEF (even though importance="high")
    # 6. story:8 (brief) -> BRIEF (even though importance="high")
    # 7. story:9 (missing signal) -> BRIEF (even though importance="high")
    plan = build_article_coverage_plan(cards, ctx, develop_story_budget=2)
    by_id = {item.story_id: item for item in plan.stories}

    # Test 6A: First lead -> DEVELOP, extra lead -> normal
    assert by_id["story:1"].prominence == "DEVELOP"

    # Test 6B: Top-ranked normal elevated to DEVELOP up to develop_story_budget = 2
    assert by_id["story:2"].prominence == "DEVELOP"  # extra lead treated as normal rank 2
    assert by_id["story:3"].prominence == "DEVELOP"  # normal rank 3
    assert by_id["story:4"].prominence == "WEAVE"  # normal rank 4 (budget exhausted)
    assert by_id["story:5"].prominence == "WEAVE"  # normal rank 5 (budget exhausted)

    # Test 6C: follow_up -> WEAVE, unverified_operational -> BRIEF, brief -> BRIEF, missing signal -> BRIEF
    assert by_id["story:6"].prominence == "WEAVE"
    assert by_id["story:7"].prominence == "BRIEF"
    assert by_id["story:8"].prominence == "BRIEF"
    assert by_id["story:9"].prominence == "BRIEF"
