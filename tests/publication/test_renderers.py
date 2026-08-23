"""Tests for Publication digest renderers (Plan 4 Task 6)."""

import datetime as dt

from src.editorial_models import (
    EditorialAnalysis,
    PreparedBundle,
    StoryCard,
    StoryElement,
)
from src.publication.editorial_adapter import FrozenEditorialInput
from src.publication.renderers import PublicationDigestRenderer

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


def _make_dummy_input() -> FrozenEditorialInput:
    cards = [
        StoryCard(
            id="story-1",
            topic="ЖКХ",
            importance="high",
            summary="Ремонт водовода на АКЗ",
            representative_source_refs=["telegram:101"],
            hard_facts=[
                StoryElement(
                    text="Ремонт завершат к 22:00",
                    source_refs=["telegram:101"],
                    status="established",
                    attribution="официальные источники",
                )
            ],
            community_observations=[
                StoryElement(
                    text="Воды нет с утра",
                    source_refs=["telegram:101"],
                    status="attributed",
                )
            ],
        ),
        StoryCard(
            id="story-2",
            topic="Транспорт",
            importance="medium",
            summary="Новое расписание автобусов",
            representative_source_refs=["telegram:102"],
            hard_facts=[
                StoryElement(
                    text="Маршрут №4 продлен",
                    source_refs=["telegram:102"],
                    status="established",
                )
            ],
        ),
    ]
    bundle = PreparedBundle(
        records={},
        prompt_text="",
        total_messages=2,
        candidate_count=2,
    )
    return FrozenEditorialInput(analysis=EditorialAnalysis(cards=cards), writer_bundle=bundle)


class TestPublicationDigestRenderer:
    """Unit tests for PublicationDigestRenderer."""

    def test_render_grouped_digest_formats_sections_and_stats(self):
        renderer = PublicationDigestRenderer(use_emojis=True, include_statistics=True)
        frozen = _make_dummy_input()
        title, lead, body = renderer.render_grouped_digest(
            frozen, edition_name="Бердянск", snapshot_at=_NOW
        )

        assert "Дайджест: Бердянск" in title
        assert "Ремонт водовода на АКЗ" in lead
        assert "### 📌 ЖКХ" in body
        assert "• Ремонт водовода на АКЗ — Ремонт завершат к 22:00 [🔗 telegram:101]" in body
        assert "### 📌 Транспорт" in body
        assert "• Новое расписание автобусов — Маршрут №4 продлен [🔗 telegram:102]" in body
        assert "Статистика: 2 тем(ы)" in body

    def test_render_channel_digest_formats_numbered_list(self):
        renderer = PublicationDigestRenderer(use_emojis=True, include_statistics=True)
        frozen = _make_dummy_input()
        title, lead, body = renderer.render_channel_digest(
            frozen, edition_name="Бердянск", snapshot_at=_NOW
        )

        assert "Сводка каналов: Бердянск" in title
        assert "1. Ремонт водовода на АКЗ" in body
        assert "Сообщения жителей: Воды нет с утра" in body
        assert "2. Новое расписание автобусов" in body
        assert "Всего событий: 2" in body

    def test_render_empty_cards_produces_clean_message(self):
        renderer = PublicationDigestRenderer()
        empty_frozen = FrozenEditorialInput(
            analysis=EditorialAnalysis(cards=[]),
            writer_bundle=PreparedBundle(
                records={}, prompt_text="", total_messages=0, candidate_count=0
            ),
        )
        title, lead, body = renderer.render_grouped_digest(empty_frozen)
        assert "Нет актуальных событий" in body
        assert lead == ""
