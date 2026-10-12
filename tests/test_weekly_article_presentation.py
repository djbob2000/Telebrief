# ruff: noqa: S101, S108
import datetime as dt
from types import SimpleNamespace

from src.article_generator import _article_period_label
from src.jobs.schedules import due_publication_actions
from src.publication.article_context import PublicationWindow
from src.publication.delivery import _render_payload


def _config() -> SimpleNamespace:
    settings = SimpleNamespace(
        timezone="Europe/Moscow",
        pre_publish_lead_minutes=0,
        schedule_time="09:00",
        article=SimpleNamespace(enabled=True, schedule_time="21:15"),
        weekly_article=SimpleNamespace(enabled=True, schedule_time="21:15", schedule_day="sunday"),
        monthly_article=None,
    )
    return SimpleNamespace(settings=settings)


def _published_types(when: dt.datetime) -> set[str]:
    actions = due_publication_actions(_config(), when)
    return {a.task_kwargs["publication_type"] for a in actions if a.kind == "publish"}


def test_weekly_review_replaces_daily_article_on_its_day() -> None:
    sunday = dt.datetime(2026, 10, 11, 18, 15, tzinfo=dt.timezone.utc)
    assert _published_types(sunday) == {"weekly_article"}


def test_daily_article_still_runs_on_other_days() -> None:
    saturday = dt.datetime(2026, 10, 10, 18, 15, tzinfo=dt.timezone.utc)
    assert _published_types(saturday) == {"daily_article"}


def _pub(publication_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        publication_type=publication_type,
        title="Заголовок",
        lead="Вступление.",
        body="Текст.",
        metadata={"photo_path": "/tmp/x.jpg"},
    )


def test_weekly_photo_post_is_labelled() -> None:
    _, data = _render_payload("telegram_channel", _pub("weekly_article"))
    assert data["text"].startswith("*ИТОГИ НЕДЕЛИ*\n📰 *Заголовок*")


def test_daily_photo_post_has_no_period_label() -> None:
    _, data = _render_payload("telegram_channel", _pub("daily_article"))
    assert data["text"].startswith("📰 *Заголовок*")


def test_period_label_for_weekly_window() -> None:
    window = PublicationWindow(
        snapshot_at=dt.datetime(2026, 10, 11, 18, 15, tzinfo=dt.timezone.utc),
        lookback_start=dt.datetime(2026, 10, 4, 18, 15, tzinfo=dt.timezone.utc),
    )
    assert _article_period_label(window, "Europe/Moscow") == "неделя с 5 по 11 октября"
