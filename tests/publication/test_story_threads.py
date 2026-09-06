import datetime as dt

import pytest

from src.editorial_models import StoryCard
from src.publication.story_threads import (
    StoryThread,
    ThreadEditorialWeight,
    TrajectoryKind,
    build_longitudinal_coverage_plan,
    build_milestone_timeline,
    classify_thread_trajectory,
    cluster_stories_into_threads,
)

pytestmark = pytest.mark.unit


def test_trajectory_classification_chronic():
    # Story active across 3 distinct days
    dates = [
        dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 9, 3, 14, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 9, 5, 9, 0, tzinfo=dt.timezone.utc),
    ]
    trajectory = classify_thread_trajectory(dates, total_observations=5)
    assert trajectory == TrajectoryKind.CHRONIC_EVOLVING


def test_trajectory_classification_acute():
    # High burst in 1-2 days (total_observations >= 3 or high source count)
    dates = [
        dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 9, 2, 18, 0, tzinfo=dt.timezone.utc),
    ]
    trajectory = classify_thread_trajectory(dates, total_observations=6)
    assert trajectory == TrajectoryKind.ACUTE_PIVOTAL


def test_trajectory_classification_background():
    dates = [
        dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.timezone.utc),
    ]
    trajectory = classify_thread_trajectory(dates, total_observations=1)
    assert trajectory == TrajectoryKind.BACKGROUND_LOCAL


def test_thread_clustering_by_rubric_and_entity():
    card1 = StoryCard(
        id="story:1",
        topic="Водоснабжение в Нагорной части города",
        summary="Перебои с водой на улице Нагорной",
        importance="medium",
        rubric_id="ЖКХ",
        representative_source_refs=["ref:1", "ref:2", "ref:3"],
    )
    card2 = StoryCard(
        id="story:2",
        topic="Ремонт водовода на Нагорной",
        summary="Завершение работ аварийных бригад водоканала",
        importance="medium",
        rubric_id="ЖКХ",
        representative_source_refs=["ref:4", "ref:5", "ref:6", "ref:7"],
    )
    card3 = StoryCard(
        id="story:3",
        topic="Бесплатные секции в детской спортшколе",
        summary="Открыт набор детей на новый учебный год",
        importance="low",
        rubric_id="Спорт",
        representative_source_refs=["ref:8"],
    )

    threads = cluster_stories_into_threads(
        cards=[card1, card2, card3],
        story_dates={
            "story:1": [
                dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 9, 2, 14, 0, tzinfo=dt.timezone.utc),
            ],
            "story:2": [dt.datetime(2026, 9, 4, 15, 0, tzinfo=dt.timezone.utc)],
            "story:3": [dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc)],
        },
    )
    assert len(threads) == 2
    water_thread = next(t for t in threads if "водо" in t.title.lower() or t.rubric == "ЖКХ")
    assert set(water_thread.story_ids) == {"story:1", "story:2"}
    assert water_thread.trajectory == TrajectoryKind.CHRONIC_EVOLVING


def test_build_milestone_timeline():
    milestones = build_milestone_timeline(
        story_id="story:1",
        facts=[
            ("2026-09-01T10:00:00Z", "Отключение водовода на Нагорной", "sup:1"),
            ("2026-09-03T14:00:00Z", "Подвоз технической воды автоцистернами", "sup:2"),
        ],
    )
    assert len(milestones) == 2
    assert milestones[0].date_str == "01.09"
    assert "Отключение водовода" in milestones[0].fact
    assert milestones[1].date_str == "03.09"


def test_build_longitudinal_coverage_plan_thematic_chapters():
    t1 = StoryThread(
        id="thread:infra",
        title="Кризис водоснабжения и ремонты водовода",
        rubric="ЖКХ",
        story_ids=("story:1", "story:2"),
        trajectory=TrajectoryKind.CHRONIC_EVOLVING,
        weight=ThreadEditorialWeight.LEAD_THREAD,
        milestones=(),
        support_ids=("story:1:ev:1", "story:2:ev:1"),
    )
    t2 = StoryThread(
        id="thread:transit",
        title="Сбои в движении пригородных маршрутов",
        rubric="Транспорт",
        story_ids=("story:3",),
        trajectory=TrajectoryKind.ACUTE_PIVOTAL,
        weight=ThreadEditorialWeight.WEAVE_THREAD,
        milestones=(),
        support_ids=("story:3:ev:1",),
    )

    plan = build_longitudinal_coverage_plan(threads=[t1, t2])
    assert len(plan.sections) >= 2
    # Verify 100% story coverage invariant: all story IDs must be in coverage plan
    assert set(plan.story_ids) == {"story:1", "story:2", "story:3"}
    sec_titles = [s.title.lower() for s in plan.sections]
    assert any("инфраструктура" in st or "жкх" in st for st in sec_titles)
    assert any("транспорт" in st for st in sec_titles)
