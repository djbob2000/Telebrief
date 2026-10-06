# ruff: noqa: S101

from types import SimpleNamespace

import pytest

from src.editorial_models import StoryCard, StoryElement
from src.publication.digest_presentation import _is_usable_fact_line, build_required_digest_facts
from src.publication.story_quality import validate_story_publication_eligibility


def _payload(text: str, *, kind: str = "community_report") -> SimpleNamespace:
    return SimpleNamespace(
        headline=text,
        digest_summary=text,
        category="",
        evidence_items=[SimpleNamespace(text=text, kind=kind, publication_use="PUBLISH")],
    )


@pytest.mark.parametrize(
    "text,kind",
    [
        (
            "В Бердянске водоканал открыл пункт выдачи питьевой воды на улице Шевченко. "
            "Вода доступна ежедневно с 9:00 до 12:00, самовывоз.",
            "service_access",
        ),
        (
            "В Бердянске произошёл пожар в студии пирсинга на улице Шевченко. "
            "Пожарные эвакуировали посетителей.",
            "community_report",
        ),
        (
            "В Бердянске памятник отключили от электроснабжения на время ремонта сети "
            "на улице Шевченко.",
            "community_report",
        ),
        (
            "На улице Шевченко в Бердянске выключили подсветку памятника Самолёт.",
            "community_report",
        ),
        (
            "В Бердянске открыли пункт помощи на улице Шевченко: "
            "бесплатно отдают дрова семьям без отопления.",
            "community_report",
        ),
        (
            "В ответ на вопрос, где получить воду, водоканал сообщил: "
            "на улице Шевченко в Бердянске питьевая вода доступна с 9:00 до 12:00.",
            "service_access",
        ),
        (
            "Отвечая на вопрос, где получить воду, водоканал сообщил: "
            "на улице Шевченко в Бердянске питьевая вода доступна с 9:00 до 12:00.",
            "community_report",
        ),
        (
            "Житель критически отозвался о доступности врачей: "
            "в поликлинике на улице Шевченко в Бердянске терапевт не принимает до пятницы.",
            "community_report",
        ),
        ("На Горе света нет", "community_report"),
        ("По сообщениям жителей, на Горе нет света.", "community_report"),
        (
            "Жильцы скинулись по 300 рублей на домовой генератор, чтобы подавать воду.",
            "community_report",
        ),
        (
            "Житель запитал оборудование провайдера от своего генератора, и Wi-Fi появился в доме.",
            "community_report",
        ),
        ("Автобус №4 ходит примерно раз в час.", "community_report"),
        ("В Бердянске открылась студия пирсинга.", "community_report"),
        (
            "В ответ на вопрос о записи школа сообщила: "
            "бесплатный набор детей перед учебным годом открыт до 15 августа.",
            "community_report",
        ),
        (
            "В ответ на вопрос, где записать ребёнка, школа сообщила: "
            "бесплатный приём заявок открыт до 15 августа.",
            "community_report",
        ),
        (
            "В ответ на вопрос, где записать ребёнка, школа сообщила, "
            "что бесплатный приём заявок открыт до 15 августа.",
            "community_report",
        ),
        (
            "В ответ на вопрос о ярмарке организаторы сообщили, что ярмарка состоится в субботу.",
            "community_report",
        ),
        (
            "В городской библиотеке Бердянска открылся обменный фонд: бесплатно отдают книги.",
            "community_report",
        ),
        (
            "В Бердянске волонтёры бесплатно отдают дрова семьям без отопления.",
            "community_report",
        ),
    ],
)
def test_concrete_reports_survive_noise_keywords(text: str, kind: str) -> None:
    assert validate_story_publication_eligibility(_payload(text, kind=kind)) == (True, None)
    assert _is_usable_fact_line(text)


@pytest.mark.parametrize(
    "text",
    [
        "Памятник, по сообщению, только что выключили.",
        "Памятник только что выключили.",
        "По словам жителя, памятник вчера отключили.",
        "Только что выключили памятник.",
        "Такой адрес студии назвали в ответ на вопрос, где делают пирсинг уха.",
        "На даче бесплатно отдают древесину от трёх засохших вишен.",
        "Отдам дрова, самовывоз с улицы Шевченко.",
        "Волонтёры бесплатно отдают дрова после расчистки собственных участков, самовывоз.",
        "У нас нет адекватных врачей, которые помогут справиться с проблемой.",
        "Житель критически отозвался о доступности врачей.",
        "На улице Шевченко находится военкомат.",
        "Житель предлагает бесплатно забрать сухие дрова на дачах. Есть ещё досточки и брусочки.",
        "В Бердянске пирсинг уха делают в студии Рутрокс возле музыкальной школы.",
        "Комендантский час в Бердянске ранее действовал с сентября по май до 22:00, напомнили в чате.",
        "Житель Бердянска подсказал, где продают уголь на развес.",
        "В Бердянске предлагают услуги по уборке квартир и домов.",
        "С 10го числа в Улей открытие.",
        "Информация об открытии заведения, может на двери пишут.",
    ],
)
def test_pure_noise_is_rejected_without_help_from_generated_summary(text: str) -> None:
    assert not validate_story_publication_eligibility(_payload(text))[0]
    assert not _is_usable_fact_line(text)


