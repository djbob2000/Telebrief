"""Unit tests for story quality validation and publication eligibility."""

from src.domain.event_payload import EventPayload, EvidenceItemPayload
from src.domain.service_state import ServiceStatePayload
from src.publication.story_quality import (
    has_meaningful_predicate,
    is_generic_service_entity,
    validate_story_publication_eligibility,
)


def test_is_generic_service_entity():
    assert is_generic_service_entity("проблемный сервис") is True
    assert is_generic_service_entity("сервис") is True
    assert is_generic_service_entity("услуга") is True
    assert is_generic_service_entity("приложение") is True
    assert is_generic_service_entity("некий сервис") is True
    assert is_generic_service_entity("") is True

    # Real named entities
    assert is_generic_service_entity("Юпитер") is False
    assert is_generic_service_entity("Промсвязьбанк") is False
    assert is_generic_service_entity("Водоканал") is False
    assert is_generic_service_entity("Бердянскводоканал") is False
    assert is_generic_service_entity("МирТелеком") is False


def test_has_meaningful_predicate():
    # Pure geographic prepositional fragments without action or state
    assert has_meaningful_predicate("В районе Водоканала на Пролетарском") is False
    assert has_meaningful_predicate("на Пролетарской вблизи Водоканала") is False
    assert has_meaningful_predicate("В районе Слободки") is False

    # Chat meta / chatter without civic event
    assert (
        has_meaningful_predicate(
            "В соцсетях жители обсуждают текущую ситуацию на Пролетарской вблизи Водоканала. Подробности уточняются."
        )
        is False
    )
    assert (
        has_meaningful_predicate(
            "Жители интересуются ситуацией в центре города. Информация уточняется."
        )
        is False
    )

    # Legitimate civic events with verbs, states, changes, or consequences
    assert has_meaningful_predicate("На Пролетарской возле Водоканала прорвало трубу") is True
    assert has_meaningful_predicate("Свет отключили в нагорной части города и на Слободке") is True
    assert (
        has_meaningful_predicate(
            "В местных чатах подтверждают, что электричество вернулось на улицу Петровского"
        )
        is True
    )
    assert (
        has_meaningful_predicate("В центре города зафиксировано низкое напряжение — около 170 В")
        is True
    )

    # A publication label such as "Сообщение о ситуации" is metadata, not an event.
    assert has_meaningful_predicate("Сообщение о ситуации в районе Водоканала") is False


def test_validate_story_publication_eligibility_rejects_resident_question_only():
    payload = EventPayload(
        headline="Вопрос о водоканале",
        evidence_items=(
            EvidenceItemPayload(
                text="Работает ли сегодня водоканал?",
                kind="resident_question",
                publication_use="PUBLISH",
                source_fragment_ids=(1,),
            ),
        ),
    )
    is_valid, reason = validate_story_publication_eligibility(payload)
    assert is_valid is False
    assert reason == "resident_question_only"


def test_validate_story_publication_eligibility_rejects_generic_service():
    payload = EventPayload(
        headline="Сервис снова доступен",
        digest_summary="Несколько горожан сообщают, что проблемный сервис восстановил работу после перебоев.",
        evidence_items=(
            EvidenceItemPayload(
                text="Несколько горожан сообщают, что проблемный сервис восстановил работу после перебоев.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(2,),
                service_state=ServiceStatePayload(
                    subject_key="service",
                    subject_label="Сервис",
                    dimension="access",
                    state="AVAILABLE",
                    entity="проблемный сервис",
                ),
            ),
        ),
    )
    is_valid, reason = validate_story_publication_eligibility(payload)
    assert is_valid is False
    assert reason == "service_access_without_concrete_entity"


def test_validate_story_publication_eligibility_accepts_named_service():
    payload = EventPayload(
        headline="Сервис Юпитер снова доступен",
        digest_summary="Пользователи сообщают, что сервис Юпитер снова работает.",
        evidence_items=(
            EvidenceItemPayload(
                text="Пользователи сообщают, что сервис Юпитер снова работает.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(6,),
                service_state=ServiceStatePayload(
                    subject_key="service",
                    subject_label="Сервис",
                    dimension="access",
                    state="AVAILABLE",
                    entity="Юпитер",
                ),
            ),
        ),
    )
    is_valid, reason = validate_story_publication_eligibility(payload)
    assert is_valid is True
    assert reason is None


def test_validate_story_publication_eligibility_rejects_predicateless_fragment():
    payload = EventPayload(
        headline="В районе Водоканала",
        digest_summary="В соцсетях жители обсуждают текущую ситуацию на Пролетарской вблизи Водоканала. Подробности уточняются.",
        evidence_items=(
            EvidenceItemPayload(
                text="В соцсетях жители обсуждают текущую ситуацию на Пролетарской вблизи Водоканала. Подробности уточняются.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(3,),
            ),
        ),
    )
    is_valid, reason = validate_story_publication_eligibility(payload)
    assert is_valid is False
    assert reason == "lacks_meaningful_predicate"


def test_validate_story_publication_eligibility_rejects_generic_service_community_report():
    """The run-44 service card was persisted as a community report, not service_access."""
    payload = EventPayload(
        headline="Жители Бердянска подтверждают, что сервис работает",
        digest_summary="Несколько жителей Бердянска подтверждают, что сервис работает.",
        evidence_items=(
            EvidenceItemPayload(
                text="Несколько жителей Бердянска подтверждают, что сервис работает.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(5,),
            ),
        ),
    )
    is_valid, reason = validate_story_publication_eligibility(payload)
    assert is_valid is False
    assert reason == "service_access_without_concrete_entity"


def test_validate_story_publication_eligibility_accepts_valid_story():
    payload = EventPayload(
        headline="Электроснабжение восстановлено на Петровского",
        digest_summary="В местных чатах подтверждают, что электричество вернулось на улицу Петровского.",
        evidence_items=(
            EvidenceItemPayload(
                text="В местных чатах подтверждают, что электричество вернулось на улицу Петровского.",
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(4,),
                service_state=ServiceStatePayload(
                    subject_key="electricity",
                    subject_label="Электроснабжение",
                    dimension="power_supply",
                    state="AVAILABLE",
                    location="ул. Петровского",
                    entity="Россети",
                ),
            ),
        ),
    )
    is_valid, reason = validate_story_publication_eligibility(payload)
    assert is_valid is True
    assert reason is None
