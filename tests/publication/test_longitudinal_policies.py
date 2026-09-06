import pytest

from src.config.schemas.publication import ArticleScheduleConfig
from src.config_loader import load_config
from src.jobs.schedules import (
    ARTICLE_PUBLICATION_TYPE,
    MONTHLY_ARTICLE_PUBLICATION_TYPE,
    WEEKLY_ARTICLE_PUBLICATION_TYPE,
)
from src.publication.policies import (
    resolve_publication_lookback_hours,
)

pytestmark = pytest.mark.unit


def test_resolve_publication_lookback_hours():
    assert resolve_publication_lookback_hours("weekly_article", None) == 168
    assert resolve_publication_lookback_hours("monthly_article", None) == 720
    assert resolve_publication_lookback_hours("daily_article", None) == 24
    assert resolve_publication_lookback_hours("article", None) == 24


def test_article_schedule_config_defaults():
    cfg = ArticleScheduleConfig()
    assert cfg.enabled is True
    assert cfg.schedule_time == "19:00"
    assert cfg.schedule_day == "sunday"
    assert cfg.lookback_hours == 168
    assert cfg.target_word_count == 2000


def test_publication_types_constants():
    assert WEEKLY_ARTICLE_PUBLICATION_TYPE == "weekly_article"
    assert MONTHLY_ARTICLE_PUBLICATION_TYPE == "monthly_article"
    assert ARTICLE_PUBLICATION_TYPE == "daily_article"


def test_config_loader_parses_longitudinal_articles():
    cfg = load_config()
    assert hasattr(cfg.settings, "weekly_article")
    assert hasattr(cfg.settings, "monthly_article")
    assert cfg.settings.weekly_article.lookback_hours == 168
    assert cfg.settings.monthly_article.lookback_hours == 720
    assert cfg.settings.weekly_article.schedule_day == "sunday"
    assert cfg.settings.monthly_article.schedule_day == 1
