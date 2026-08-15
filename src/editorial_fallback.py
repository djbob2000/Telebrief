"""Deterministic thematic digest used when all editorial AI paths are unavailable."""

from __future__ import annotations

import re
from collections import defaultdict

from src.editorial_models import PreparedBundle, SourceRecord, StoryCard, StoryElement
from src.editorial_writer import ArticleDraft, ArticleSection


class NoSubstantiveMaterialError(RuntimeError):
    """Raised when filtering leaves no usable local information."""


_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "electricity": ("свет", "электр", "генератор", "подстанц", "заряд"),
    "water": ("вод", "водопровод", "колонк", "скважин"),
    "communications": ("связ", "интернет", "мобил", "телефон", "роуминг"),
    "incidents": ("взрыв", "пожар", "дтп", "авар", "обстрел", "бпла", "пострад"),
    "transport": ("дорог", "маршрут", "автобус", "перекрыт", "транспорт"),
    "social": ("выплат", "пенси", "пособ", "гуманитар", "больниц", "лекарств"),
    "city_life": ("магазин", "очеред", "район", "двор", "дом", "улиц"),
}


class DeterministicStoryCardBuilder:
    """Cluster prepared records conservatively without deriving causes or scale."""

    def build(self, bundle: PreparedBundle) -> list[StoryCard]:
        if not bundle.records:
            raise NoSubstantiveMaterialError("no substantive city material remains")
        grouped: dict[str, list[tuple[str, SourceRecord]]] = defaultdict(list)
        for ref, record in bundle.records.items():
            topic = self._topic_for(record.message.text)
            grouped[topic].append((ref, record))

        cards: list[StoryCard] = []
        for index, (topic, entries) in enumerate(grouped.items(), start=1):
            hard_facts: list[StoryElement] = []
            observations: list[StoryElement] = []
            useful: list[StoryElement] = []
            for ref, record in entries:
                text = record.message.text.strip()
                element = StoryElement(
                    text=text,
                    source_refs=[ref],
                    status=self._status(record.source_type, text),
                    attribution=record.message.channel_name,
                    areas=self._areas(text),
                )
                if record.source_type in {"official", "news"}:
                    hard_facts.append(element)
                else:
                    observations.append(element)
                if element.areas:
                    useful.append(element)
            summary = entries[0][1].message.text.strip()
            cards.append(
                StoryCard(
                    id=f"SC{index:03d}",
                    topic=topic,
                    importance=self._importance(topic, entries),
                    story_kind=(
                        "infrastructure"
                        if topic in {"water", "electricity", "communications"}
                        else topic
                    ),
                    summary=summary,
                    hard_facts=hard_facts,
                    community_observations=observations,
                    useful_details=useful,
                )
            )
        if not cards:
            raise NoSubstantiveMaterialError("no substantive city material remains")
        return cards

    @staticmethod
    def _topic_for(text: str) -> str:
        normalized = text.lower()
        for topic, keywords in _TOPIC_KEYWORDS.items():
            if any(keyword in normalized for keyword in keywords):
                return topic
        return "city_life"

    @staticmethod
    def _status(source_type: str, text: str) -> str:
        if source_type == "official" and re.search(
            r"\b(сообщил|объявил|отключат|начн|работы)\b", text.lower()
        ):
            return "established"
        return "attributed"

    @staticmethod
    def _importance(topic: str, entries: list[tuple[str, SourceRecord]]) -> str:
        if topic in {"incidents", "electricity", "water"}:
            return "high"
        if len(entries) > 2:
            return "medium"
        return "low"

    @staticmethod
    def _areas(text: str) -> list[str]:
        return re.findall(r"(?:на|в|по)\s+([А-ЯЁЇІЄ][\w-]+)", text)


class StoryCardRenderer:
    """Render fallback cards as a readable, compact thematic article."""

    def render(self, cards: list[StoryCard]) -> ArticleDraft:
        if not cards:
            raise NoSubstantiveMaterialError("no story cards to render")
        sections: list[ArticleSection] = []
        for card in cards:
            paragraphs: list[str] = []
            if card.hard_facts:
                paragraphs.extend(self._render_element(element) for element in card.hard_facts)
            if card.community_observations:
                observations = "; ".join(element.text for element in card.community_observations)
                paragraphs.append(f"Жители в сообщениях обсуждали: {observations}.")
            if paragraphs:
                sections.append(ArticleSection(self._heading(card.topic), paragraphs))
        if not sections:
            raise NoSubstantiveMaterialError("story cards contain no renderable material")
        lead = "За последние сутки в городе обсуждали несколько практических тем и происшествий."
        return ArticleDraft(
            headline="Что происходило в городе за сутки",
            lead=lead,
            paragraphs=[],
            sections=sections,
        )

    @staticmethod
    def _render_element(element: StoryElement) -> str:
        """Preserve attribution unless the source explicitly established its own action."""
        if element.status == "established":
            return element.text
        source = element.attribution or "источник"
        return f"{source} сообщил: {element.text}"

    @staticmethod
    def _heading(topic: str) -> str:
        return {
            "electricity": "Электроснабжение",
            "water": "Вода",
            "communications": "Связь и интернет",
            "incidents": "Происшествия",
            "transport": "Транспорт и дороги",
            "social": "Социальные вопросы",
            "city_life": "Городская жизнь",
        }.get(topic, topic.capitalize())
