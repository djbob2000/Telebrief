"""Deterministic thematic digest used when all editorial AI paths are unavailable."""

from __future__ import annotations

import re
from collections import defaultdict

from src.editorial_input import EditorialInputBuilder
from src.editorial_models import PreparedBundle, SourceRecord, StoryCard, StoryElement
from src.editorial_writer import ArticleDraft, ArticleSection


class NoSubstantiveMaterialError(RuntimeError):
    """Raised when filtering leaves no usable local information."""


_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "electricity": ("свет", "электр", "генератор", "подстанц", "заряд"),
    "water": ("вод", "водопровод", "колонк", "скважин"),
    "communications": (
        "связ",
        "интернет",
        "мобил",
        "телефон",
        "роуминг",
        "onet",
        "юпитер",
        "телеком",
    ),
    "incidents": ("взрыв", "пожар", "дтп", "авар", "обстрел", "бпла", "пострад"),
    "transport": ("дорог", "маршрут", "автобус", "перекрыт", "транспорт"),
    "social": ("выплат", "пенси", "пособ", "гуманитар", "больниц", "лекарств"),
    "city_life": ("магазин", "очеред", "район", "двор", "дом", "улиц"),
}

_TOPIC_HEADINGS = {
    "electricity": "Электроснабжение",
    "water": "Вода",
    "communications": "Связь и интернет",
    "incidents": "Происшествия",
    "transport": "Транспорт и дороги",
    "social": "Социальные вопросы",
    "city_life": "Городская жизнь",
}

_KNOWN_AREAS = (
    "АКЗ",
    "Колония",
    "Морская",
    "Центральная",
    "Победы",
    "Азовская",
    "Южный",
    "Лиски",
)
_KNOWN_PROVIDERS = (
    "Onet",
    "Юпитер",
    "+7телеком",
    "7телеком",
    "Киевстар",
    "Vodafone",
    "Lifecell",
)
_NEGATIVE_SIGNAL = re.compile(
    r"(?:нет|не\s+работ|пропал|исчез|перебо|плох|сбой|отключ)", re.IGNORECASE
)


