from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from src.editorial_models import StoryCard
from src.publication.article_claims import extract_concrete_claims
from src.publication.article_context import ArticleEditorialContext, ArticleSupport

_STORY_ID_RE = re.compile(r"story:(?:[^:]+|\d+)")

ArticleProminence = Literal["DEVELOP", "WEAVE", "BRIEF"]

_DETAIL_LIMIT: dict[ArticleProminence, int] = {
    "DEVELOP": 4,
    "WEAVE": 2,
    "BRIEF": 1,
}


@dataclass(frozen=True)
class ArticleStoryCoverage:
    story_id: str
    topic: str
    rank: int
    prominence: ArticleProminence
    support_ids: tuple[str, ...]
    detail_support_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArticleStoryAssignment:
    story_id: str
    section_id: str
    depth: ArticleProminence
    rank: int
    primary_evidence_ids: tuple[str, ...]
    concrete_details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArticleThematicSection:
    section_id: str
    title: str
    lead_story_id: str
    story_assignments: tuple[ArticleStoryAssignment, ...]
    narrative_intent: str


@dataclass(frozen=True)
class ArticleCoveragePlan:
    stories: tuple[ArticleStoryCoverage, ...]
    sections: tuple[ArticleThematicSection, ...] = ()

    @property
    def story_ids(self) -> tuple[str, ...]:
        return tuple(item.story_id for item in self.stories)

    @property
    def by_story_id(self) -> dict[str, ArticleStoryCoverage]:
        return {item.story_id: item for item in self.stories}

    @property
    def support_ids_by_story(self) -> dict[str, tuple[str, ...]]:
        return {item.story_id: item.support_ids for item in self.stories}

    @property
    def by_section_id(self) -> dict[str, ArticleThematicSection]:
        return {sec.section_id: sec for sec in self.sections}

    def section_for_story(self, story_id: str) -> ArticleThematicSection | None:
        for sec in self.sections:
            if any(a.story_id == story_id for a in sec.story_assignments):
                return sec
        return None


def _story_id_from_support_id(support_id: str) -> str:
    match = _STORY_ID_RE.search(support_id)
    return match.group(0) if match else ""


def _publish_supports_by_story(
    context: ArticleEditorialContext,
) -> dict[str, tuple[ArticleSupport, ...]]:
    grouped: dict[str, list[ArticleSupport]] = defaultdict(list)
    for support in context.support_index:
        if support.publication_use != "PUBLISH":
            continue
        if support.evidence_kind == "resident_question":
            continue
        story_id = support.story_id or _story_id_from_support_id(support.support_id)
        if not story_id:
            continue
        grouped[story_id].append(support)
    return {story_id: tuple(items) for story_id, items in grouped.items()}


def _prominence_legacy(card: StoryCard, support_count: int) -> ArticleProminence:
    if card.importance == "high" or support_count >= 4:
        return "DEVELOP"
    if support_count >= 2:
        return "WEAVE"
    return "BRIEF"


def score_detail_support(support: ArticleSupport) -> int:
    text = " ".join(part for part in (support.text, support.source_text) if part).strip()
    if not text:
        return 0

    score = 0
    claims = extract_concrete_claims(text)
    if claims:
        score += 3
    if support.evidence_kind in {
        "community_report",
        "service_access",
        "operational_observation",
    }:
        score += 2
    if len(support.source_text.split()) >= 8:
        score += 1
    if any(marker in support.source_text for marker in ("«", "»", '"')):
        score += 1
    return score


def _detail_support_ids(
    supports: tuple[ArticleSupport, ...],
    prominence: ArticleProminence,
) -> tuple[str, ...]:
    ranked = sorted(
        supports,
        key=lambda s: (-score_detail_support(s), s.support_id),
    )
    positive = [s for s in ranked if score_detail_support(s) > 0]
    chosen = positive or list(ranked)
    return tuple(s.support_id for s in chosen[: _DETAIL_LIMIT[prominence]])


