"""Recognize a resident's outage complaint that names no place at all.

A report with no location tells a digest reader nothing: the speaker may live
anywhere. The check is deliberately closed. Every word of the reported fact must
belong to a small status vocabulary, so a street, a provider, a cause or any other
unknown word keeps the report. Evidence stays internal; only the digest skips it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_DURATION = re.compile(
    r"\b\d{1,3}\s*(?:-?[а-яё]{0,3}\s*)?(?:дн[яе][йм]?|день|сут\w*|час\w*|недел\w*|месяц\w*)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

# Attribution, the speaker's own home, the service, its absence, and time.
_STATUS_WORD = re.compile(
    r"(?:по|сообщени[юя]м?|словам|жител[ьяюейм]+|жительниц[аыеу]|местн\w+|автор\w*"
    r"|сообща\w+|сообщил\w*|пиш\w+|написал\w*|отмеч\w+|заявл\w+|заяв\w+|утвержда\w+"
    r"|рассказ\w+|жалу\w+|говор\w+|что|о|об|также|тоже"
    r"|у|я|мы|меня|нас|него|не[её]|них|его|е[её]|их|мой|мо[её]м|мое[йм]|наш\w*|дома|доме"
    r"|квартир\w+|тут|здесь|в|на|с|и|а|но|или|же|вот|просто|сид\w+|сижу|холодно"
    r"|свет[ауе]?|светом|электричеств\w+|электроснабжени\w+|вод[аыуе]|водой|водоснабжени\w+"
    r"|газ[ауе]?|газом|газоснабжени\w+|отоплени\w+|тепл[оау]|теплом|батаре\w+"
    r"|интернет\w*|связ[ьи]|связью|мобильн\w+|сотов\w+"
    r"|нет|нету|ни|без|не|отсутств\w+|работа\w+|пропал\w*|отключ\w+|да[юл]\w*"
    r"|грани|отказа|скоро|почти|совсем|вообще|полностью|никак\w*|нормальн\w+|постоянно"
    r"|уже|вс[её]|ещ[её]|снова|опять|пока|сейчас|сегодня|вчера|давно|до|сих|пор"
    r"|сутки|суток|день|дня|дней|неделю|недели|недель|месяц\w*|час\w*|ночь\w*|утра|вечера"
    r"|перв\w+|втор\w+|трет\w+|четв[её]рт\w+|пят\w+|шест\w+|седьм\w+|восьм\w+|девят\w+|десят\w+"
    r"|один|одн\w+|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять)",
    re.IGNORECASE,
)
_ABSENT_SERVICE = re.compile(
    r"\b(?:нет|нету|ни|без|отсутств\w+|не\s+работа\w+|не\s+да[юл]\w*|пропал\w*|отключ\w+"
    r"|на\s+грани)\b",
    re.IGNORECASE,
)
_SERVICE = re.compile(
    r"\b(?:свет[ауе]?|светом|электричеств\w+|электроснабжени\w+|вод[аыуе]|водой|водоснабжени\w+"
    r"|газ[ауе]?|газом|газоснабжени\w+|отоплени\w+|тепл[оау]|теплом|батаре\w+"
    r"|интернет\w*|связ[ьи]|связью)\b",
    re.IGNORECASE,
)
_LOCATOR_WORD = re.compile(
    r"\b(?:город\w*|район\w*|р-н\w*|улиц\w*|ул\.|переул\w*|проспект\w*|дом\s*№?\s*\d"
    r"|домах|домов|подъезд\w*|квартал\w*|массив\w*|микрорайон\w*|част\w+\s+сектор\w*|частник\w*"
    r"|девятиэтаж\w*|пятиэтаж\w*|многоэтаж\w*|сел[оае]\b|сёл\w*|посёл\w*|поселк\w*|центр\w*"
    r"|рын\w+|магазин\w*|школ\w*|больниц\w*|напротив|возле|около|рядом)\b",
    re.IGNORECASE,
)
# «на лиепайской», «по петровского»: a lowercase street named by its adjective or surname.
_LOWERCASE_STREET = re.compile(
    r"\b(?:на|по)\s+[а-яё]{4,}(?:ого|ской|цкой|вой|ная|ной|ева|ова|ина)\b", re.IGNORECASE
)
_LATIN_OR_ADDRESS_TOKEN = re.compile(r"\b(?:[A-Za-z]\w*|\d+[А-Яа-яЁё]\b|\d+-[а-яё]+)", re.UNICODE)
_SENTENCE_NAME_BEFORE_ABSENCE = re.compile(
    r"(?:^|[.!?]\s+|\n)(?P<name>[А-ЯЁ][а-яё]{3,})(?:\s+[а-яё]+){0,2}\s+(?:нет|без|ни)\b"
)
_MID_SENTENCE_NAME = re.compile(r"(?<=[а-яё,]\s)[А-ЯЁ][а-яё]{2,}")


def _is_status_only(text: str) -> bool:
    words = _WORD.findall(_DURATION.sub(" ", text))
    return bool(words) and all(_STATUS_WORD.fullmatch(word) for word in words)


def _names_a_place(text: str, resolver: Any) -> bool:
    if any(entity.kind in {"place", "area"} for entity in resolver.resolve(text).entities):
        return True
    if (
        _LOCATOR_WORD.search(text)
        or _LOWERCASE_STREET.search(text)
        or _LATIN_OR_ADDRESS_TOKEN.search(_DURATION.sub(" ", text))
        or _MID_SENTENCE_NAME.search(text)
    ):
        return True
    return any(
        not _STATUS_WORD.fullmatch(match["name"])
        for match in _SENTENCE_NAME_BEFORE_ABSENCE.finditer(text)
    )


def is_unlocated_outage_complaint(
    reported_facts: Sequence[str],
    source_texts: Sequence[str],
    *,
    resolver: Any,
) -> bool:
    """True when each fact only states a missing service and no text names a place."""
    return (
        bool(reported_facts)
        and all(
            _SERVICE.search(fact) and _ABSENT_SERVICE.search(fact) and _is_status_only(fact)
            for fact in reported_facts
        )
        and not any(_names_a_place(text, resolver) for text in (*reported_facts, *source_texts))
    )
