import datetime as dt

import pytest

from src.publication.delivery import _render_payload
from src.publication.models import Publication
from src.publication.renderers import render_longitudinal_telegram_teaser

pytestmark = pytest.mark.unit


def test_render_longitudinal_telegram_teaser_weekly():
    title = "Бердянск: главное за неделю с 31 августа по 6 сентября"
    lead = "Неделя прошла под знаком масштабных ремонтов водовода и подготовки школ."
    body = (
        "## Инфраструктура и жизнеобеспечение\n\n"
        "Главным событием недели стали ремонты на Нагорной части.\n\n"
        "## Городской транспорт\n\n"
        "Маршрут №4 вернулся к обычному графику движения.\n\n"
        "## Городской горизонт\n\n"
        "К концу недели основные аварии ликвидированы."
    )
    telegraph_url = "https://telegra.ph/Berdyansk-Weekly-09-06"

    teaser = render_longitudinal_telegram_teaser(
        publication_type="weekly_article",
        title=title,
        lead=lead,
        body=body,
        telegraph_url=telegraph_url,
    )
    assert "ИТОГИ НЕДЕЛИ" in teaser
    assert "Инфраструктура и жизнеобеспечение" in teaser
    assert "Городской транспорт" in teaser
    assert "Главным событием недели стали ремонты" in teaser
    assert telegraph_url in teaser


def test_render_longitudinal_telegram_teaser_monthly():
    title = "Бердянск в августе 2026 года"
    lead = "Месяц продемонстрировал постепенную стабилизацию городской логистики."
    body = (
        "## Потребительский рынок и цены\n\n"
        "Цены на сезонные овощи снизились на 15 процентов.\n\n"
        "## Социальная жизнь и городская среда\n\n"
        "В городских парках завершились работы по благоустройству."
    )
    telegraph_url = "https://telegra.ph/Berdyansk-August-2026"

    teaser = render_longitudinal_telegram_teaser(
        publication_type="monthly_article",
        title=title,
        lead=lead,
        body=body,
        telegraph_url=telegraph_url,
    )
    assert "ПАНОРАМА МЕСЯЦА" in teaser
    assert "Потребительский рынок и цены" in teaser
    assert "Социальная жизнь и городская среда" in teaser
    assert telegraph_url in teaser


def test_render_payload_for_longitudinal_article():
    now = dt.datetime.now(dt.timezone.utc)
    pub = Publication(
        id=1,
        publication_run_id=1,
        winning_generation_attempt_id=1,
        publication_type="weekly_article",
        title="Итоги недели в Бердянске",
        lead="Вводный абзац недели.",
        body="## Инфраструктура\n\nРемонт сетей завершен.",
        metadata={"telegraph_url": "https://telegra.ph/weekly-test"},
        created_at=now,
    )

    fmt_tg, content_tg = _render_payload("telegram_channel", pub)
    assert fmt_tg == "telegram_html"
    assert "ИТОГИ НЕДЕЛИ" in content_tg["text"]
    assert "https://telegra.ph/weekly-test" in content_tg["text"]

    fmt_tph, content_tph = _render_payload("telegraph", pub)
    assert fmt_tph == "telegraph_nodes"
    assert content_tph["title"] == "Итоги недели в Бердянске"
    assert "Ремонт сетей завершен" in content_tph["body_markdown"]
