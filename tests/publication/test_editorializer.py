"""Contract and unit tests for DigestEditorializer (Plan 5 Task 5)."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest

from src.ai_providers import AIProvider
from src.collector import Message
from src.editorial_input import PreparedBundle
from src.editorial_models import SourceRecord, StoryCard, StoryElement, Uncertainty
from src.publication.editorializer import (
    DigestEditorializer,
    EditorializationError,
)

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


class MockAIProvider(AIProvider):
    def __init__(self, response_text: str = "") -> None:
        self.response_text = response_text
        self.calls: list[list[dict[str, str]]] = []

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        max_tokens: int = 1500,
        reasoning_effort: str | None = None,
        thinking: bool | None = None,
        response_format: dict | None = None,
    ) -> str:
        self.calls.append(messages)
        return self.response_text

    async def chat_completion_stream(self, *args, **kwargs):
        raise NotImplementedError
        yield ""


def _make_sample_card(
    card_id: str, topic: str = "Городские события", category: str = "other"
) -> StoryCard:
    return StoryCard(
        id=card_id,
        topic=topic,
        importance="high",
        summary="Исходный текст из базы данных.",
        category=category,
        representative_source_refs=["telegram:source:1:item:1:rev:1"],
        hard_facts=[
            StoryElement(
                text="Факт 1",
                source_refs=["telegram:source:1:item:1:rev:1"],
                status="established",
                attribution="КП Водоканал",
            )
        ],
        community_observations=[
            StoryElement(
                text="Наблюдение 1",
                source_refs=["telegram:source:1:item:1:rev:1"],
                status="attributed",
                attribution="Житель",
            )
        ],
        useful_details=[
            StoryElement(
                text="Время: 8:00 - 18:00",
                source_refs=["telegram:source:1:item:1:rev:1"],
                status="attributed",
            )
        ],
        uncertainties=[
            Uncertainty(
                text="Сроки не ясны",
                basis="Источник не указал",
                related_source_refs=["telegram:source:1:item:1:rev:1"],
            )
        ],
    )


def _make_sample_bundle() -> PreparedBundle:
    msg = Message(
        text="Сырой текст сообщения",
        sender="КП Водоканал",
        timestamp=_NOW,
        link="https://t.me/chan/1",
        channel_name="Бердянск Новости",
        has_media=False,
        media_type="",
        message_id=1,
    )
    record = SourceRecord(
        ref="telegram:source:1:item:1:rev:1",
        message=msg,
        source_type="official",
        context_text="Контекст новости",
    )
    return PreparedBundle(
        records={"telegram:source:1:item:1:rev:1": record},
        prompt_text="",
        total_messages=1,
        candidate_count=1,
    )


@pytest.mark.asyncio
async def test_editorializer_empty_cards_noop():
    provider = MockAIProvider()
    editorializer = DigestEditorializer(provider=provider)
    bundle = _make_sample_bundle()

    result = await editorializer.editorialize(cards=[], bundle=bundle)

    assert result == []
    assert len(provider.calls) == 0


@pytest.mark.asyncio
async def test_editorializer_success_preserves_cardinality_and_order_and_evidence():
    card1 = _make_sample_card("story-1")
    card2 = _make_sample_card("story-2")
    card3 = _make_sample_card("story-3")
    cards = [card1, card2, card3]
    bundle = _make_sample_bundle()

    # Model returns overlays in reversed order
    response = """
    {
      "overlays": [
        {
          "id": "story-3",
          "topic": "Новое расписание маршруток",
          "category": "transport",
          "summary": "Маршрут №4 продлен до микрорайона АКЗ."
        },
        {
          "id": "story-1",
          "topic": "Ремонт водовода на Восточном",
          "category": "utilities",
          "summary": "Водоснабжение восстановят к 20:00."
        },
        {
          "id": "story-2",
          "topic": "Проверка систем связи",
          "category": "telecom",
          "summary": "Операторы проводят плановые работы."
        }
      ]
    }
    """
    provider = MockAIProvider(response_text=response)
    editorializer = DigestEditorializer(provider=provider)

    result = await editorializer.editorialize(cards=cards, bundle=bundle)

    assert len(result) == 3
    # Check strict order preservation
    assert [c.id for c in result] == ["story-1", "story-2", "story-3"]

    # Check that presentation overlay fields changed
    assert result[0].topic == "Ремонт водовода на Восточном"
    assert result[0].category == "utilities"
    assert result[0].summary == "Водоснабжение восстановят к 20:00."

    assert result[1].topic == "Проверка систем связи"
    assert result[1].category == "telecom"

    assert result[2].topic == "Новое расписание маршруток"
    assert result[2].category == "transport"

    # Check IMMUTABILITY of evidence and other fields
    for before, after in zip(cards, result, strict=True):
        assert before.id == after.id
        assert before.importance == after.importance
        assert before.representative_source_refs == after.representative_source_refs
        assert before.hard_facts == after.hard_facts
        assert before.community_observations == after.community_observations
        assert before.useful_details == after.useful_details
        assert before.uncertainties == after.uncertainties


@pytest.mark.asyncio
async def test_editorializer_partial_fallback_on_invalid_or_missing_fields():
    card1 = _make_sample_card("story-1", topic="Старая тема 1")
    card2 = _make_sample_card("story-2", topic="Старая тема 2")
    card3 = _make_sample_card("story-3", topic="Старая тема 3")
    cards = [card1, card2, card3]
    bundle = _make_sample_bundle()

    # story-1 is valid, story-2 has invalid category, story-3 is missing in response
    response = """
    {
      "overlays": [
        {
          "id": "story-1",
          "topic": "Свет на Азмоле",
          "category": "utilities",
          "summary": "Энергетики завершили ремонт подстанции."
        },
        {
          "id": "story-2",
          "topic": "Невалидная категория",
          "category": "totally_invalid_cat",
          "summary": "Текст."
        }
      ]
    }
    """
    provider = MockAIProvider(response_text=response)
    editorializer = DigestEditorializer(provider=provider)

    result = await editorializer.editorialize(cards=cards, bundle=bundle)

    assert len(result) == 3
    assert result[0].topic == "Свет на Азмоле"
    assert result[0].category == "utilities"

    # story-2 and story-3 fell back to original canonical cards
    assert result[1].topic == "Старая тема 2"
    assert result[2].topic == "Старая тема 3"


@pytest.mark.asyncio
async def test_editorializer_structural_failure_raises_editorialization_error():
    card1 = _make_sample_card("story-1")
    cards = [card1]
    bundle = _make_sample_bundle()

    # Case 1: Unknown ID
    resp_unknown = '{"overlays": [{"id": "story-unknown", "topic": "Т", "category": "utilities", "summary": "С"}]}'
    editorializer = DigestEditorializer(provider=MockAIProvider(resp_unknown))
    with pytest.raises(EditorializationError, match="unknown story ID"):
        await editorializer.editorialize(cards=cards, bundle=bundle)

    # Case 2: Duplicate ID
    resp_dup = '{"overlays": [{"id": "story-1", "topic": "Т1", "category": "utilities", "summary": "С1"}, {"id": "story-1", "topic": "Т2", "category": "utilities", "summary": "С2"}]}'
    editorializer = DigestEditorializer(provider=MockAIProvider(resp_dup))
    with pytest.raises(EditorializationError, match="duplicate story ID"):
        await editorializer.editorialize(cards=cards, bundle=bundle)

    # Case 3: Malformed JSON
    editorializer = DigestEditorializer(provider=MockAIProvider("not json at all"))
    with pytest.raises(EditorializationError, match="failed to parse editorializer JSON"):
        await editorializer.editorialize(cards=cards, bundle=bundle)


@pytest.mark.asyncio
async def test_editorializer_records_attempt_observer_lifecycle():
    card = _make_sample_card("story-1")
    cards = [card]
    bundle = _make_sample_bundle()

    response = """
    {
      "overlays": [
        {
          "id": "story-1",
          "topic": "Ремонт дороги",
          "category": "transport",
          "summary": "Улицу перекрыли до понедельника."
        }
      ]
    }
    """
    provider = MockAIProvider(response_text=response)
    editorializer = DigestEditorializer(provider=provider)

    mock_observer = AsyncMock()
    mock_observer.attempt_started.return_value = 101

    result = await editorializer.editorialize(
        cards=cards, bundle=bundle, attempt_observer=mock_observer
    )

    assert len(result) == 1
    mock_observer.attempt_started.assert_awaited_once()
    assert mock_observer.attempt_started.call_args[0][0] == "digest_editorializer"
    mock_observer.attempt_finished.assert_awaited_once_with(101, "succeeded")
