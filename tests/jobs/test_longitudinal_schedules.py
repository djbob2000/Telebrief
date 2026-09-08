import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from src.config_loader import load_config
from src.jobs.schedules import (
    MONTHLY_ARTICLE_PUBLICATION_TYPE,
    WEEKLY_ARTICLE_PUBLICATION_TYPE,
    due_publication_actions,
)

pytestmark = pytest.mark.unit


def test_due_weekly_article_on_sunday_evening():
    config = load_config()
    # 2026-09-06 is Sunday
    tz = ZoneInfo(config.settings.timezone)
    sunday_1900 = dt.datetime(2026, 9, 6, 19, 0, tzinfo=tz)

    actions = due_publication_actions(config, sunday_1900)
    weekly_acts = [
        a
        for a in actions
        if a.task_kwargs.get("publication_type") == WEEKLY_ARTICLE_PUBLICATION_TYPE
    ]
    assert len(weekly_acts) >= 1
    assert weekly_acts[0].kind == "publish"


def test_not_due_weekly_article_on_other_days():
    config = load_config()
    # 2026-09-05 is Saturday
    tz = ZoneInfo(config.settings.timezone)
    sat_1900 = dt.datetime(2026, 9, 5, 19, 0, tzinfo=tz)

    actions = due_publication_actions(config, sat_1900)
    weekly_acts = [
        a
        for a in actions
        if a.task_kwargs.get("publication_type") == WEEKLY_ARTICLE_PUBLICATION_TYPE
    ]
    assert len(weekly_acts) == 0


def test_due_monthly_article_on_first_day():
    config = load_config()
    tz = ZoneInfo(config.settings.timezone)
    first_2000 = dt.datetime(2026, 9, 1, 20, 0, tzinfo=tz)

    actions = due_publication_actions(config, first_2000)
    monthly_acts = [
        a
        for a in actions
        if a.task_kwargs.get("publication_type") == MONTHLY_ARTICLE_PUBLICATION_TYPE
    ]
    assert len(monthly_acts) >= 1
    assert monthly_acts[0].kind == "publish"


def test_not_due_monthly_article_on_other_days():
    config = load_config()
    tz = ZoneInfo(config.settings.timezone)
    second_2000 = dt.datetime(2026, 9, 2, 20, 0, tzinfo=tz)

    actions = due_publication_actions(config, second_2000)
    monthly_acts = [
        a
        for a in actions
        if a.task_kwargs.get("publication_type") == MONTHLY_ARTICLE_PUBLICATION_TYPE
    ]
    assert len(monthly_acts) == 0


def test_due_publication_actions_catch_up_within_window():
    config = load_config()
    tz = ZoneInfo(config.settings.timezone)
    # Article scheduled at 21:15; dispatcher tick delayed by 12 minutes to 21:27
    delayed_tick = dt.datetime(2026, 9, 7, 21, 27, tzinfo=tz)

    actions = due_publication_actions(config, delayed_tick, catch_up_window_minutes=120)
    article_acts = [a for a in actions if a.task_kwargs.get("publication_type") == "daily_article"]
    assert len(article_acts) == 1
    assert article_acts[0].kind == "publish"
    # Scheduled snapshot_at must still be the intended slot (21:15 - lag), not the delayed tick
    lag = dt.timedelta(minutes=config.settings.publication_snapshot_lag_minutes)
    expected_snapshot_dt = (dt.datetime(2026, 9, 7, 21, 15, tzinfo=tz) - lag).astimezone(
        dt.timezone.utc
    )
    assert article_acts[0].task_kwargs["snapshot_at"] == expected_snapshot_dt.isoformat()


def test_not_due_publication_actions_past_catch_up_window():
    config = load_config()
    tz = ZoneInfo(config.settings.timezone)
    # Article scheduled at 21:15; tick arrives 3 hours later (past 120m window)
    expired_tick = dt.datetime(2026, 9, 8, 0, 30, tzinfo=tz)

    actions = due_publication_actions(config, expired_tick, catch_up_window_minutes=120)
    article_acts = [a for a in actions if a.task_kwargs.get("publication_type") == "daily_article"]
    assert len(article_acts) == 0
