"""Tests for source-bounded editorial data models."""

import pytest

from src.editorial_models import EditorialAnalysis, StoryCard, StoryElement, Uncertainty


def test_story_card_round_trip_preserves_element_refs_and_editorial_angle():
    card = StoryCard(
        id="SC001",
        topic="Вода",
        importance="high",
        summary="В нескольких районах обсуждали перебои с водой.",
        story_kind="infrastructure",
        editorial_angle={
            "text": "Перебои с водой стали заметной бытовой темой дня.",
            "basis_refs": ["S000001"],
            "type": "editorial_synthesis",
        },
        hard_facts=[
            StoryElement(
                text="Коммунальное предприятие сообщило об отключении.",
                source_refs=["S000001"],
                status="established",
            )
        ],
        uncertainties=[
            Uncertainty(
                text="Причина перебоев не установлена.",
                basis="No supplied source directly establishes the cause",
                related_source_refs=["S000001"],
            )
        ],
    )

    restored = StoryCard.from_dict(card.to_dict())

    assert restored.to_dict() == card.to_dict()
    assert restored.editorial_angle["type"] == "editorial_synthesis"
    assert restored.hard_facts[0].source_refs == ["S000001"]
    assert restored.uncertainties[0].basis.startswith("No supplied")


def test_story_card_rejects_invalid_importance_and_element_status():
    with pytest.raises(ValueError, match="importance"):
        StoryCard(id="SC001", topic="Тема", importance="urgent", summary="Текст")

    with pytest.raises(ValueError, match="status"):
        StoryElement(text="Текст", source_refs=["S000001"], status="confirmed")


def test_story_card_validates_element_refs_against_prepared_bundle():
    card = StoryCard(
        id="SC001",
        topic="Тема",
        importance="medium",
        summary="Текст",
        hard_facts=[StoryElement(text="Факт", source_refs=["S000404"])],
    )

    with pytest.raises(ValueError, match="S000404"):
        card.validate_refs({"S000001"})


def test_story_kind_is_free_form_and_analysis_serializes_labels():
    analysis = EditorialAnalysis(
        cards=[
            StoryCard(
                id="SC001",
                topic="Городской быт",
                importance="low",
                summary="Жители обсуждали зарядку телефонов.",
                story_kind="new-future-category",
            )
        ],
        labels={"S000001": {"label": "community_observation", "flags": ["question"]}},
    )

    restored = EditorialAnalysis.from_dict(analysis.to_dict())
    assert restored.cards[0].story_kind == "new-future-category"
    assert restored.labels["S000001"]["flags"] == ["question"]


def test_story_element_from_string_with_card_refs():
    elem = StoryElement.from_dict(
        "Жители сообщали об отключениях", card_refs=["S000001", "S000002"]
    )
    assert elem.text == "Жители сообщали об отключениях"
    assert elem.source_refs == ["S000001", "S000002"]
    assert elem.status == "attributed"


def test_story_element_from_string_without_card_refs_fails():
    with pytest.raises(ValueError, match="source_refs"):
        StoryElement.from_dict("Текст без рефов", card_refs=[])


def test_uncertainty_without_basis_defaults_to_unspecified():
    unc = Uncertainty.from_dict(
        {"text": "Неясны сроки ремонта", "related_source_refs": ["S000001"]}
    )
    assert unc.text == "Неясны сроки ремонта"
    assert unc.basis == "unspecified"
    assert unc.related_source_refs == ["S000001"]


def test_uncertainty_from_string():
    unc = Uncertainty.from_dict("Неясны сроки ремонта", card_refs=["S000001"])
    assert unc.text == "Неясны сроки ремонта"
    assert unc.basis == "unspecified"
    assert unc.related_source_refs == ["S000001"]


def test_story_card_canonical_representative_refs_from_aliases():
    data = {
        "id": "SC001",
        "topic": "Электричество",
        "summary": "Массовые отключения",
        "sources": ["S000001", "S000002", "S000001"],  # deduplication & alias
        "hard_facts": ["Света нет на АКЗ"],
        "uncertainties": ["Сроки включения неизвестны"],
    }
    card = StoryCard.from_dict(data)
    assert card.representative_source_refs == ["S000001", "S000002"]
    assert len(card.hard_facts) == 1
    assert card.hard_facts[0].source_refs == ["S000001", "S000002"]
    assert card.hard_facts[0].status == "attributed"
    assert card.all_source_refs() == {"S000001", "S000002"}


def test_story_card_keeps_valid_nested_element_when_siblings_are_malformed():
    data = {
        "id": "SC001",
        "topic": "Свет",
        "summary": "Отключения",
        "representative_source_refs": ["S000001"],
        "hard_facts": [
            None,
            {"text": ""},  # empty text
            12345,  # wrong type
            "Жители АКЗ сообщали об отключениях",  # valid string
            {
                "text": "Подтверждено КП",
                "source_refs": ["S000001"],
                "status": "established",
            },  # valid dict
        ],
        "uncertainties": [
            None,
            {"text": "Неясны сроки"},  # valid dict with default basis
        ],
    }
    card = StoryCard.from_dict(data)
    assert len(card.hard_facts) == 2
    assert card.hard_facts[0].text == "Жители АКЗ сообщали об отключениях"
    assert card.hard_facts[0].source_refs == ["S000001"]
    assert card.hard_facts[1].text == "Подтверждено КП"
    assert len(card.uncertainties) == 1
    assert card.uncertainties[0].text == "Неясны сроки"


def test_story_card_sanitized_against_refs_removes_bad_ref_locally():
    card = StoryCard(
        id="SC001",
        topic="Свет",
        importance="high",
        summary="Отключения",
        representative_source_refs=["S000001", "S999999"],
        hard_facts=[
            StoryElement(text="Факт 1", source_refs=["S000001", "S999999"]),
            StoryElement(text="Факт 2 (только битый ref)", source_refs=["S999999"]),
        ],
    )
    sanitized = card.sanitized_against_refs({"S000001"})
    assert sanitized is not None
    assert sanitized.representative_source_refs == ["S000001"]
    assert len(sanitized.hard_facts) == 1
    assert sanitized.hard_facts[0].text == "Факт 1"
    assert sanitized.hard_facts[0].source_refs == ["S000001"]


def test_story_card_sanitized_against_refs_returns_none_if_no_valid_refs():
    card = StoryCard(
        id="SC001",
        topic="Свет",
        importance="high",
        summary="Отключения",
        representative_source_refs=["S999999"],
        hard_facts=[StoryElement(text="Факт", source_refs=["S999999"])],
    )
    assert card.sanitized_against_refs({"S000001"}) is None


def test_editorial_analysis_sanitized_against_refs():
    card1 = StoryCard(
        id="SC001",
        topic="Свет",
        importance="high",
        summary="Отключения",
        representative_source_refs=["S000001"],
    )
    card2 = StoryCard(
        id="SC002",
        topic="Газ",
        importance="low",
        summary="Газ",
        representative_source_refs=["S999999"],
    )
    analysis = EditorialAnalysis(
        cards=[card1, card2],
        labels={"S000001": {"flag": "ok"}, "S999999": {"flag": "bad"}},
    )
    sanitized = analysis.sanitized_against_refs({"S000001"})
    assert len(sanitized.cards) == 1
    assert sanitized.cards[0].id == "SC001"
    assert "S000001" in sanitized.labels
    assert "S999999" not in sanitized.labels
