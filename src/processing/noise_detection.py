"""Deterministic detectors for commercial ads, private classifieds, directory payload, and chatter noise.

Shared single source of truth for raw text pattern recognition used by both
the fragmenter (for pre-embedding filtering) and Gate V2 (for post-triage hard exclusion).
"""

from __future__ import annotations

import re

MIN_CANDIDATE_ALNUM_CHARS = 6

_SHORT_REACTION_PATTERN = re.compile(
    r"^(?:ок|окей|да|нет|ага|угу|понял(?:а)?|понятно|ясно|хорошо|ладно|норм|супер|класс|лол|привет|хз)[!.?]*$",
    re.IGNORECASE,
)

_UNCERTAINTY_PATTERN = re.compile(
    r"^(?:не знаю|хз|без понятия|кто (?:его )?знает|никто не знает|хз[,.]?\s*сам в шоке)[!.?]*$",
    re.IGNORECASE,
)

# Common URL pattern
URL_PATTERN = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
WHITESPACE_PATTERN = re.compile(r"\s+")
NON_ALPHANUM_PATTERN = re.compile(r"[^\w\s]", re.UNICODE)

# Patterns for commercial classified ads (multi-cue required)
CLASSIFIED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(продам|куплю|сдам|сдаю|сниму|аренда|продается|продаётся)\b", re.IGNORECASE),
    re.compile(r"\b(цена|стоимость|руб|рублей|р\.|грн|usd|\$|₽)\b.*\b\d+\b", re.IGNORECASE),
    re.compile(r"\b\d+\b.*\b(руб|рублей|р\.|грн|usd|\$|₽)\b", re.IGNORECASE),
    re.compile(r"\b\d+\s*([₽р]|руб|грн)(?:/(?:литр|л|кг|шт|час|м2|сотку))?\b", re.IGNORECASE),
    re.compile(r"\b(звонить|обращаться|писать в лс|л\.с\.|самовывоз|доставка)\b", re.IGNORECASE),
    re.compile(r"(\+7\s?9\d{2}|\+380|\b89\d{2})\s?\d{3}", re.IGNORECASE),
    re.compile(r"\b(маникюр|педикюр|наращивание|ресниц|брови|эпиляция|стрижка)\b", re.IGNORECASE),
    re.compile(
        r"\b(грузоперевозки|грузчики|переезды|ремонт квартир|установка окон|бурение скважин)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(в наличии|под заказ|розлив|оптом и в розницу|скидк[аи]|распродаж[аи]|акци[яи])\b",
        re.IGNORECASE,
    ),
)

# Specific pattern for retail price/commodity offers (e.g., "3 ₽/литр", "вода на розлив — 3 руб")
_COMMODITY_SALE_PATTERN = re.compile(
    r"(?:на розлив|прода(?:жа|ем|ется)|в наличии|цена)\s*[-—–:]?\s*\d+\s*(?:[₽р]|руб|грн)(?:/(?:литр|л|кг|бут))?",
    re.IGNORECASE,
)
_PER_UNIT_PRICE_PATTERN = re.compile(
    r"\b\d+\s*(?:[₽р]|руб|грн)/(?:литр|л|кг|бут|шт)\b",
    re.IGNORECASE,
)

# Patterns for private classifieds: lost & found, private bulletin board items
_LOST_AND_FOUND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(потеряли|потерял|потеряла|утерян[оыа]?|найден[оыа]?|нашли)\b.*\b(ключ|ключи|документ|документы|паспорт|права|кошелек|кошелёк|карту|карточка|телефон|номер)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(нашедшего|нашедших|кто нашел|кто нашёл)\s+(?:прошу|просьба|напишите|вернуть|позвонить)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bпрошу написать в личку\b", re.IGNORECASE),
)

# Patterns for directory payload: store product/service adverts without civic news
_DIRECTORY_PAYLOAD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:магазин|склад|павильон|бутик|отдел|база)\s+[«\"]?\w+[»\"]?.*(?:хорошие цены|низкие цены|большой выбор|в наличии и под заказ|ждём вас|скидки)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:в наличии и под заказ|под заказ и в наличии|хорошие цены|по доступным ценам)\b",
        re.IGNORECASE,
    ),
)

