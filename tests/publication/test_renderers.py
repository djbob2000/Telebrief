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
        assert lead == ""
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
            DigestEditorialItemDraft,
            DigestNarrativeBlockDraft,
            DigestNarrativeDraft,
        )

        card1 = StoryCard(
            id="story:1",
            topic="Водоснабжение",
            importance="high",
            summary="Ремонт завершен",
            rubric_id="utilities",
        )
        card2 = StoryCard(
            id="story:2",
            topic="Банковские услуги",
            importance="medium",
            summary="Работа банкоматов",
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
            analysis=EditorialAnalysis(cards=[card1, card2]),
            writer_bundle=PreparedBundle(
                records=records, prompt_text="", total_messages=1, candidate_count=2
            ),
        )

        narrative_draft = DigestNarrativeDraft(
            blocks=(
                DigestNarrativeBlockDraft(
                    block_id="block:utilities:0",
                    items=(
                        DigestEditorialItemDraft(
                            headline="Подтвержденных сроков восстановления света пока нет",
                            body="Сообщения об отключениях поступали в разные дни.",
                            cited_support_ids=("sup:1",),
                            covered_story_ids=("story:1",),
                        ),
                        DigestEditorialItemDraft(
                            headline="На Горе банкоматы без связи, но карты в магазинах принимают",
                            body="Безналичная оплата в торговых точках города проходит без сбоев.",
                            cited_support_ids=("sup:2",),
                            covered_story_ids=("story:2",),
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

        assert (
            "• **Подтвержденных сроков восстановления света пока нет**: Сообщения об отключениях поступали в разные дни."
            in body
        )
        assert (
            "• **На Горе банкоматы без связи, но карты в магазинах принимают**: Безналичная оплата в торговых точках города проходит без сбоев."
            in body
        )
        assert "story:" not in body
        assert "sup:" not in body
        assert (
            "• **Водоснабжение**" not in body
        )  # Canonical card bullets are suppressed when narrative draft is used!
        assert "Статистика:" in body

    def test_render_grouped_digest_with_narrative_situation_items(self):
        from src.publication.digest_narrative import (
            DigestEditorialItemDraft,
            DigestNarrativeBlockDraft,
            DigestNarrativeDraft,
            DigestSituationItemDraft,
        )

        card = StoryCard(
            id="story:1",
            topic="Электричество",
            importance="high",
            summary="Отключения",
        )
        frozen = FrozenEditorialInput(
            analysis=EditorialAnalysis(cards=[card]),
            writer_bundle=PreparedBundle(
                records={}, prompt_text="", total_messages=0, candidate_count=1
            ),
        )

        draft = DigestNarrativeDraft(
            situation_items=(
                DigestSituationItemDraft(
                    group_id="situation:power:avail",
                    label="Электроснабжение",
                    body="Без света: Центр и Колония.",
                    cited_support_ids=("ref-1",),
                ),
            ),
            blocks=(
                DigestNarrativeBlockDraft(
                    block_id="block:utilities:0",
                    items=(
                        DigestEditorialItemDraft(
                            headline="Ремонт продолжается",
                            body="Аварийные бригады работают на подстанциях.",
                            cited_support_ids=("ref-2",),
                            covered_story_ids=("story:1",),
                        ),
                    ),
                ),
            ),
        )

        renderer = PublicationDigestRenderer(use_emojis=True)
        title, lead, body = renderer.render_grouped_digest(
            frozen,
            edition_name="Бердянск",
            narrative_draft=draft,
        )

        assert "Городская обстановка" in body
        assert "• **Электроснабжение**: Без света: Центр и Колония." in body
        assert "• **Ремонт продолжается**: Аварийные бригады работают на подстанциях." in body

    def test_render_layered_short_read_telegram_html(self):
        from src.publication.digest_narrative import (
            DigestEditorialItemDraft,
            DigestNarrativeBlockDraft,
            DigestSituationItemDraft,
        )
        from src.publication.renderers import render_layered_short_read_telegram_html

        situation_items = [
            DigestSituationItemDraft(
                group_id="situation:water:avail",
                label="Водоснабжение",
                body="Азмол: нет воды; верхние этажи: слабое давление.",
                cited_support_ids=("ref-w-1",),
            ),
        ]
        rubric_blocks = [
            DigestNarrativeBlockDraft(
                block_id="block:utilities:0",
                items=(
                    DigestEditorialItemDraft(
                        headline="Ремонт сетей на востоке города",
                        body="Бригады завершают замену кабеля.",
                        cited_support_ids=("ref-1",),
                        covered_story_ids=("story:1",),
                    ),
                ),
            ),
        ]

        html = render_layered_short_read_telegram_html(
            edition_name="Бердянск",
            snapshot_at=_NOW,
            situation_items=situation_items,
            rubric_blocks=rubric_blocks,
            statistics_text="📊 Статистика: обработано 12 каналов",
        )

        assert "<b>Дайджест: Бердянск · 22.08.2026</b>" in html
        assert "<b>🏙 Городская обстановка</b>" in html
        assert "• <b>Водоснабжение</b>: Азмол: нет воды; верхние этажи: слабое давление." in html
        assert "<b>⚡️ Коммунальная обстановка</b>" in html
        assert "• <b>Ремонт сетей на востоке города</b>: Бригады завершают замену кабеля." in html
        assert "<i>📊 Статистика: обработано 12 каналов</i>" in html

    def test_render_city_situation_uses_planned_label_and_mixed_state_icon(self):
        from src.publication.digest_presentation import (
            CitySituationPresentationGroup,
            CitySituationPresentationPlan,
            render_city_situation_presentation,
        )

        plan = CitySituationPresentationPlan(
            groups=(
                CitySituationPresentationGroup(
                    group_id="situation:banking_cash:availability",
                    group_kind="subject_status",
                    subject_key="banking_cash",
                    subject_label="Банковские услуги и наличные",
                    state="CONFLICTING",
                    source_refs=("ref-a", "ref-b"),
                    detail_lines=("Гора: нет связи", "Залив: банкомат выдает наличные"),
                ),
            ),
            covered_source_refs=("ref-a", "ref-b"),
        )

        rendered = render_city_situation_presentation(plan, use_emojis=True)
        assert "🟡 **Банковские услуги и наличные**" in rendered
        assert "Гора: нет связи" in rendered
        assert "Залив: банкомат выдает наличные" in rendered

    def test_render_grouped_digest_prefers_presentation_plan_over_llm_situation_items(self):
        from src.publication.digest_narrative import (
            DigestNarrativeDraft,
            DigestSituationItemDraft,
        )
        from src.publication.digest_presentation import (
            CitySituationPresentationGroup,
            CitySituationPresentationPlan,
            DigestPresentationPlan,
        )

        card = StoryCard(
            id="story:1",
            topic="Электричество",
            importance="high",
            summary="Отключения",
        )
        frozen = FrozenEditorialInput(
            analysis=EditorialAnalysis(cards=[card]),
            writer_bundle=PreparedBundle(
                records={}, prompt_text="", total_messages=0, candidate_count=1
            ),
        )
        llm_draft = DigestNarrativeDraft(
            blocks=(),
            situation_items=(
                DigestSituationItemDraft(
                    group_id="situation:power:avail",
                    label="LLM Inappropriate Label",
                    body="LLM body text.",
                    cited_support_ids=("ref-1",),
                ),
            ),
        )
        plan = DigestPresentationPlan(
            city_situation=CitySituationPresentationPlan(
                groups=(
                    CitySituationPresentationGroup(
                        group_id="situation:power:avail",
                        group_kind="subject_status",
                        subject_key="power",
                        subject_label="Электроснабжение (Плановое)",
                        state="DISRUPTED",
                        source_refs=("ref-1",),
                        detail_lines=("Центр: отключение света",),
                    ),
                ),
                covered_source_refs=("ref-1",),
            ),
            detail_story_ids=(),
            story_hints=(),
        )
        renderer = PublicationDigestRenderer(use_emojis=True)
        _, _, body = renderer.render_grouped_digest(
            frozen,
            narrative_draft=llm_draft,
            presentation_plan=plan,
        )
        assert "Электроснабжение (Плановое)" in body
        assert "LLM Inappropriate Label" not in body

    def test_render_grouped_digest_consumes_deterministic_draft_without_special_branch(self):
        from src.publication.digest_narrative import (
            build_deterministic_digest_draft,
        )
        from src.publication.digest_presentation import (
            CitySituationPresentationGroup,
            CitySituationPresentationPlan,
            DigestPresentationPlan,
            DigestStoryPresentation,
        )
        from src.publication.evidence import PublicationEvidence

        now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
        renderer = PublicationDigestRenderer(use_emojis=True)

        card_1 = StoryCard(
            id="story:1",
            topic="Электричество",
            importance="high",
            summary="Отключения",
            rubric_id="utilities",
        )
        card_2 = StoryCard(
            id="story:2",
            topic="Спорт",
            importance="medium",
            summary="Бесплатный набор",
            rubric_id="society",
        )
        card_3 = StoryCard(
            id="story:3",
            topic="Водоснабжение",
            importance="high",
            summary="Воды нет, жильцы скидываются",
            rubric_id="utilities",
        )

        evi_1 = PublicationEvidence(
            evidence_id="sup:1:dash",
            story_id=1,
            text="Света нет в центре",
            source_text="Света нет в центре",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=1,
            source_ref="ref-1",
            source_id=1,
            source_item_id=1,
            source_role="official",
            observed_at=now,
        )
        evi_2 = PublicationEvidence(
            evidence_id="sup:2:detail",
            story_id=2,
            text="Открыт бесплатный набор детей на футбол",
            source_text="Открыт бесплатный набор детей на футбол",
            kind="community_report",
            publication_use="PUBLISH",
            fragment_id=2,
            source_ref="ref-2",
            source_id=2,
            source_item_id=2,
            source_role="community",
            observed_at=now,
        )
        evi_3_dash = PublicationEvidence(
            evidence_id="sup:3:dash",
            story_id=3,
            text="Воды нет в районе",
            source_text="Воды нет в районе",
            kind="service_access",
            publication_use="PUBLISH",
            fragment_id=3,
            source_ref="ref-3",
            source_id=3,
            source_item_id=3,
            source_role="official",
            observed_at=now,
        )
        evi_3_detail = PublicationEvidence(
            evidence_id="sup:3:detail",
            story_id=3,
            text="Жильцы дома скинулись по 300 рублей на подвоз воды",
            source_text="Жильцы дома скинулись по 300 рублей на подвоз воды",
            kind="community_report",
            publication_use="PUBLISH",
            fragment_id=4,
            source_ref="ref-4",
            source_id=3,
            source_item_id=4,
            source_role="community",
            observed_at=now,
        )

        evidence_dict = {
            "sup:1:dash": evi_1,
            "sup:2:detail": evi_2,
            "sup:3:dash": evi_3_dash,
            "sup:3:detail": evi_3_detail,
        }

        sit_group = CitySituationPresentationGroup(
            group_id="sit:1",
            group_kind="subject_status",
            subject_key="power",
            subject_label="Электроснабжение",
            state="UNAVAILABLE",
            source_refs=("ref-1", "ref-3"),
            detail_lines=("Света нет в центре", "Воды нет в районе"),
            covered_story_ids=("story:1", "story:3"),
            cited_support_ids=("sup:1:dash", "sup:3:dash"),
        )

        plan = DigestPresentationPlan(
            city_situation=CitySituationPresentationPlan(
                groups=(sit_group,),
                covered_source_refs=("ref-1", "ref-3"),
            ),
            story_presentations=(
                DigestStoryPresentation(
                    story_id="story:1",
                    mode="DASHBOARD_ONLY",
                    city_situation_group_ids=("sit:1",),
                    detail_support_ids=(),
                    merge_group_id="story:1",
                ),
                DigestStoryPresentation(
                    story_id="story:2",
                    mode="DETAIL_ONLY",
                    city_situation_group_ids=(),
                    detail_support_ids=("sup:2:detail",),
                    merge_group_id="story:2",
                ),
                DigestStoryPresentation(
                    story_id="story:3",
                    mode="DASHBOARD_AND_DRILLDOWN",
                    city_situation_group_ids=("sit:1",),
                    detail_support_ids=("sup:3:detail",),
                    merge_group_id="story:3",
                ),
            ),
        )

        frozen = FrozenEditorialInput(
            analysis=EditorialAnalysis(cards=[card_1, card_2, card_3]),
            writer_bundle=PreparedBundle(
                records={}, prompt_text="", total_messages=0, candidate_count=3
            ),
        )

        deterministic_draft = build_deterministic_digest_draft(
            cards=[card_1, card_2, card_3],
            evidence=evidence_dict,
            rubrics=renderer.rubrics,
            presentation_plan=plan,
        )

        title, lead, body = renderer.render_grouped_digest(
            frozen,
            snapshot_at=now,
            narrative_draft=deterministic_draft,
            presentation_plan=plan,
        )

        # Story 1 appears in City Situation, not as thematic item
        assert "Электроснабжение" in body
        assert "Света нет в центре" in body

        # Story 2 and 3 appear in thematic sections
        assert "Спорт" in body or "Открыт бесплатный набор детей на футбол" in body
        assert "жильцы дома скинулись по 300 рублей на подвоз воды" in body
