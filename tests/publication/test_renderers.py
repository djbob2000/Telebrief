"""Tests for Publication digest renderers (Plan 4 Task 6)."""

import datetime as dt

from src.collector import Message
from src.editorial_models import (
    EditorialAnalysis,
    PreparedBundle,
    SourceRecord,
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
        assert "Коммунальная обстановка" in body
        assert "Ремонт водовода на АКЗ" in body
        assert "Транспорт и дороги" in body
        assert "Новое расписание автобусов" in body
        assert "Статистика:" in body

    def test_render_channel_digest_formats_numbered_list(self):
        renderer = PublicationDigestRenderer(use_emojis=True, include_statistics=True)
        frozen = _make_dummy_input()
        title, lead, body = renderer.render_channel_digest(
            frozen, edition_name="Бердянск", snapshot_at=_NOW
        )

        assert "Дайджест: Бердянск" in title
        assert "Ремонт водовода на АКЗ" in body
        assert "Новое расписание автобусов" in body

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

    def test_render_suppresses_generic_fallback_topics_in_bullet(self):
        renderer = PublicationDigestRenderer(use_emojis=False, include_statistics=False)
        cards = [
            StoryCard(
                id="story-1",
                topic="Городские события",
                importance="medium",
                summary="В районе проводятся плановые работы",
                category="utilities",
                representative_source_refs=["ref-1"],
            )
        ]
        frozen = FrozenEditorialInput(
            analysis=EditorialAnalysis(cards=cards),
            writer_bundle=PreparedBundle(
                records={}, prompt_text="", total_messages=1, candidate_count=1
            ),
        )
        _, _, body = renderer.render_grouped_digest(frozen)

        # Must render clean bullet without '**Городские события**'
        assert "• В районе проводятся плановые работы." in body
        assert "**Городские события**" not in body

    def test_render_suppresses_redundant_observation_tautology(self):
        renderer = PublicationDigestRenderer(use_emojis=False, include_statistics=False)
        cards = [
            StoryCard(
                id="story-1",
                topic="Водоснабжение",
                importance="medium",
                summary="Воды нет с утра на Восточном",
                category="utilities",
                representative_source_refs=["ref-1"],
                community_observations=[
                    StoryElement(
                        text="Воды нет с утра",
                        source_refs=["ref-1"],
                        status="attributed",
                    )
                ],
            )
        ]
        frozen = FrozenEditorialInput(
            analysis=EditorialAnalysis(cards=cards),
            writer_bundle=PreparedBundle(
                records={}, prompt_text="", total_messages=1, candidate_count=1
            ),
        )
        _, _, body = renderer.render_grouped_digest(frozen)

        # Observation 'Воды нет с утра' is substring of summary, so redundant sub-point is suppressed
        assert "По сообщениям жителей:" not in body

    def test_render_grouped_digest_uses_rubrics_config_order_and_emojis(self):
        from src.config_loader import DigestRubricConfig, DigestRubricsConfig

        rubrics = DigestRubricsConfig(
            min_similarity=0.38,
            items=(
                DigestRubricConfig(
                    id="safety",
                    name="Безопасность",
                    description="",
                    emoji="💥",
                    fallback=False,
                ),
                DigestRubricConfig(
                    id="infrastructure",
                    name="Инфраструктура",
                    description="",
                    emoji="⚡️",
                    fallback=False,
                ),
                DigestRubricConfig(
                    id="other",
                    name="Другое",
                    description="",
                    emoji="📌",
                    fallback=True,
                ),
            ),
        )
        renderer = PublicationDigestRenderer(rubrics_config=rubrics, use_emojis=True)
        cards = [
            StoryCard(
                id="story-1",
                topic="Водовод",
                importance="medium",
                summary="Ремонт трубы",
                rubric_id="infrastructure",
                representative_source_refs=["ref-1"],
            ),
            StoryCard(
                id="story-2",
                topic="ПВО",
                importance="high",
                summary="Сбита цель",
                rubric_id="safety",
                representative_source_refs=["ref-2"],
            ),
        ]
        frozen = FrozenEditorialInput(
            analysis=EditorialAnalysis(cards=cards),
            writer_bundle=PreparedBundle(
                records={}, prompt_text="", total_messages=2, candidate_count=2
            ),
        )
        _, _, body = renderer.render_grouped_digest(frozen)

        # Safety should appear before Infrastructure because of config order
        pos_safety = body.find("💥 Безопасность")
        pos_infra = body.find("⚡️ Инфраструктура")
        assert pos_safety != -1
        assert pos_infra != -1
        assert pos_safety < pos_infra

    def test_render_grouped_digest_includes_city_situation_and_4level_stats(self):
        from src.collector import Message
        from src.editorial_models import SourceRecord
        from src.publication.city_situation import CitySituationItem, CitySituationRollup

        now = dt.datetime(2026, 8, 29, 12, 0, tzinfo=dt.timezone.utc)
        item = CitySituationItem(
            subject_key="water_supply",
            subject_label="Водоснабжение",
            dimension="availability",
            location="Колония",
            entity="водовод",
            state="UNAVAILABLE",
            detail="Аварийное отключение до 18:00",
            source_refs=("ref-1",),
            first_observed_at=now,
            last_observed_at=now,
            observation_count=1,
        )
        rollup = CitySituationRollup(items=(item,))

        card = StoryCard(
            id="story-1",
            topic="Порыв на водоводе",
            importance="high",
            summary="Авария в Колонии",
            rubric_id="utilities",
            representative_source_refs=["ref-1"],
        )

        msg1 = Message(
            text="Порыв трубы в Колонии",
            sender="Водоканал",
            timestamp=now,
            link="https://t.me/c/1",
            channel_id="c1",
            channel_name="Бердянскводоканал",
        )
        msg2 = Message(
            text="Сварочные работы продолжаются",
            sender="Водоканал",
            timestamp=now,
            link="https://t.me/c/2",
            channel_id="c1",
            channel_name="Бердянскводоканал",
        )
        records = {
            "telegram:source:1:item:1:rev:1:frag:101": SourceRecord(
                ref="telegram:source:1:item:1:rev:1:frag:101",
                message=msg1,
                source_type="official",
            ),
            "telegram:source:1:item:2:rev:1:frag:102": SourceRecord(
                ref="telegram:source:1:item:2:rev:1:frag:102",
                message=msg2,
                source_type="official",
            ),
        }

        frozen = FrozenEditorialInput(
            analysis=EditorialAnalysis(cards=[card], city_situation=rollup),
            writer_bundle=PreparedBundle(
                records=records, prompt_text="", total_messages=2, candidate_count=1
            ),
        )

        renderer = PublicationDigestRenderer(use_emojis=True, include_statistics=True)
        _, _, body = renderer.render_grouped_digest(frozen)

        # 1. City situation section rendered
        assert "Городская обстановка:" in body
        assert "🔴 <b>Водоснабжение (Колония)</b>: Аварийное отключение до 18:00" in body

        # 2. 4-level statistics rendered
        assert "Статистика: источников: 1, сообщений: 2, фактов: 2, событий: 1." in body

    def test_render_grouped_digest_with_narrative_draft(self):
        from src.publication.digest_narrative import (
            DigestNarrativeBlockDraft,
            DigestNarrativeDraft,
            DigestNarrativeParagraph,
        )

        card = StoryCard(
            id="story:1",
            topic="Водоснабжение",
            importance="high",
            summary="Ремонт завершен",
            rubric_id="utilities",
        )
        msg = Message(
            text="Водоканал завершил ремонтные работы на сетях.",
            sender="Official",
            timestamp=_NOW,
            link="https://t.me/c/1",
            channel_id="c1",
            channel_name="Official",
        )
        records = {
            "telegram:source:1:item:1:rev:1:frag:1": SourceRecord(
                ref="telegram:source:1:item:1:rev:1:frag:1",
                message=msg,
                source_type="official",
            )
        }
        frozen = FrozenEditorialInput(
            analysis=EditorialAnalysis(cards=[card]),
            writer_bundle=PreparedBundle(
                records=records, prompt_text="", total_messages=1, candidate_count=1
            ),
        )

        narrative_draft = DigestNarrativeDraft(
            blocks=(
                DigestNarrativeBlockDraft(
                    block_id="block:utilities:0",
                    heading="Городское хозяйство",
                    paragraphs=(
                        DigestNarrativeParagraph(
                            text="Водоканал завершил ремонтные работы на сетях в центре города.",
                            cited_support_ids=("sup:1",),
                            covered_story_ids=("story:1",),
                        ),
                    ),
                ),
            )
        )

        renderer = PublicationDigestRenderer(use_emojis=True, include_statistics=True)
        title, lead, body = renderer.render_grouped_digest(
            frozen,
            edition_name="Бердянск",
            narrative_draft=narrative_draft,
        )

        assert "*⚡️ Городское хозяйство*" in body or "Городское хозяйство" in body
        assert "Водоканал завершил ремонтные работы на сетях в центре города." in body
        assert "• **Водоснабжение**" not in body  # Replaced by flowing paragraph!
        assert "Статистика:" in body
