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


def test_validate_story_publication_eligibility_rejects_pure_advice_without_event():
    """A warning or appeal alone is not a local event for publication."""
    payload = EventPayload(
        headline="Житель Бердянска предупреждает об опасном месте",
        digest_summary="Местный житель выражает ужас и советует не появляться в определённом месте.",
        evidence_items=(
            EvidenceItemPayload(
                text="Ужас, не советую там появляться.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(8,),
            ),
        ),
    )

    is_valid, reason = validate_story_publication_eligibility(payload)

    assert is_valid is False
    assert reason == "advice_without_event"


def test_validate_story_publication_eligibility_keeps_advice_with_event():
    """A practical warning remains publishable when grounded in a concrete event."""
    payload = EventPayload(
        headline="После взрыва жителей просят не выходить на улицу",
        digest_summary="После взрыва в городе жителям советуют не выходить на улицу.",
        evidence_items=(
            EvidenceItemPayload(
                text="После взрыва в городе жителям советуют не выходить на улицу.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(9,),
            ),
        ),
    )

    is_valid, reason = validate_story_publication_eligibility(payload)

    assert is_valid is True
    assert reason is None


def test_validate_story_publication_eligibility_rejects_question_without_answer():
    payload = EventPayload(
        headline="Жители спрашивают, есть ли свет",
        digest_summary="В чате жители интересуются наличием электричества в районах города.",
        evidence_items=(
            EvidenceItemPayload(
                text="Есть ли у кого-нибудь свет?",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(10,),
            ),
        ),
    )

    is_valid, reason = validate_story_publication_eligibility(payload)

    assert is_valid is False
    assert reason == "resident_question_only"


def test_validate_story_publication_eligibility_rejects_non_editorial_payload():
    payload = EventPayload(
        headline="Поиск работы в Бердянске",
        digest_summary="Один человек ищет подработку на завтра в Бердянске.",
        evidence_items=(
            EvidenceItemPayload(
                text="Ищу шабашку на завтра.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(11,),
            ),
        ),
    )

    is_valid, reason = validate_story_publication_eligibility(payload)

    assert is_valid is False
    assert reason == "non_editorial_payload"


def test_validate_story_publication_eligibility_rejects_promotional_event_copy():
    payload = EventPayload(
        headline="Культурное мероприятие для девочек",
        digest_summary="Планируется мероприятие, создающее атмосферу красоты для маленьких леди.",
        evidence_items=(
            EvidenceItemPayload(
                text="Атмосфера красоты будет окружать наших маленьких леди.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(12,),
            ),
        ),
    )

    is_valid, reason = validate_story_publication_eligibility(payload)

    assert is_valid is False
    assert reason == "non_editorial_payload"


def test_validate_story_publication_eligibility_rejects_unresolved_transport_chatter():
    payload = EventPayload(
        headline="Проблемы с прохождением КПП",
        digest_summary="Житель сообщает, что не может пройти через КПП. Подробности уточняются.",
        evidence_items=(
            EvidenceItemPayload(
                text="Не могу пройти через КПП.",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(13,),
            ),
        ),
    )

    is_valid, reason = validate_story_publication_eligibility(payload)

    assert is_valid is False
    assert reason == "non_editorial_payload"


def test_validate_story_publication_eligibility_rejects_meta_only_local_reports():
    cases = (
        (
            "В Бердянске объявлена запись на приём",
            "Жителям Бердянска сообщают о записи на приём. Подробности не раскрыты.",
        ),
        (
            "Сообщение о районе Восточный",
            "Житель Бердянска сообщает о ситуации в районе Восточный. Детали не раскрыты.",
        ),
        (
            "Жители Бердянска обсуждают возможное появление мечети",
            "В городском чате обсуждается вопрос о возможном появлении мечети.",
        ),
        (
            "В Бердянске произошло отключение в 21:10",
            "Житель Бердянска сообщил, что что-то отключилось в 21:10.",
        ),
        (
            "Рыбак планирует снять видео с третьей дамбы",
            "Местный житель сообщает о намерении порыбачить на третьей дамбе и снять видео.",
        ),
        (
            "Житель Бердянска иронизирует о плакатах",
            "Сообщение содержит ироничное упоминание Бердянска, конкретной информации нет.",
        ),
        (
            "В Бердянске заметили кошку после стерилизации — возможно, потерялась",
            "Жительница сообщает о кошке после стерилизации: возможно, кто-то её потерял.",
        ),
    )

    for headline, summary in cases:
        payload = EventPayload(
            headline=headline,
            digest_summary=summary,
            evidence_items=(
                EvidenceItemPayload(
                    text=summary,
                    kind="community_report",
                    publication_use="PUBLISH",
                    source_fragment_ids=(14,),
                ),
            ),
        )

        is_valid, reason = validate_story_publication_eligibility(payload)

        assert is_valid is False
        assert reason in {"non_editorial_payload", "lacks_meaningful_predicate"}


def test_validate_story_publication_eligibility_rejects_reply_annotation_only_service():
    payload = EventPayload(
        headline="Житель Бердянска спрашивает о водоканале",
        digest_summary="Сообщение из Бердянска о ситуации с водоканалом.",
        tags=("водоснабжение",),
        evidence_items=(
            EvidenceItemPayload(
                text='(in_reply_to: "А к нам сегодня приходили показания электроэнергии проверять.")',
                kind="service_access",
                publication_use="PUBLISH",
                source_fragment_ids=(7,),
                service_state=ServiceStatePayload(
                    subject_key="water_supply",
                    subject_label="Водоснабжение",
                    dimension="availability",
                    state="UNAVAILABLE",
                    entity="",
                ),
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


def test_validate_story_publication_eligibility_rejects_lost_and_found_pet():
    payload = EventPayload(
        headline="В районе 16-й школы нашли собаку — ищут хозяина",
        digest_summary="Житель Бердянска сообщает о найденной собаке в районе 16-й школы и просит помочь найти хозяина.",
        evidence_items=(
            EvidenceItemPayload(
                text="Помогите найти хозяина, в районе 16 школы нашли собачку",
                kind="community_report",
                publication_use="PUBLISH",
                source_fragment_ids=(5,),
            ),
        ),
    )
    is_valid, reason = validate_story_publication_eligibility(payload)
    assert is_valid is False
    assert reason == "lost_and_found_pet"


def test_has_meaningful_predicate_rejects_meta_message_headline():
    assert has_meaningful_predicate("Сообщение о районе 16 школы в Бердянске") is False
    assert (
        has_meaningful_predicate(
            "Житель упоминает район 16 школы в Бердянске, но детали не раскрыты."
        )
        is False
    )
    assert has_meaningful_predicate("Внизу, район 16 школы") is False
