# ruff: noqa: S101
"""Explicit source relationships must survive extraction as evidence."""

from __future__ import annotations

import pytest

from src.domain.event_payload import EventPayload, EvidenceItemPayload
from src.processing.operational_semantics import normalize_service_state_evidence


def normalize(source: str, claim: str, parent: str = ""):
    payload = EventPayload(
        evidence_items=(
            EvidenceItemPayload(
                text=claim,
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(1,),
            ),
        )
    )
    return normalize_service_state_evidence(
        payload,
        {1: source},
        reply_parent_context_by_fragment_id={1: parent},
    )


@pytest.mark.parametrize(
    "claim",
    [
        "По сообщению жителя, отключение света сохраняется на улице Павлова.",
        "По Павлова света нет.",
    ],
)
def test_positive_continuity_reply_cannot_be_extracted_as_outage(claim):
    payload, audit = normalize(
        "По Павлова вроде как был так и остался и ещё где то появился",
        claim,
        "На восьмухе свет не появился?",
    )
    assert payload.evidence_items[0].publication_use == "CONTEXT"
    assert audit.rejection_reasons == ("source_availability_conflict",)


def test_positive_continuity_reply_remains_publishable_with_faithful_claim():
    payload, audit = normalize(
        "По Павлова вроде как был так и остался и ещё где то появился",
        "По сообщению жителя, на Павлова свет сохранился.",
        "На восьмухе свет не появился?",
    )
    assert payload.evidence_items[0].publication_use == "PUBLISH"
    assert audit.rejected_count == 0


def test_lowercase_local_reply_preserves_its_own_weekday_duration():
    payload, audit = normalize(
        "Да на вроцлавской на ртс тоже с воскресенья нет",
        "По сообщению жителя, на улице Вроцлавской (РТС) света нет с воскресенья.",
        "Не всем включали,мы РТС с воскресенья без света,хотя соседние улицы есть",
    )
    assert payload.evidence_items[0].publication_use == "PUBLISH"
    assert audit.rejected_count == 0


def test_reply_cannot_borrow_parents_weekday_instead_of_its_own():
    payload, audit = normalize(
        "Да на вроцлавской на ртс тоже с пятницы нет",
        "По сообщению жителя, на улице Вроцлавской (РТС) света нет с воскресенья.",
        "Мы РТС с воскресенья без света",
    )
    assert payload.evidence_items[0].publication_use == "CONTEXT"
    assert audit.rejected_count == 1


def test_numeric_voltage_report_names_power_without_word_light():
    payload, audit = normalize(
        "На Жуковского сегодня подали с напряжением 167",
        "По сообщению жителя, на Жуковского сегодня подали электричество с напряжением 167.",
    )
    assert payload.evidence_items[0].publication_use == "PUBLISH"
    assert audit.rejected_count == 0


def test_emotional_tension_does_not_establish_electricity_supply():
    payload, audit = normalize(
        "На Жуковского сегодня напряжение в отношениях",
        "По сообщению жителя, на Жуковского сегодня подали электричество.",
    )
    assert payload.evidence_items[0].publication_use == "CONTEXT"
    assert audit.rejected_count == 1


def test_duration_of_availability_cannot_be_changed_to_frequency_within_period():
    payload, audit = normalize(
        "Слободка 1 раз кажется тоже со светом побыла 3 дня",
        "По сообщению жителя, на Слободке свет появился один раз в течение трёх дней.",
    )
    assert payload.evidence_items[0].publication_use == "CONTEXT"
    assert audit.rejection_reasons == ("source_duration_relation_conflict",)


def test_duration_of_availability_remains_publishable():
    payload, audit = normalize(
        "Слободка 1 раз кажется тоже со светом побыла 3 дня",
        "По сообщению жителя, на Слободке свет однажды был три дня.",
    )
    assert payload.evidence_items[0].publication_use == "PUBLISH"
    assert audit.rejected_count == 0


def test_explicit_source_period_and_frequency_remain_publishable():
    payload, audit = normalize(
        "На Слободке свет появился один раз в течение трёх дней.",
        "На Слободке свет появился один раз в течение трёх дней.",
    )
    assert payload.evidence_items[0].publication_use == "PUBLISH"
    assert audit.rejected_count == 0


def test_elliptical_local_supply_interval_is_publishable_with_unique_service_parent():
    payload, audit = normalize(
        "На Тверской один раз в неделю на сутки.",
        "По сообщению жителя, на Тверской свет дают один раз в неделю на сутки.",
        "Как часто на Тверской дают свет?",
    )
    assert payload.evidence_items[0].publication_use == "PUBLISH"
    assert audit.rejected_count == 0


def test_elliptical_supply_interval_cannot_choose_between_two_parent_services():
    payload, _ = normalize(
        "На Тверской один раз в неделю на сутки.",
        "На Тверской свет дают один раз в неделю на сутки.",
        "Как часто вам дают воду и электричество?",
    )
    assert payload.evidence_items[0].publication_use == "CONTEXT"


def test_elliptical_interval_does_not_select_state_from_mixed_parent():
    payload, _ = normalize(
        "На Тверской один раз в неделю на сутки.",
        "На Тверской свет отключают один раз в неделю на сутки.",
        "Вы рассказывайте правильно. Один дом со светом в центре, другой нет",
    )
    assert payload.evidence_items[0].publication_use == "CONTEXT"


def test_elliptical_answer_keeps_the_question_action_instead_of_inverting_it():
    payload, _ = normalize(
        "На Тверской один раз в неделю на сутки.",
        "На Тверской свет отключают один раз в неделю на сутки.",
        "Как часто на Тверской дают свет?",
    )
    assert payload.evidence_items[0].publication_use == "CONTEXT"