def _story_topic_signature(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
) -> str:
    """Determine fine-grained subtopic signature to cluster closely related civic reports."""
    sups = getattr(context, "support_by_id", {})
    text_parts = [story.topic]
    for sid in story.support_ids:
        sup = sups.get(sid)
        if sup and getattr(sup, "text", ""):
            text_parts.append(sup.text)
        elif sup and getattr(sup, "source_text", ""):
            text_parts.append(sup.source_text)
    text = " ".join(text_parts).casefold()
    if any(
        w in text for w in ("электр", "свет", "напряжен", "подстанци", "питани", "энерг", "блэкаут")
    ):
        return "power"
    if any(w in text for w in ("вод", "водоканал", "водовод", "труб", "порыв", "насос")):
        return "water"
    if any(
        w in text
        for w in (
            "интернет",
            "связь",
            "провайдер",
            "роутер",
            "юпитер",
            "терминал",
            "повербанк",
            "миранд",
            "миртелеком",
        )
    ):
        return "internet"
    if any(
        w in text
        for w in ("автобус", "маршрут", "транспорт", "рейс", "проезд", "такси", "дорог", "трасс")
    ):
        return "transport"
    if any(
        w in text
        for w in (
            "кошк",
            "кот",
            "собак",
            "пес",
            "пёс",
            "щен",
            "котен",
            "котён",
            "животн",
            "питом",
        )
    ):
        return "pets"
    if any(
        w in text
        for w in ("спорт", "футбол", "секц", "школ", "набор", "дети", "девоч", "тренировк")
    ):
        return "sports"
    if any(
        w in text
        for w in (
            "бабушк",
            "бабул",
            "пожил",
            "пропал",
            "поиск",
            "потер",
            "полици",
            "скорая",
            "пропавш",
        )
    ):
        return "missing_person"
    if any(
        w in text
        for w in (
            "магазин",
            "товар",
            "торговл",
            "рынок",
            "проспект",
            "сакура",
            "мясокомбинат",
            "продукци",
            "вывоз",
        )
    ):
        return "retail"
    if any(
        w in text
        for w in ("взрыв", "хлопок", "прилет", "обстрел", "бпла", "дрон", "пво", "вибраци", "волна")
    ):
        return "incident"
    if any(w in text for w in ("рецепт", "кулинар", "кухн")):
        return "culinary"
    return story.story_id