@pytest.mark.parametrize("kind", ["community_report", "service_access"])
def test_noise_labelled_as_service_access_is_not_an_operational_fact(kind: str) -> None:
    payload = _payload("Памятник, по сообщению, только что выключили.", kind=kind)
    assert not validate_story_publication_eligibility(payload)[0]


def test_noise_in_generated_headline_does_not_veto_grounded_report() -> None:
    payload = _payload("На улице Шевченко в Бердянске воду подают с 9:00 до 12:00.")
    payload.headline = "Житель критически отозвался о доступности врачей"
    assert validate_story_publication_eligibility(payload) == (True, None)


def test_mixed_story_preserves_report_but_noise_is_not_a_required_digest_fact() -> None:
    report = "На улице Шевченко в Бердянске воду подают с 9:00 до 12:00."
    noise = "У нас нет адекватных врачей, которые помогут справиться с проблемой."
    payload = _payload(report, kind="service_access")
    payload.evidence_items.append(
        SimpleNamespace(text=noise, kind="community_report", publication_use="PUBLISH")
    )
    assert validate_story_publication_eligibility(payload) == (True, None)

    card = StoryCard(
        id="story:mixed",
        topic="Водоснабжение",
        importance="high",
        summary=report,
        rubric_id="utilities",
        story_kind="operational_status",
        representative_source_refs=["ref:water", "ref:noise"],
        hard_facts=[
            StoryElement(text=report, source_refs=["ref:water"]),
            StoryElement(text=noise, source_refs=["ref:noise"]),
        ],
    )
    facts = build_required_digest_facts(cards=(card,))
    assert len(facts) == 1
    assert facts[0].story_ids == ("story:mixed",)
    assert facts[0].support_ids == ("ref:water", "story:mixed")
    assert payload.evidence_items[1].text == noise
    assert payload.evidence_items[1].publication_use == "PUBLISH"


def test_noise_only_evidence_cannot_be_rescued_by_generated_event() -> None:
    payload = _payload("Памятник только что выключили.")
    payload.headline = "В Бердянске произошла авария электросети у памятника"
    payload.digest_summary = payload.headline
    assert not validate_story_publication_eligibility(payload)[0]


def test_private_lost_document_notice_is_not_city_news() -> None:
    """Run 311: a lost-documents notice with names and a phone reached the plan."""
    text = (
        "Утеряны 2 детских пенсионных удостоверения на имя Клима Д. и Иванова К. "
        "Просьба вернуть по телефону +79902338089 или в личку"
    )
    # Real sealed BRIEF payload shape: its summary passes the predicate check.
    payload = SimpleNamespace(
        headline="В Бердянске утеряны два детских пенсионных удостоверения",
        digest_summary=(
            "В Бердянске утеряны два детских пенсионных удостоверения. "
            "Владельцы просят вернуть их за вознаграждение."
        ),
        category="",
        evidence_items=[
            SimpleNamespace(text=text, kind="community_report", publication_use="PUBLISH")
        ],
    )
    eligible, reason = validate_story_publication_eligibility(payload)
    assert not eligible
    assert reason == "non_editorial_payload"


@pytest.mark.parametrize(
    "text",
    [
        "В МФЦ Бердянска объяснили, как восстановить утерянный паспорт: "
        "приём ведётся по будням с 9:00 до 17:00.",
        "В соцзащите на улице Шевченко начали принимать заявления на восстановление "
        "утерянных пенсионных удостоверений.",
    ],
)
def test_civic_lost_document_guidance_stays_eligible(text: str) -> None:
    eligible, _ = validate_story_publication_eligibility(_payload(text))
    assert eligible


def test_lost_document_reply_does_not_suppress_mixed_story() -> None:
    notice = "Потерял паспорт на Шевченко, просьба вернуть по телефону +79900000000"
    report = "По сообщениям жителей, на АКЗ нет света третьи сутки."
    payload = SimpleNamespace(
        headline=report,
        digest_summary=report,
        category="",
        evidence_items=[
            SimpleNamespace(text=report, kind="community_report", publication_use="PUBLISH"),
            SimpleNamespace(text=notice, kind="community_report", publication_use="PUBLISH"),
        ],
    )
    eligible, _ = validate_story_publication_eligibility(payload)
    assert eligible
