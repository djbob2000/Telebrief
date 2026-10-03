# ruff: noqa: S101
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

from src.editorial_models import EditorialAnalysis, StoryCard, StoryElement
from src.publication.digest_source_material import project_digest_source_material
from src.publication.evidence import PublicationEvidence

_NOW = dt.datetime(2026, 10, 3, 13, tzinfo=dt.timezone.utc)
_PENSION_AD = """Помощь с пенсиями, картами и банками
Нужна помощь с верификацией? Решаем сложные вопросы быстро и конфиденциально.
Перевод выплат: смена банка для получения пенсии, если старый личный кабинет заблокирован.
Контакты для связи: телефон.
"""
_WATCH_AD = """РЕМОНТ ЧАСОВ
Замена элементов питания. Покупаем коллекционные часы.
Универмаг «Южный», 2 этаж
Время работы: с 9:00 до 14:00
Телефон для связи.
"""
_COURSE_AD = """Бердянский клуб ЗРОО ДОСААФ
СОБАКА НЕ СЛУШАЕТСЯ? ИСПРАВИМ.
ОТКРЫТ НАБОР НА ДРЕССИРОВКУ ОКД
Групповые занятия. Небольшие группы — до 7 человек.
Расписание:
Пн / Ср / Пт — 15:00
Сб / Вс — 16:00
Занятия до конца декабря — при благоприятной погоде.
Площадка ДОСААФ, пр. Азовский, 2-А
Телефон для записи: +7 999 123 45 67
Не ждите, начните работать сейчас.
"""


def _evidence(
    story_id: int, text: str, raw: str, *, fragment_id: int = 1, revision_id: int = 101
) -> PublicationEvidence:
    return PublicationEvidence(
        evidence_id=f"story:{story_id}:evidence:0:frag:{fragment_id}",
        story_id=story_id,
        text=text,
        source_text=text,
        kind="community_report",
        publication_use="PUBLISH",
        fragment_id=fragment_id,
        source_ref=f"telegram:source:7:item:10:rev:{revision_id}:frag:{fragment_id}",
        source_id=7,
        source_item_id=10,
        source_role="community",
        observed_at=_NOW,
        source_item_revision_id=revision_id,
        source_item_context_text=raw,
    )


def _analysis(*evidence: PublicationEvidence) -> EditorialAnalysis:
    cards = []
    for sid in dict.fromkeys(e.story_id for e in evidence):
        items = [e for e in evidence if e.story_id == sid]
        cards.append(
            StoryCard(
                id=f"story:{sid}",
                topic=items[0].text,
                importance="medium",
                summary=" ".join(e.text for e in items),
                representative_source_refs=[e.source_ref for e in items],
                community_observations=[
                    StoryElement(text=e.text, source_refs=[e.source_ref]) for e in items
                ],
            )
        )
    return EditorialAnalysis(cards=cards, evidence={e.evidence_id: e for e in evidence})


def test_private_pension_and_watch_fragments_do_not_become_digest_stories() -> None:
    pension = _evidence(
        1857,
        "Перевод выплат: смена банка для получения пенсии при блокировке кабинета.",
        _PENSION_AD,
    )
    watch = _evidence(18051, "Универмаг «Южный», 2 этаж: время работы с 9:00 до 14:00.", _WATCH_AD)
    original = _analysis(pension, watch)

    projected = project_digest_source_material(original)

    assert projected.cards == []
    assert all(e.publication_use == "CONTEXT" for e in projected.evidence.values())
    assert len(original.cards) == 2
    assert original.evidence[pension.evidence_id].publication_use == "PUBLISH"
    assert original.evidence[watch.evidence_id].source_item_context_text == _WATCH_AD