# Patterns for short chatter, greetings, reaction noise
NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:доброе утро|добрый день|добрый вечер|спокойной ночи|всем привет|привет всем)[!.]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:спасибо|благодарю|пожалуйста|не за что|плюсую|согласен|согласна)[!.]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:подскажите пожалуйста|подскажите|кто знает|есть у кого)[?.]*$",
        re.IGNORECASE,
    ),
)


def is_question_only(text: str) -> bool:
    """Detect if fragment is a standalone question without any factual assertion or answer."""
    t = text.strip()
    if not t.endswith("?"):
        lower = t.lower()
        if lower.startswith(("подскажите", "кто знает", "где можно", "есть у кого")):
            return True
        return False

    # Check if there is text after the final question mark
    after_last_q = t[t.rfind("?") + 1 :].strip()
    if after_last_q and any(c.isalnum() for c in after_last_q):
        return False

    parts = [p.strip() for p in t.split("?") if p.strip()]
    if not parts:
        return True

    for part in parts:
        if ("." in part or "!" in part) and len(part) > 50:
            sub = [s.strip() for s in part.replace("!", ".").split(".") if len(s.strip()) > 20]
            if len(sub) > 1:
                return False

    return True


def detect_classified_cues(text: str) -> list[str]:
    """Return all matched classified ad pattern identifiers."""
    cues: list[str] = []
    for idx, pattern in enumerate(CLASSIFIED_PATTERNS):
        if pattern.search(text):
            cues.append(f"cue_{idx}")
    return cues


def is_commercial_classified(text: str) -> bool:
    """Detect commercial sales offer, promotional listing, or paid service ad."""
    trimmed = text.strip()
    if not trimmed:
        return False
    # Direct commodity price match (e.g. "Питьевая вода на розлив — 3 ₽/литр")
    if _COMMODITY_SALE_PATTERN.search(trimmed) or _PER_UNIT_PRICE_PATTERN.search(trimmed):
        return True
    cues = detect_classified_cues(trimmed)
    return len(cues) >= 2


def is_private_classified(text: str) -> bool:
    """Detect private bulletin board notices: lost & found items, personal searches."""
    trimmed = text.strip()
    if not trimmed:
        return False
    for pattern in _LOST_AND_FOUND_PATTERNS:
        if pattern.search(trimmed):
            return True
    return False


def is_directory_payload(text: str) -> bool:
    """Detect store/shop promotional directory listings with in-stock/ordering copy."""
    trimmed = text.strip()
    if not trimmed:
        return False
    for pattern in _DIRECTORY_PAYLOAD_PATTERNS:
        if pattern.search(trimmed):
            return True
    return False


def is_obvious_noise(text: str) -> tuple[bool, str | None]:
    """Check text against deterministic noise, short chatter, and emptiness rules."""
    trimmed = text.strip()
    if not trimmed:
        return True, "empty"

    if _SHORT_REACTION_PATTERN.search(trimmed):
        return True, "obvious_noise"

    if _UNCERTAINTY_PATTERN.search(trimmed):
        return True, "obvious_noise"

    for p in NOISE_PATTERNS:
        if p.search(trimmed):
            return True, "obvious_noise"

    alnum_chars = sum(1 for c in trimmed if c.isalnum())
    if alnum_chars < MIN_CANDIDATE_ALNUM_CHARS:
        return True, "too_short"

    return False, None


def classify_text_noise_or_exclusion(
    text: str, *, check_length: bool = False
) -> tuple[bool, str | None]:
    """Unified classifier for fragment filtering and Gate postcondition checking."""
    is_noise, noise_reason = is_obvious_noise(text)
    if is_noise:
        if check_length or noise_reason == "obvious_noise":
            return True, "obvious_noise"

    if is_commercial_classified(text):
        return True, "commercial_classified"

    if is_private_classified(text):
        return True, "private_classified"

    if is_directory_payload(text):
        return True, "directory_payload"

    return False, None
