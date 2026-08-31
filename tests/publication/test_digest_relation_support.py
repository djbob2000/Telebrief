"""Tests for narrow deterministic causal/mechanism relation validation in digests."""

from src.publication.digest_relation_support import (
    find_unsupported_digest_relations,
)


def test_find_unsupported_digest_relations_detects_invented_mechanism_cause() -> None:
    # Source only mentions outage, no cause
    supports = ["По сообщениям жителей, на Горе нет света уже два часа."]
    draft_text = "Авария на подстанции оставила Гору без света."

    violations = find_unsupported_digest_relations(draft_text, supports)
    assert len(violations) >= 1
    assert violations[0].reason == "UNSUPPORTED_DIGEST_RELATION"
    assert (
        "авария" in violations[0].cause.casefold() or "подстанции" in violations[0].cause.casefold()
    )


def test_find_unsupported_digest_relations_detects_invented_connector_cause() -> None:
    supports = ["Жители сообщают: на Горе отключили электричество."]
    draft_text = "На Горе нет света из-за аварии на трансформаторе."

    violations = find_unsupported_digest_relations(draft_text, supports)
    assert len(violations) >= 1
    assert violations[0].reason == "UNSUPPORTED_DIGEST_RELATION"


def test_find_unsupported_digest_relations_accepts_supported_causal_mechanism() -> None:
    supports = ["Авария на подстанции: на Горе отключено электроснабжение."]
    draft_text = "Авария на подстанции оставила Гору без света."

    violations = find_unsupported_digest_relations(draft_text, supports)
    assert len(violations) == 0


def test_find_unsupported_digest_relations_accepts_non_causal_factual_prose() -> None:
    supports = ["На Горе нет света."]
    draft_text = "По сообщениям жителей, на Горе нет света."

    violations = find_unsupported_digest_relations(draft_text, supports)
    assert len(violations) == 0


def test_find_unsupported_digest_relations_accepts_action_workaround_without_causal_claim() -> None:
    supports = [
        "На Горе нет света.",
        "Жильцы дома 12 запустили домовой генератор.",
    ]
    draft_text = "На Горе нет света, жильцы запустили домовой генератор."

    violations = find_unsupported_digest_relations(draft_text, supports)
    assert len(violations) == 0
