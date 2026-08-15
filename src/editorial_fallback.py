"""Deterministic thematic digest used when all editorial AI paths are unavailable."""

from __future__ import annotations

import re
from collections import defaultdict

from src.editorial_models import (
    PreparedBundle,
    SourceRecord,
    StoryCard,
    StoryElement,
    is_expected_language,
)
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
    "other": "Прочее",
}

_NEGATIVE_SIGNAL = re.compile(
    r"(?:нет|не\s+работ|пропал|исчез|перебо|плох|сбой|отключ)", re.IGNORECASE
)


class DeterministicStoryCardBuilder:
    """Build short editorial summaries without copying source messages into cards."""

    def build(self, bundle: PreparedBundle) -> list[StoryCard]:
        grouped: dict[str, list[tuple[str, SourceRecord]]] = defaultdict(list)
        unclassified: list[tuple[str, SourceRecord]] = []
        for ref, record in bundle.records.items():
            topic = self._topic_for(record.message.text)
            if topic is None:
                unclassified.append((ref, record))
            else:
                grouped[topic].append((ref, record))
        if len(unclassified) >= 2 or any(
            record.source_type in {"official", "news"} for _, record in unclassified
        ):
            grouped["other"].extend(unclassified)
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
    def _topic_for(text: str) -> str | None:
        normalized = text.lower()
        scores = {
            topic: sum(normalized.count(keyword) for keyword in keywords)
            for topic, keywords in _TOPIC_KEYWORDS.items()
        }
        topic, score = max(scores.items(), key=lambda item: item[1])
        return topic if score else None

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
        areas = self._extract_areas(entries)
        providers = self._extract_providers(entries)
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
            "other": "городских событиях",
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
            if _NEGATIVE_SIGNAL.search(joined):
                return "Жители сообщали о перебоях с электроснабжением."
            return "Жители обсуждали генераторы и вопросы электроснабжения."
        if topic == "water":
            if _NEGATIVE_SIGNAL.search(joined):
                return "Жители сообщали о перебоях с водоснабжением."
            return "Жители обсуждали вопросы водоснабжения."
        if topic == "incidents":
            if "взрыв" in joined or "громк" in joined:
                return "Жители сообщили о громком звуке или взрыве."
            return "Жители сообщали о происшествии."
        return {
            "transport": "Жители обсуждали ситуацию на дорогах и в транспорте.",
            "social": "Жители обсуждали вопросы социальной сферы.",
            "other": "Жители обсуждали другие городские события.",
        }.get(topic, "Жители обсуждали городские бытовые вопросы.")

    @staticmethod
    def _importance(topic: str, entries: list[tuple[str, SourceRecord]]) -> str:
        if topic in {"incidents", "electricity", "water"}:
            return "high"
        if len(entries) > 2:
            return "medium"
        return "low"

    @staticmethod
    def _extract_areas(entries: list[tuple[str, SourceRecord]]) -> list[str]:
        areas: list[str] = []
        seen: set[str] = set()
        for _, record in entries:
            ctx = getattr(record, "city_context", None)
            if not ctx or not getattr(ctx, "entities", None):
                continue
            for entity in ctx.entities:
                if entity.kind in {"area", "place"}:
                    name = entity.canonical_name or entity.entity_id
                    if name and name not in seen:
                        seen.add(name)
                        areas.append(name)
        return areas

    @staticmethod
    def _extract_providers(entries: list[tuple[str, SourceRecord]]) -> list[str]:
        providers: list[str] = []
        seen: set[str] = set()
        for _, record in entries:
            ctx = getattr(record, "city_context", None)
            if not ctx or not getattr(ctx, "entities", None):
                continue
            for entity in ctx.entities:
                if entity.kind == "provider":
                    name = entity.canonical_name or entity.entity_id
                    if name and name not in seen:
                        seen.add(name)
                        providers.append(name)
        return providers


_GENERIC_ATTRIBUTIONS = {
    "residents in community chat": "Жители в чате",
    "multiple residents in community chat": "Жители в чате",
    "residents in chat": "Жители в чате",
    "multiple residents in chat": "Жители в чате",
    "residents": "Жители",
    "multiple residents": "Жители",
    "resident reports and absence of official statements in corpus": "По сообщениям жителей",
    "parents and a school director's statement as relayed by a resident": "Родители и руководство школ",
}


class StoryCardRenderer:
    """Render normalized Story Cards as a short thematic article."""

    def __init__(self, output_language: str = "Russian"):
        self.output_language = output_language

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
        draft = ArticleDraft(
            headline="Что происходило в городе за сутки",
            lead=lead,
            paragraphs=[],
            sections=sections,
        )
        if any(
            not is_expected_language(unit.text, self.output_language)
            for unit in draft.audit_units().values()
        ):
            raise ValueError(f"rendered draft language mismatch: expected {self.output_language}")
        return draft

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

    @classmethod
    def _render_element(cls, element: StoryElement) -> str:
        """Render normalized text while retaining attribution for attributed sources."""
        text = element.text.strip()
        if element.status == "established":
            return text
        if text.startswith(
            ("Жители ", "Источник ", "Источники ", "По сообщениям ", "В чатах ", "Родители ")
        ):
            return text
        raw_source = element.attribution.strip()
        if not raw_source:
            return text
        norm_source = _GENERIC_ATTRIBUTIONS.get(raw_source.lower(), raw_source)
        if norm_source in {"Источник", "Жители"}:
            return text
        if norm_source in {
            "Жители в чате",
            "Жители",
            "Родители и руководство школ",
        } or norm_source.endswith("жители"):
            return f"{norm_source} сообщали: {text}"
        if norm_source.startswith("По сообщениям"):
            return f"{norm_source}: {text}"
        return f"{norm_source} сообщил: {text}"

    @staticmethod
    def _heading(topic: str) -> str:
        return _TOPIC_HEADINGS.get(
            topic.lower(), topic.capitalize() if topic else "Городские события"
        )
