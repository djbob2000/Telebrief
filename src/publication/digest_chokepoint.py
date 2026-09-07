"""Quality chokepoint for digest candidates: filtering out commercial spam, ads, and chatter."""

from __future__ import annotations

import re
from typing import Sequence

from src.editorial_models import StoryCard

_AD_AND_SPAM_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Commercial passenger transport / tickets / foreign trips with pricing
    re.compile(
        r"(?:рейс[ыа]|поездк[иа]|пассажирские\s+перевозки|едем\s*:).{0,60}(?:450\$|\$\s*\d+|\bруб\b|места\s+есть|бронирован)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:Киев|Польш|Грузи|Батуми|Тбилиси|Ереван).{0,50}(?:пассажирские|перевозк|бронир|выезд)",
        re.IGNORECASE,
    ),
    # Private grey market / banking services / card unlock / pension brokers
    re.compile(
        r"(?:разблокировк|актуализаци|верификаци).{0,40}(?:карт|счетов|пенси|пф|приват|сбер|личных\s+кабинетов)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:оформление|помощь).{0,30}(?:ецп|эцп|пенсий|карт).{0,40}(?:@|\+7|тел)",
        re.IGNORECASE,
    ),
    # Telegram handles and phone numbers soliciting business
    re.compile(r"(?:telegram|тг|тел|viber|whatsapp)\s*:\s*(?:@\w+|\+?7\d{10})", re.IGNORECASE),
    # Clinic price lists / medical services catalogues
    re.compile(
        r"(?:список\s+доступных\s+(?:специалистов|услуг)|узи серде|кукушка|плазмалифтинг|лечебный\s+массаж)",
        re.IGNORECASE,
    ),
    # Commercial establishments promotions / sales
    re.compile(
        r"(?:столовая\s+«?чили»|скидк\w+\s+на\s+одежд|магазин\s+(?:женской\s+)?одежды|продажа\s+торгового\s+помещения)",
        re.IGNORECASE,
    ),
    # Empty chat chatter / flood
    re.compile(r"на\s+акз\s+все\s+живые", re.IGNORECASE),
    re.compile(
        r"(?:летает\s+что-то\s+мелкое|ничего\s+не\s+покупайте\s+по\s+предоплате|светофор\s+мигнул\s+и\s+выключился)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:дарья\s+стенковая|студия\s+по\s+мазина)", re.IGNORECASE),
)


def is_noise_or_ad(text: str) -> bool:
    clean = text.strip()
    if not clean:
        return True
    return any(p.search(clean) for p in _AD_AND_SPAM_PATTERNS)


def detect_topic_family(card: StoryCard) -> str:
    """Classify a story card into a core municipal domain to prevent single-incident crowding."""
    text = f"{card.topic or ''} {card.summary or ''} {card.rubric_id or ''}".lower()
    if any(w in text for w in ("газ", "горгаз", "104", "концентраци", "одорант")):
        return "gas"
    if any(
        w in text
        for w in ("свет", "электр", "блэкаут", "трансформатор", "обрыв", "лэп", "генератор")
    ):
        return "electricity"
    if any(w in text for w in ("вод", "водоканал", "напор", "скважин", "водоснабж")):
        return "water"
    if any(w in text for w in ("взрыв", "бпла", "пво", "стрельб", "прилет", "обстрел", "сирен")):
        return "security"
    if any(w in text for w in ("автобус", "маршрутк", "дорог", "такси", "трасс", "проезд")):
        return "transport"
    if any(w in text for w in ("связ", "интернет", "провайдер", "мобильн", "вышк")):
        return "communications"
    if any(w in text for w in ("больниц", "врач", "поликлиник", "аптек", "медицин", "лекарств")):
        return "health"
    if any(w in text for w in ("пенси", "пособи", "соцзащит", "выплат", "соцфонд", "пфр")):
        return "social"
    return card.rubric_id or card.topic or "other"


def filter_digest_candidate_cards(
    cards: Sequence[StoryCard],
    max_cards: int = 35,
    max_per_topic: int = 4,
) -> list[StoryCard]:
    """Filter out commercial ads, spam, and chat fluff from candidate story cards,
    while balancing topics to ensure diverse civic coverage (electricity, water, security, etc.)
    instead of letting a single multi-cluster incident crowd out all slots."""
    filtered: list[StoryCard] = []
    for c in cards:
        combined = f"{c.topic or ''} {c.summary or ''}"
        facts_text = " ".join(f.text for f in getattr(c, "hard_facts", ()) if f.text)
        obs_text = " ".join(o.text for o in getattr(c, "community_observations", ()) if o.text)
        full_text = f"{combined} {facts_text} {obs_text}".strip()

        if is_noise_or_ad(full_text):
            continue

        if len((c.topic or "") + (c.summary or "")) < 15 and not facts_text:
            continue

        filtered.append(c)

    importance_order = {"high": 0, "medium": 1, "low": 2}
    # Sort initially by importance, then ID
    filtered.sort(key=lambda x: (importance_order.get(getattr(x, "importance", "low"), 2), x.id))

    # Group into topic families
    by_family: dict[str, list[StoryCard]] = {}
    for c in filtered:
        family = detect_topic_family(c)
        by_family.setdefault(family, []).append(c)

    # Balanced selection: take up to max_per_topic per family in importance order
    selected: list[StoryCard] = []
    selected_ids: set[str] = set()

    for fam_cards in by_family.values():
        for c in fam_cards[:max_per_topic]:
            if len(selected) < max_cards:
                selected.append(c)
                selected_ids.add(c.id)

    # Final presentation order: high importance first
    selected.sort(key=lambda x: (importance_order.get(getattr(x, "importance", "low"), 2), x.id))
    return selected
