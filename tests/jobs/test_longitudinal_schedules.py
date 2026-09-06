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
    first_1000 = dt.datetime(2026, 9, 1, 10, 0, tzinfo=tz)

    actions = due_publication_actions(config, first_1000)
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
    second_1000 = dt.datetime(2026, 9, 2, 10, 0, tzinfo=tz)

    actions = due_publication_actions(config, second_1000)
    monthly_acts = [
        a
        for a in actions
        if a.task_kwargs.get("publication_type") == MONTHLY_ARTICLE_PUBLICATION_TYPE
    ]
    assert len(monthly_acts) == 0