def test_course_ad_fragments_keep_one_substantive_story_and_exact_practical_details() -> None:
    teaser = _evidence(
        35681, "В Бердянске предлагается услуга по коррекции поведения собак.", _COURSE_AD
    )
    course = _evidence(
        35682,
        "Бердянский клуб ДОСААФ объявил набор на дрессировку ОКД.",
        _COURSE_AD,
        fragment_id=2,
    )

    projected = project_digest_source_material(_analysis(teaser, course))

    assert [c.id for c in projected.cards] == ["story:35682"]
    details = [
        e
        for e in projected.evidence.values()
        if e.story_id == 35682
        and e.publication_use == "PUBLISH"
        and e.evidence_id != course.evidence_id
    ]
    assert len(details) == 1
    assert "Пн / Ср / Пт — 15:00" in details[0].text
    assert "Сб / Вс — 16:00" in details[0].text
    assert "Азовский, 2-А" in details[0].text
    assert "при благоприятной погоде" in details[0].text
    assert "999" not in details[0].text
    assert "начните работать" not in details[0].text
    assert details[0].source_item_revision_id == 101
    assert details[0].source_item_context_text == _COURSE_AD
    assert details[0].source_scope == "source_item_revision"


def test_supplied_course_details_replace_the_stale_no_details_summary() -> None:
    course = _evidence(35682, "ДОСААФ объявил набор на дрессировку ОКД.", _COURSE_AD)
    analysis = _analysis(course)
    analysis.cards[0].summary += " Детали (место, время, стоимость) в сообщении не указаны."
    projected = project_digest_source_material(analysis)
    assert "не указаны" not in projected.cards[0].summary
    assert "15:00" in projected.cards[0].useful_details[0].text


def test_private_reply_does_not_remove_another_legitimate_single_source_report() -> None:
    ad = _evidence(10, "Перевод выплат: смена банка для получения пенсии.", _PENSION_AD)
    water = _evidence(
        10,
        "На верхних этажах воду дают только ночью раз в 5–6 дней.",
        "У нас на верхних этажах воду дают только ночью раз в 5–6 дней.",
        fragment_id=2,
        revision_id=102,
    )

    projected = project_digest_source_material(_analysis(ad, water))

    assert [c.id for c in projected.cards] == ["story:10"]
    assert projected.cards[0].summary == water.text
    assert projected.cards[0].topic == water.text
    assert projected.cards[0].representative_source_refs == [water.source_ref]
    assert projected.evidence[water.evidence_id] == water


def test_source_context_does_not_ban_bank_hours_or_community_microdetails() -> None:
    bank = _evidence(
        1, "Отделение банка принимает посетителей с 9:00 до 13:00.", "Расписание банка."
    )
    coping = _evidence(
        2,
        "Жильцы скинулись по 300 рублей на генератор для подачи воды.",
        "Жильцы скинулись по 300 рублей на генератор для подачи воды.",
    )
    original = _analysis(bank, coping)
    assert project_digest_source_material(original) == original


def test_source_context_never_uses_reply_parent_or_a_different_revision() -> None:
    course = _evidence(3, "В клубе открыт набор на дрессировку ОКД.", "В клубе открыт набор.")
    course = replace(course, reply_parent_context_text=_COURSE_AD, reply_parent_item_id=20)
    assert len(project_digest_source_material(_analysis(course)).evidence) == 1
    mismatched = replace(course, source_item_context_text=_COURSE_AD, source_item_revision_id=999)
    assert len(project_digest_source_material(_analysis(mismatched)).evidence) == 1


def test_digest_projection_is_idempotent() -> None:
    course = _evidence(3, "В клубе открыт набор на дрессировку ОКД.", _COURSE_AD)
    projected = project_digest_source_material(_analysis(course))
    assert project_digest_source_material(projected) == projected


def test_a_concrete_service_change_survives_in_a_promotional_source() -> None:
    report = _evidence(
        4,
        "В мастерской после аварии нет света, приём заказов отменили.",
        _WATCH_AD + "В мастерской после аварии нет света, приём заказов отменили.",
    )
    original = _analysis(report)
    assert project_digest_source_material(original) == original


def test_unrelated_water_fact_in_the_same_ad_message_survives() -> None:
    water_text = "Вода подаётся на верхние этажи только ночью и раз в 5–6 дней."
    water = _evidence(4, water_text, _WATCH_AD + water_text)
    original = _analysis(water)
    assert project_digest_source_material(original) == original


