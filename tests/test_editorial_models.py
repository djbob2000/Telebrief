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
