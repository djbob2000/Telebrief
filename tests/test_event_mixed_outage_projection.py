# ruff: noqa: S101
"""Mixed source text must not replace already separated evidence claims."""

from src.domain.event_payload import EventPayload
from src.processing.event_triage import decompose_mixed_outage_evidence


def _payload(items: list[dict]) -> EventPayload:
    return EventPayload.from_dict({"topic": "Электроснабжение", "evidence_items": items})


def _item(text: str, kind: str = "community_report", use: str = "PUBLISH") -> dict:
    return {
        "text": text,
        "kind": kind,
        "publication_use": use,
        "source_fragment_ids": [109062],
    }


def test_separated_claims_keep_their_own_text_kind_and_publication_use() -> None:
    source = (
        "Зачем родственнику ночью свет? Когда весь город отключен от электроэнергии. "
        "Люди заряжают телефоны возле магазинов и кафе. Батарейка стоит 300–600 рублей."
    )
    payload = _payload(
        [
            _item("Зачем родственнику ночью свет?", "resident_question", "CONTEXT"),
            _item("По сообщению жителя, город отключён от электроэнергии."),
            _item("Люди заряжают телефоны возле магазинов и кафе."),
            _item("Батарейка стоит 300–600 рублей."),
        ]
    )

    result = decompose_mixed_outage_evidence(payload, {109062: source})

    assert result.evidence_items == payload.evidence_items


def test_question_only_extraction_can_recover_source_outage_once() -> None:
    source = "Свет будет? На улице Морозова нет электричества третий день."
    payload = _payload([_item("Свет будет?", "resident_question", "CONTEXT")])

    result = decompose_mixed_outage_evidence(payload, {109062: source})

    assert len(result.evidence_items) == 2
    assert result.evidence_items[0].kind == "resident_question"
    assert result.evidence_items[0].publication_use == "CONTEXT"
    assert result.evidence_items[1].source_fragment_ids == (109062,)
    assert "третий день" in result.evidence_items[1].text
    assert result.evidence_items[1].publication_use == "PUBLISH"
    assert decompose_mixed_outage_evidence(result, {109062: source}) == result


def test_mixed_claim_split_does_not_borrow_other_source_details() -> None:
    claim = "Свет будет? На улице Морозова нет электричества третий день."
    payload = _payload([_item(claim)])

    result = decompose_mixed_outage_evidence(
        payload, {109062: claim + " Батарейка стоит 300–600 рублей."}
    )

    assert len(result.evidence_items) == 2
    assert "300" not in result.evidence_items[1].text


def test_excluded_mixed_claim_is_not_promoted_to_publish() -> None:
    payload = _payload([_item("Свет будет? На улице Морозова нет электричества.", use="EXCLUDE")])

    assert decompose_mixed_outage_evidence(payload) == payload