def test_adapter_loads_own_frozen_revision_and_projects_only_digest_material() -> None:
    from src.publication.event_editorial_adapter import EventEditorialAdapter
    from src.publication.models import PublicationInput

    fragment = "Универмаг «Южный», 2 этаж. Время работы: с 9:00 до 14:00."
    payload = {
        "headline": "Универмаг «Южный» работает с 9:00 до 14:00",
        "digest_summary": fragment,
        "publishability": "news",
        "evidence_items": [
            {
                "kind": "community_report",
                "publication_use": "PUBLISH",
                "text": "Мастерская в универмаге «Южный» работает с 9:00 до 14:00.",
                "source_fragment_ids": [7],
            }
        ],
    }

    class Cursor:
        def __init__(self, rows):
            self.rows = rows

        async def fetchone(self):
            return self.rows[0] if self.rows else None

        async def fetchall(self):
            return self.rows

    class Connection:
        async def execute(self, query, params):
            if "FROM story_revisions" in query:
                return Cursor([(90, payload["headline"], fragment, fragment, payload, _NOW, _NOW)])
            if "FROM source_fragments" in query:
                assert "sir.text_content" in query
                return Cursor(
                    [
                        (
                            7,
                            fragment,
                            1,
                            "telegram",
                            "Город",
                            "community",
                            "",
                            "city",
                            10,
                            101,
                            "",
                            "Житель",
                            _NOW,
                            None,
                            _WATCH_AD,
                        )
                    ]
                )
            raise AssertionError(query)

    class Repository:
        def __init__(self, publication_type):
            self.publication_type = publication_type

        async def get_run_by_id(self, conn, run_id):
            return SimpleNamespace(publication_type=self.publication_type, snapshot_at=_NOW)

    async def adapt(publication_type):
        adapter = EventEditorialAdapter(uow=None, repo=Repository(publication_type))
        return await adapter.adapt_inputs_on(
            Connection(),
            274,
            inputs=[PublicationInput(1, 274, 18051, 90, 1, "brief", 1, _NOW, fragment_ids=[7])],
        )

    frozen_digest = asyncio.run(adapt("digest_grouped"))
    assert frozen_digest.analysis.cards == []
    evidence = next(iter(frozen_digest.analysis.evidence.values()))
    assert evidence.source_item_context_text == _WATCH_AD
    assert evidence.source_item_revision_id == 101
    assert evidence.source_text == fragment
    assert evidence.source_ref == "telegram:source:1:item:10:rev:101:frag:7"
    assert frozen_digest.writer_bundle.records[evidence.source_ref].message.text == fragment
    assert evidence.publication_use == "CONTEXT"
    assert frozen_digest.writer_bundle.candidate_count == 0

    # A non-digest adaptation still carries the unchanged fragment evidence.
    frozen_other = asyncio.run(adapt("test_unprojected"))
    assert len(frozen_other.analysis.cards) == 1
    assert next(iter(frozen_other.analysis.evidence.values())).publication_use == "PUBLISH"