class DeterministicStoryCardBuilder:
    """Build short editorial summaries without copying source messages into cards."""

    def build(self, bundle: PreparedBundle) -> list[StoryCard]:
        grouped: dict[str, list[tuple[str, SourceRecord]]] = defaultdict(list)
        for ref, record in bundle.records.items():
            if EditorialInputBuilder._is_noise(record.message.text):
                continue
            grouped[self._topic_for(record.message.text)].append((ref, record))
        if not grouped:
            raise NoSubstantiveMaterialError("no substantive city material remains")

        cards: list[StoryCard] = []
        for index, (topic, entries) in enumerate(grouped.items(), start=1):
            source_entries = [
                item for item in entries if item[1].source_type in {"official", "news"}
            ]
            community_entries = [
                item for item in entries if item[1].source_type not in {"official", "news"}
            ]
            hard_fact = self._make_element(topic, source_entries, source_group=True)
            observation = self._make_element(topic, community_entries, source_group=False)
            hard_facts = [hard_fact] if hard_fact else []
            observations = [observation] if observation else []
            summary_element = hard_fact or observation
            if summary_element is None:
                continue
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
                    summary=summary_element.text,
                    hard_facts=hard_facts,
                    community_observations=observations,
                )
            )
        if not cards:
            raise NoSubstantiveMaterialError("no substantive city material remains")
        return cards

    @staticmethod
    def _topic_for(text: str) -> str:
        normalized = text.lower()
        scores = {
            topic: sum(normalized.count(keyword) for keyword in keywords)
            for topic, keywords in _TOPIC_KEYWORDS.items()
        }
        topic, score = max(scores.items(), key=lambda item: item[1])
        return topic if score else "city_life"

    def _make_element(
        self,
        topic: str,
        entries: list[tuple[str, SourceRecord]],
        *,
        source_group: bool,
    ) -> StoryElement | None:
        if not entries:
            return None
        texts = [record.message.text.strip() for _, record in entries]
        refs = [ref for ref, _ in entries]
        areas = self._areas(texts)
        providers = self._providers(texts)
        established = (
            source_group
            and all(record.source_type == "official" for _, record in entries)
            and any(self._is_own_action(text) for text in texts)
        )
        text = self._normalized_text(
            topic,
            texts,
            areas=areas,
            providers=providers,
            source_group=source_group,
        )
        return StoryElement(
            text=text,
            source_refs=refs,
            status="established" if established else "attributed",
            attribution=self._attribution(entries, source_group=source_group),
            areas=areas,
        )

    @staticmethod
    def _is_own_action(text: str) -> bool:
        return bool(
            re.search(
                r"\b(?:сообщил|сообщает|объявил|объявляет|отключат|отключили|"
                r"восстановили|начнут|проведут|работы)\b",
                text.lower(),
            )
        )

    @staticmethod
    def _attribution(entries: list[tuple[str, SourceRecord]], *, source_group: bool) -> str:
        if not source_group:
            return "Жители"
        channels = {record.message.channel_name for _, record in entries}
        return next(iter(channels)) if len(channels) == 1 else "Источники"

    @staticmethod
    def _normalized_text(
        topic: str,
        texts: list[str],
        *,
        areas: list[str],
        providers: list[str],
        source_group: bool,
    ) -> str:
        if source_group:
            base = DeterministicStoryCardBuilder._source_sentence(topic, texts)
        else:
            base = DeterministicStoryCardBuilder._community_sentence(topic, texts)
        additions: list[str] = []
        if providers and topic == "communications":
            additions.append(f"Жители упоминали провайдеров: {', '.join(providers)}.")
        if len(areas) >= 2:
            additions.append(f"Сообщения поступали из районов: {', '.join(areas)}.")
        elif areas:
            additions.append(f"Жители упоминали район {areas[0]}.")
        return " ".join([base, *additions]).strip()

    @staticmethod
    def _source_sentence(topic: str, texts: list[str]) -> str:
        joined = " ".join(texts).lower()
        labels = {
            "water": "водоснабжении",
            "electricity": "электроснабжении",
            "communications": "связи и интернете",
            "transport": "транспортной ситуации",
            "social": "социальной сфере",
            "incidents": "происшествии",
            "city_life": "городской жизни",
        }
        subject = labels[topic]
        if topic == "water" and "восстанов" in joined:
            return "Источник сообщил о восстановлении водоснабжения."
        if topic == "water" and ("отключ" in joined or "прекрат" in joined):
            return "Источник сообщил об отключении водоснабжения."
        if topic == "electricity" and ("отключ" in joined or "нет свет" in joined):
            return "Источник сообщил об отключении электроснабжения."
        if topic == "communications" and _NEGATIVE_SIGNAL.search(joined):
            return "Источник сообщил о перебоях со связью или интернетом."
        return f"Источник сообщил об изменениях в теме {subject}."

    @staticmethod
    def _community_sentence(topic: str, texts: list[str]) -> str:
        joined = " ".join(texts).lower()
        if topic == "communications":
            if _NEGATIVE_SIGNAL.search(joined):
                return "Жители сообщали о перебоях с мобильной связью и интернетом."
            return "Жители обсуждали доступность мобильной связи и интернета."
        if topic == "electricity":
            return "Жители сообщали о перебоях с электроснабжением."
        if topic == "water":
            return "Жители сообщали о перебоях с водоснабжением."
        if topic == "incidents":
            if "взрыв" in joined or "громк" in joined:
                return "Жители сообщили о громком звуке или взрыве."
            return "Жители сообщали о происшествии."
        if topic == "transport":
            return "Жители обсуждали ситуацию на дорогах и в транспорте."
        if topic == "social":
            return "Жители обсуждали вопросы социальной сферы."
        return "Жители обсуждали городские бытовые вопросы."

    @staticmethod
    def _importance(topic: str, entries: list[tuple[str, SourceRecord]]) -> str:
        if topic in {"incidents", "electricity", "water"}:
            return "high"
        if len(entries) > 2:
            return "medium"
        return "low"

    @staticmethod
    def _areas(texts: list[str]) -> list[str]:
        joined = " ".join(texts)
        return [
            area
            for area in _KNOWN_AREAS
            if re.search(rf"\b{re.escape(area)}\b", joined, re.IGNORECASE)
        ]

    @staticmethod
    def _providers(texts: list[str]) -> list[str]:
        joined = " ".join(texts)
        result: list[str] = []
        for provider in _KNOWN_PROVIDERS:
            if re.search(re.escape(provider), joined, re.IGNORECASE):
                if provider == "7телеком" and "+7телеком" in result:
                    continue
                result.append(provider)
        return result


class StoryCardRenderer:
    """Render normalized Story Cards as a short thematic article."""

    def render(self, cards: list[StoryCard]) -> ArticleDraft:
        if not cards:
            raise NoSubstantiveMaterialError("no story cards to render")
        sections: list[ArticleSection] = []
        for card in cards:
            paragraphs = self._paragraphs_for_card(card)
            if paragraphs:
                sections.append(ArticleSection(self._heading(card.topic), paragraphs[:3]))
        if not sections:
            raise NoSubstantiveMaterialError("story cards contain no renderable material")
        headings = [section.heading.lower() for section in sections[:3]]
        if len(headings) == 1:
            lead = f"За последние сутки в городе обсуждали тему: {headings[0]}."
        else:
            lead = "За последние сутки в городе обсуждали " + ", ".join(headings) + "."
        return ArticleDraft(
            headline="Что происходило в городе за сутки",
            lead=lead,
            paragraphs=[],
            sections=sections,
        )

    def _paragraphs_for_card(self, card: StoryCard) -> list[str]:
        paragraphs: list[str] = []
        if card.hard_facts:
            paragraphs.append(self._render_element(card.hard_facts[0]))
        if card.community_observations:
            observation = self._render_element(card.community_observations[0])
            if observation not in paragraphs:
                paragraphs.append(observation)
        if card.uncertainties:
            uncertainty = card.uncertainties[0].text.strip()
            if uncertainty and uncertainty not in paragraphs:
                paragraphs.append(uncertainty)
        return paragraphs

    @staticmethod
    def _render_element(element: StoryElement) -> str:
        """Render normalized text while retaining attribution for attributed sources."""
        text = element.text.strip()
        if element.status == "established":
            return text
        if text.startswith(("Жители ", "Источник ", "Источники ")):
            return text
        source = element.attribution.strip()
        if source and source not in {"Источник", "Жители"}:
            return f"{source} сообщил: {text}"
        return text

    @staticmethod
    def _heading(topic: str) -> str:
        return _TOPIC_HEADINGS.get(topic, topic.capitalize())