def build_article_coverage_plan(
    cards: Sequence[StoryCard],
    context: ArticleEditorialContext,
    develop_story_budget: int = 2,
) -> ArticleCoveragePlan:
    support_map = _publish_supports_by_story(context)
    card_list = list(cards)
    if not card_list:
        card_list = [
            StoryCard(id=sid, topic=sid, importance="medium", summary=sid)
            for sid in support_map.keys()
        ]

    valid_cards = [c for c in card_list if c.id in support_map and support_map[c.id]]

    signals = context.selection_by_story
    if signals:
        card_intents: list[tuple[StoryCard, str, int]] = []
        for c in valid_cards:
            selection_signal = signals.get(c.id) or signals.get(c.id.removeprefix("story:"))
            intent = selection_signal.intent if selection_signal is not None else "brief"
            rank_val = (
                selection_signal.rank
                if (selection_signal is not None and isinstance(selection_signal.rank, int))
                else 9999
            )
            card_intents.append((c, intent, rank_val))

        lead_assigned = False
        effective_intents: list[tuple[StoryCard, str, int]] = []
        for c, intent, r in card_intents:
            if intent == "lead":
                if not lead_assigned:
                    effective_intents.append((c, "lead_develop", r))
                    lead_assigned = True
                else:
                    effective_intents.append((c, "normal", r))
            else:
                effective_intents.append((c, intent, r))

        normal_cards = [item for item in effective_intents if item[1] == "normal"]
        sorted_normals = sorted(normal_cards, key=lambda x: x[2])
        elevated_normal_ids = {
            item[0].id for item in sorted_normals[: max(0, develop_story_budget)]
        }

        prominence_by_card_id: dict[str, ArticleProminence] = {}
        for c, eff_intent, _ in effective_intents:
            if eff_intent == "lead_develop":
                prominence_by_card_id[c.id] = "DEVELOP"
            elif eff_intent == "normal":
                if c.id in elevated_normal_ids:
                    prominence_by_card_id[c.id] = "DEVELOP"
                else:
                    prominence_by_card_id[c.id] = "WEAVE"
            elif eff_intent == "follow_up":
                prominence_by_card_id[c.id] = "WEAVE"
            elif eff_intent in ("unverified_operational", "brief"):
                prominence_by_card_id[c.id] = "BRIEF"
            else:
                prominence_by_card_id[c.id] = "BRIEF"

        stories: list[ArticleStoryCoverage] = []
        for rank, card in enumerate(valid_cards, start=1):
            supports = support_map[card.id]
            prominence = prominence_by_card_id.get(card.id, "BRIEF")
            stories.append(
                ArticleStoryCoverage(
                    story_id=card.id,
                    topic=card.topic or card.summary or card.id,
                    rank=rank,
                    prominence=prominence,
                    support_ids=tuple(s.support_id for s in supports),
                    detail_support_ids=_detail_support_ids(supports, prominence),
                )
            )
    else:
        stories = []
        for rank, card in enumerate(valid_cards, start=1):
            supports = support_map[card.id]
            prominence = _prominence_legacy(card, len(supports))
            stories.append(
                ArticleStoryCoverage(
                    story_id=card.id,
                    topic=card.topic or card.summary or card.id,
                    rank=rank,
                    prominence=prominence,
                    support_ids=tuple(s.support_id for s in supports),
                    detail_support_ids=_detail_support_ids(supports, prominence),
                )
            )

    card_map = {c.id: c for c in valid_cards}

    # Subtopic support pooling:
    # When multiple stories describe the same practical subject within a domain
    # (e.g. multiple reports about outages and voltage drops across different streets, or multiple retail closures),
    # pool their support IDs so that comprehensive narrative coverage of the topic credits all related stories.
    subtopic_sups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for s in stories:
        c_item = card_map.get(s.story_id)
        sec_id = _thematic_section_id(c_item) if c_item else "city_life"
        subtopic_signature = _story_topic_signature(s, context)
        subtopic_sups[(sec_id, subtopic_signature)].extend(s.support_ids)

    pooled_stories: list[ArticleStoryCoverage] = []
    for s in stories:
        c_item = card_map.get(s.story_id)
        sec_id = _thematic_section_id(c_item) if c_item else "city_life"
        subtopic_signature = _story_topic_signature(s, context)
        cluster_sups = tuple(
            dict.fromkeys(list(s.support_ids) + subtopic_sups[(sec_id, subtopic_signature)])
        )
        pooled_stories.append(
            ArticleStoryCoverage(
                story_id=s.story_id,
                topic=s.topic,
                rank=s.rank,
                prominence=s.prominence,
                support_ids=cluster_sups,
                detail_support_ids=s.detail_support_ids,
            )
        )
    stories = pooled_stories

    stories_by_section: dict[str, list[ArticleStoryCoverage]] = defaultdict(list)
    for s in stories:
        c_item = card_map.get(s.story_id)
        sec_id = _thematic_section_id(c_item) if c_item else "city_life"
        stories_by_section[sec_id].append(s)

    sec_defs = list(_THEMATIC_SECTIONS_DEF)
    active_sec_ids = [sdef[0] for sdef in sec_defs if stories_by_section.get(sdef[0])]
    if len(active_sec_ids) < 3 and len(stories) >= 3:
        for sdef in sec_defs:
            sid = sdef[0]
            if sid not in stories_by_section or not stories_by_section[sid]:
                active_donor_keys = [
                    k for k in stories_by_section.keys() if len(stories_by_section[k]) >= 2
                ]
                if active_donor_keys:
                    donor_sid = max(active_donor_keys, key=lambda k: len(stories_by_section[k]))
                    moved = stories_by_section[donor_sid].pop()
                    stories_by_section[sid].append(moved)
            if len([k for k, v in stories_by_section.items() if v]) >= 3:
                break

    sections: list[ArticleThematicSection] = []
    for sec_id, sec_title, sec_intent in _THEMATIC_SECTIONS_DEF:
        sec_stories = stories_by_section.get(sec_id)
        if not sec_stories:
            continue
        sorted_sec_stories = sorted(sec_stories, key=lambda s: s.rank)
        lead_story_id = sorted_sec_stories[0].story_id
        assignments = tuple(
            ArticleStoryAssignment(
                story_id=s.story_id,
                section_id=sec_id,
                depth=s.prominence,
                rank=s.rank,
                primary_evidence_ids=s.support_ids[:3],
                concrete_details=s.detail_support_ids,
            )
            for s in sorted_sec_stories
        )
        sections.append(
            ArticleThematicSection(
                section_id=sec_id,
                title=sec_title,
                lead_story_id=lead_story_id,
                story_assignments=assignments,
                narrative_intent=sec_intent,
            )
        )

    return ArticleCoveragePlan(stories=tuple(stories), sections=tuple(sections))


