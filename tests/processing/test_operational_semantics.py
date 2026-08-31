from __future__ import annotations

import datetime as dt

from src.domain.event_payload import (
    EventPayload,
    EvidenceItemPayload,
    OperationalObservationPayload,
)
from src.processing.operational_semantics import (
    normalize_operational_payload,
)

_NOW = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)


def test_drops_observation_supported_only_by_coping_report():
    evidence = [
        EvidenceItemPayload(
            text="Жители скинулись по 300 рублей на генератор.",
            kind="community_report",
            publication_use="PUBLISH",
            source_fragment_ids=(1,),
        )
    ]
    observations = [
        OperationalObservationPayload(
            subject_key="generator_use",
            subject_label="Использование генератора",
            dimension="availability",
            location="",
            entity="",
            state="AVAILABLE",
            detail="Генератор работает",
            source_fragment_ids=(1,),
        )
    ]
    payload = EventPayload(
        topic="Генераторы",
        headline="Тест",
        digest_summary="Тест",
        evidence_items=tuple(evidence),
        operational_observations=tuple(observations),
    )

    normalized, audit = normalize_operational_payload(payload)

    assert normalized.operational_observations == ()
    assert audit.dropped_observation_count == 1
    assert audit.dropped_observation_subject_keys == ("generator_use",)


def test_keeps_observation_supported_by_publish_service_access():
    evidence = [
        EvidenceItemPayload(
            text="Вода на верхние этажи не поступает.",
            kind="service_access",
            publication_use="PUBLISH",
            source_fragment_ids=(2,),
        )
    ]
    observations = [
        OperationalObservationPayload(
            subject_key="water_supply",
            subject_label="Водоснабжение",
            dimension="availability",
            location="",
            entity="",
            state="UNAVAILABLE",
            detail="На верхних этажах сухо",
            source_fragment_ids=(2,),
        )
    ]
    payload = EventPayload(
        topic="Водоснабжение",
        headline="Тест",
        digest_summary="Тест",
        evidence_items=tuple(evidence),
        operational_observations=tuple(observations),
    )

    normalized, audit = normalize_operational_payload(payload)

    assert len(normalized.operational_observations) == 1
    assert normalized.operational_observations[0].subject_key == "water_supply"
    assert audit.dropped_observation_count == 0
    assert audit.uncovered_service_access_fragment_ids == ()


def test_reports_service_access_without_projection_but_does_not_invent():
    evidence = [
        EvidenceItemPayload(
            text="Автобус №4 ходит по расписанию.",
            kind="service_access",
            publication_use="PUBLISH",
            source_fragment_ids=(3,),
        )
    ]
    payload = EventPayload(
        topic="Транспорт",
        headline="Тест",
        digest_summary="Тест",
        evidence_items=tuple(evidence),
        operational_observations=(),
    )

    normalized, audit = normalize_operational_payload(payload)

    assert normalized.operational_observations == ()
    assert audit.uncovered_service_access_fragment_ids == (3,)


def test_question_context_cannot_support_operational_observation():
    evidence = [
        EvidenceItemPayload(
            text="Работает ли банк?",
            kind="resident_question",
            publication_use="CONTEXT",
            source_fragment_ids=(4,),
        )
    ]
    observations = [
        OperationalObservationPayload(
            subject_key="banking",
            subject_label="Банки",
            dimension="availability",
            location="",
            entity="",
            state="UNKNOWN",
            detail="Неизвестно",
            source_fragment_ids=(4,),
        )
    ]
    payload = EventPayload(
        topic="Банки",
        headline="Тест",
        digest_summary="Тест",
        evidence_items=tuple(evidence),
        operational_observations=tuple(observations),
    )

    normalized, audit = normalize_operational_payload(payload)

    assert normalized.operational_observations == ()
    assert audit.dropped_observation_count == 1


def test_legacy_payload_without_evidence_preserves_observations():
    observations = [
        OperationalObservationPayload(
            subject_key="power_supply",
            subject_label="Электроснабжение",
            dimension="availability",
            location="",
            entity="",
            state="UNAVAILABLE",
            detail="Света нет",
            source_fragment_ids=(5,),
        )
    ]
    payload = EventPayload(
        topic="Свет",
        headline="Тест",
        digest_summary="Тест",
        evidence_items=(),
        operational_observations=tuple(observations),
    )

    normalized, audit = normalize_operational_payload(payload)

    assert len(normalized.operational_observations) == 1
    assert audit.dropped_observation_count == 0