def test_projected_material_reaches_composition_validation_and_telegram_rendering() -> None:
    from src.editorial_models import PreparedBundle
    from src.publication.digest_composition import build_digest_composition
    from src.publication.digest_narrative import (
        _composition_narrative_plan,
        _parse_composition_writer_output,
        build_digest_support_text_index,
        validate_digest_narrative,
    )
    from src.publication.digest_presentation import build_digest_presentation_plan
    from src.publication.editorial_adapter import FrozenEditorialInput
    from src.publication.renderers import PublicationDigestRenderer

    water_text = "На верхних этажах воду дают только ночью раз в 5–6 дней."
    water = _evidence(35707, water_text, water_text, fragment_id=3, revision_id=102)
    course = _evidence(
        35682, "Бердянский клуб ДОСААФ объявил набор на дрессировку ОКД.", _COURSE_AD
    )
    watch = _evidence(18051, "Время работы: с 9:00 до 14:00.", _WATCH_AD, fragment_id=4)
    pension = _evidence(
        1857, "Перевод выплат: смена банка для получения пенсии.", _PENSION_AD, fragment_id=5
    )
    projected = project_digest_source_material(_analysis(water, course, watch, pension))
    for card in projected.cards:
        card.rubric_id = "education_culture" if card.id == "story:35682" else "infrastructure"
    rubrics = (
        {"id": "infrastructure", "title": "Коммунальная обстановка"},
        {"id": "education_culture", "title": "Образование и культура"},
    )
    plan = build_digest_presentation_plan(
        cards=projected.cards, evidence=projected.evidence, include_all_candidates=True
    )
    composition = build_digest_composition(
        plan,
        projected.cards,
        projected.evidence,
        edition_slug="berdyansk",
        snapshot_at=_NOW,
        max_chars=3900,
        reserved_chars=128,
        include_statistics=False,
        rubric_labels={r["id"]: r["title"] for r in rubrics},
    )
    assert composition.feasible
    assert composition.admitted_story_ids == {"story:35707", "story:35682"}
    narrative_plan = _composition_narrative_plan(
        cards=projected.cards, rubrics=rubrics, presentation_plan=plan.with_composition(composition)
    )
    course_body = (
        "Бердянский клуб ЗРОО ДОСААФ объявил набор на дрессировку ОКД. "
        "Занятия на площадке ДОСААФ, пр. Азовский, 2-А: Пн / Ср / Пт — 15:00, "
        "Сб / Вс — 16:00. Занятия до конца декабря — при благоприятной погоде."
    )
    # Simulated writer response exercises the production parser and validator;
    # it is never used as a publication fallback.
    raw = {
        "blocks": [
            {
                "block_id": block.block_id,
                "items": [
                    {
                        "composition_unit_ids": [u.unit_id for u in block.composition_units],
                        "covered_fact_ids": [r.fact_id for r in block.composition_fact_records],
                        "headline": "",
                        "body": (
                            course_body
                            if block.rubric_id == "education_culture"
                            else "По словам жителя, " + water_text[:1].lower() + water_text[1:]
                        ),
                        "emoji": "🐕" if block.rubric_id == "education_culture" else "💧",
                        "claims": [
                            {"text": r.text, "covered_fact_ids": [r.fact_id]}
                            for r in block.composition_fact_records
                        ],
                    }
                ],
            }
            for block in narrative_plan.blocks
        ]
    }
    draft = _parse_composition_writer_output(raw, plan=narrative_plan)
    support_index = build_digest_support_text_index(
        evidence=projected.evidence, cards=projected.cards
    )
    validation = validate_digest_narrative(draft, narrative_plan, support_index)
    assert validation.is_valid, validation.violations
    assert len(draft.blocks) == 2
    assert all(len(block.items) == 1 for block in draft.blocks)
    frozen = FrozenEditorialInput(
        analysis=projected,
        writer_bundle=PreparedBundle(
            records={}, prompt_text="", total_messages=0, candidate_count=len(projected.cards)
        ),
        run_id=274,
    )
    renderer = PublicationDigestRenderer()
    _, lead, body = renderer.render_grouped_digest(
        frozen,
        snapshot_at=_NOW,
        narrative_draft=draft,
        presentation_plan=plan.with_composition(composition),
    )
    assert lead == ""
    assert "5–6 дней" in body and "15:00" in body and "16:00" in body and "Азовский" in body
    assert "Южный" not in body and "пенсии" not in body and "999" not in body
    assert len(body) < 4096

    course_block = next(b for b in draft.blocks if "education_culture" in b.block_id)
    bad_item = replace(course_block.items[0], body=course_body.replace("15:00", "19:00"))
    bad_draft = replace(
        draft,
        blocks=tuple(
            replace(b, items=(bad_item,)) if b is course_block else b for b in draft.blocks
        ),
    )
    assert not validate_digest_narrative(bad_draft, narrative_plan, support_index).is_valid