_THEMATIC_SECTIONS_DEF: tuple[tuple[str, str, str], ...] = (
    (
        "infrastructure",
        "Жизнеобеспечение и коммунальная обстановка",
        "Комплексная картина работы коммунальных сетей, подачи электричества, воды и устранения аварийных ситуаций.",
    ),
    (
        "communications",
        "Связь, интернет и цифровые сервисы",
        "Работа мобильных операторов, интернет-провайдеров, перебои со связью и каналы поддержки абонентов.",
    ),
    (
        "mobility",
        "Городской транспорт и логистика",
        "Работа городского общественного транспорта, междугородние рейсы, состояние дорог и дорожная ситуация.",
    ),
    (
        "city_life",
        "Городская хроника, торговля и происшествия",
        "Повседневная жизнь города, продовольствие, торговые сети, происшествия и бытовые решения горожан.",
    ),
    (
        "society",
        "Социальная сфера, гуманитарная обстановка и медицина",
        "Работа медицинских учреждений, социальные выплаты, гуманитарная помощь и поддержка жителей.",
    ),
    (
        "culture_education",
        "Образование, дети и городские события",
        "Учебный процесс в школах, детские секции, спортивные и культурные мероприятия в городе.",
    ),
)


def _thematic_section_id(card: StoryCard | None) -> str:
    if card is None:
        return "city_life"
    rid = (getattr(card, "rubric_id", "") or "").casefold()
    cat = (getattr(card, "category", "") or "").casefold()
    topic = (getattr(card, "topic", "") or "").casefold()
    summary = (getattr(card, "summary", "") or "").casefold()
    tags = {str(t).casefold() for t in getattr(card, "tags", []) or []}
    text = f"{rid} {cat} {topic} {summary} {' '.join(tags)}".casefold()
    tokens = set(re.findall(r"[a-zа-яё0-9]+", text))

    # Priority 1: Infrastructure (power, water, gas, heating, utilities)
    # Checked first so that reports like "voltage in the area of school #16" stay in infrastructure
    infra_prefixes = (
        "электр",
        "свет",
        "напряжен",
        "вольт",
        "подстанци",
        "энерг",
        "вод",
        "водоканал",
        "водовод",
        "водопровод",
        "протечк",
        "порыв",
        "труб",
        "газ",
        "отоплен",
        "котельн",
        "теплоснабжен",
        "жкх",
        "коммунал",
        "блэкаут",
        "вспышк",
        "замыкан",
        "обесточ",
        "авари",
    )
    if any(any(tok.startswith(p) for p in infra_prefixes) for tok in tokens):
        return "infrastructure"

    # Priority 2: Communications & Telecom
    comm_prefixes = (
        "интернет",
        "провайдер",
        "юпитер",
        "миранд",
        "миртелеком",
        "роутер",
        "терминал",
        "повербанк",
        "wifi",
        "wi-fi",
        "связ",
    )
    if any(any(tok.startswith(p) for p in comm_prefixes) for tok in tokens):
        return "communications"

    # Priority 3: Mobility & Transport
    mob_prefixes = (
        "транспорт",
        "автобус",
        "маршрут",
        "рейс",
        "проезд",
        "дорог",
        "трасс",
        "такси",
        "водител",
        "автомоб",
        "пдд",
        "вожден",
    )
    if any(any(tok.startswith(p) for p in mob_prefixes) for tok in tokens):
        return "mobility"

    # Priority 3.5: Pets and animals (stay in city_life, not hijacked by school landmarks or missing persons)
    pet_prefixes = (
        "кошк",
        "кот",
        "собак",
        "пес",
        "пёс",
        "щен",
        "котен",
        "котён",
        "животн",
        "питом",
    )
    if any(any(tok.startswith(p) for p in pet_prefixes) for tok in tokens):
        return "city_life"

    # Priority 4: Education, Children & Culture (genuine education/sports topics)
    edu_prefixes = (
        "школ",
        "детсад",
        "садик",
        "спорт",
        "футбол",
        "секци",
        "тренировк",
        "дети",
        "культура",
        "музей",
    )
    if any(any(tok.startswith(p) for p in edu_prefixes) for tok in tokens):
        return "culture_education"

    # Priority 5: Society, Healthcare, Social assistance, Missing persons
    soc_prefixes = (
        "медицин",
        "больниц",
        "поликлиник",
        "врач",
        "скорая",
        "полици",
        "пожил",
        "бабушк",
        "бабул",
        "пропал",
        "поиск",
        "помощ",
        "пенсион",
        "пособи",
    )
    if any(any(tok.startswith(p) for p in soc_prefixes) for tok in tokens):
        return "society"

    return "city_life"
