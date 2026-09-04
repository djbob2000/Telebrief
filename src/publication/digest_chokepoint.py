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


def filter_digest_candidate_cards(
    cards: Sequence[StoryCard],
    max_cards: int = 25,
) -> list[StoryCard]:
    """Filter out commercial ads, spam, and chat fluff from candidate story cards."""
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
    filtered.sort(key=lambda x: importance_order.get(getattr(x, "importance", "low"), 2))
    return filtered[:max_cards]
