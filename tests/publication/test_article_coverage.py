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
    text: str, evidence_kind: str = "community_report", source_text: str = ""
) -> ArticleSupport:
    return ArticleSupport(
        support_id="story:test:evidence:0:frag:1",
        text=text,
        source_text=source_text or text,
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(1,),
        source_item_ids=(1,),
        observed_at=dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc),
        evidence_kind=evidence_kind,
        story_id="story:test",
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
