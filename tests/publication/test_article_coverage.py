from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from src.editorial_models import StoryCard
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import build_article_coverage_plan

_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "article_city_life_coverage_cases.json"


def _load_cases() -> dict:
    with open(_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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
