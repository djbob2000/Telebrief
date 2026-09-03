"""Concrete claim extraction, text normalization, and evidence support validation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from src.publication.article_semantic_lexicon import (
    canonical_semantic_concepts,
    canonicalize_semantic_token,
)

ClaimKind = Literal[
    "number",
    "date",
    "time",
    "money",
    "percent",
    "phone",
    "quoted_term",
    "direct_quote",
    "acronym",
    "causal_relation",
    "mechanism_relation",
]


_MONTHS_MAP = {
    "января": "01",
    "январь": "01",
    "январе": "01",
    "січня": "01",
    "січень": "01",
    "січні": "01",
    "февраля": "02",
    "февраль": "02",
    "феврале": "02",
    "лютого": "02",
    "лютий": "02",
    "лютому": "02",
    "марта": "03",
    "март": "03",
    "марте": "03",
    "березня": "03",
    "березень": "03",
    "апреля": "04",
    "апрель": "04",
    "апреле": "04",
    "квітня": "04",
    "квітень": "04",
    "мая": "05",
    "май": "05",
    "мае": "05",
    "травня": "05",
    "травень": "05",
    "июня": "06",
    "июнь": "06",
    "июне": "06",
    "червня": "06",
    "червень": "06",
    "июля": "07",
    "июль": "07",
    "июле": "07",
    "липня": "07",
    "липень": "07",
    "августа": "08",
    "август": "08",
    "августе": "08",
    "серпня": "08",
    "серпень": "08",
    "серпні": "08",
    "сентября": "09",
    "сентябрь": "09",
    "сентябре": "09",
    "вересня": "09",
    "вересень": "09",
    "октября": "10",
    "октябрь": "10",
    "октябре": "10",
    "жовтня": "10",
    "жовтень": "10",
    "ноября": "11",
    "ноябрь": "11",
    "ноябре": "11",
    "листопада": "11",
    "листопад": "11",
    "декабря": "12",
    "декабрь": "12",
    "декабре": "12",
    "грудня": "12",
    "грудень": "12",
}

_TIME_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "21:00": (
        "9 вечера",
        "9 вечер",
        "в 9 вечер",
        "до 9 вечер",
        "девяти вечера",
        "девять вечера",
        "21:00",
        "21.00",
        "21-00",
    ),
    "20:00": (
        "8 вечера",
        "8 вечер",
        "в 8 вечер",
        "до 8 вечер",
        "восьми вечера",
        "восемь вечера",
        "20:00",
        "20.00",
        "20-00",
    ),
    "22:00": (
        "10 вечера",
        "10 вечер",
        "в 10 вечер",
        "до 10 вечер",
        "десяти вечера",
        "десять вечера",
        "22:00",
        "22.00",
        "22-00",
    ),
    "23:00": ("11 вечера", "11 вечер", "в 11 вечер", "до 11 вечер", "23:00", "23.00", "23-00"),
    "19:00": (
        "7 вечера",
        "7 вечер",
        "в 7 вечер",
        "до 7 вечер",
        "семи вечера",
        "семь вечера",
        "19:00",
        "19.00",
        "19-00",
    ),
    "18:00": (
        "6 вечера",
        "6 вечер",
        "в 6 вечер",
        "до 6 вечер",
        "шести вечера",
        "шесть вечера",
        "18:00",
        "18.00",
        "18-00",
    ),
    "17:00": (
        "5 вечера",
        "5 вечер",
        "5 дня",
        "в 5 вечер",
        "до 5 вечер",
        "пяти вечера",
        "пять вечера",
        "17:00",
        "17.00",
        "17-00",
    ),
    "16:00": (
        "4 дня",
        "4 вечер",
        "в 4 дня",
        "до 4 дня",
        "четырех дня",
        "четыре дня",
        "16:00",
        "16.00",
        "16-00",
    ),
    "15:00": ("3 дня", "в 3 дня", "до 3 дня", "трех дня", "три дня", "15:00", "15.00", "15-00"),
    "14:00": ("2 дня", "в 2 дня", "до 2 дня", "двух дня", "два дня", "14:00", "14.00", "14-00"),
    "13:00": ("1 дня", "в 1 дня", "до 1 дня", "одного дня", "час дня", "13:00", "13.00", "13-00"),
    "12:00": ("12 дня", "в 12 дня", "до 12 дня", "полдень", "12:00", "12.00", "12-00"),
    "09:00": (
        "9 утра",
        "в 9 утр",
        "до 9 утр",
        "девяти утра",
        "девять утра",
        "09:00",
        "9:00",
        "9.00",
        "9-00",
    ),
    "08:00": (
        "8 утра",
        "в 8 утр",
        "до 8 утр",
        "восьми утра",
        "восемь утра",
        "08:00",
        "8:00",
        "8.00",
        "8-00",
    ),
    "07:00": (
        "7 утра",
        "в 7 утр",
        "до 7 утр",
        "семи утра",
        "семь утра",
        "07:00",
        "7:00",
        "7.00",
        "7-00",
    ),
    "06:00": (
        "6 утра",
        "в 6 утр",
        "до 6 утр",
        "шести утра",
        "шесть утра",
        "06:00",
        "6:00",
        "6.00",
        "6-00",
    ),
}


_DASH_RE = re.compile(r"[\u2010\u2012\u2013\u2014\u2212]")
_SPACES_RE = re.compile(r"\s+")
_QUOTES_RE = re.compile(r"[«»“”\"]")


def normalize_direct_quote(text: str) -> str:
    t = _DASH_RE.sub("-", text)
    t = _SPACES_RE.sub(" ", t).strip()
    return t


def _classify_quoted_span(inner: str) -> ClaimKind:
    tokens = re.findall(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9]+", inner)
    return "direct_quote" if len(tokens) >= 2 else "quoted_term"


def _stem(word: str) -> str:
    """Conservative Russian/Ukrainian suffix trimmer for entity/relation token matching."""
    w = (
        word.lower()
        .replace("ё", "е")
        .replace("і", "и")
        .replace("ї", "и")
        .replace("є", "е")
        .replace("ґ", "г")
    )
    if len(w) > 5 and (w.endswith("ся") or w.endswith("сь")):
        w = w[:-2]

    # Pass 1: Inflectional endings (case, number, gender, verbal endings)
    for suffix in (
        "ами",
        "ями",
        "иями",
        "ого",
        "его",
        "ому",
        "ему",
        "ыми",
        "ими",
        "ском",
        "ской",
        "скую",
        "ские",
        "ских",
        "ются",
        "ется",
        "атся",
        "ятся",
        "аются",
        "яются",
        "ивать",
        "ывать",
        "ов",
        "ев",
        "ей",
        "ом",
        "ем",
        "ой",
        "ей",
        "ах",
        "ях",
        "иях",
        "ым",
        "им",
        "иям",
        "ую",
        "юю",
        "ое",
        "ее",
        "ые",
        "ие",
        "ая",
        "яя",
        "ый",
        "ий",
        "ых",
        "их",
        "ение",
        "ения",
        "ению",
        "ением",
        "ении",
        "ений",
        "ание",
        "ания",
        "анию",
        "анием",
        "ании",
        "аний",
        "ией",
        "ием",
        "ию",
        "ия",
        "ии",
        "ать",
        "ять",
        "ить",
        "еть",
        "ует",
        "уют",
        "ает",
        "ают",
        "яет",
        "яют",
        "ал",
        "ял",
        "ил",
        "ел",
        "ли",
        "ла",
        "ло",
        "ут",
        "ют",
        "ат",
        "ят",
        "а",
        "я",
        "о",
        "е",
        "ы",
        "и",
        "у",
        "ю",
        "ь",
    ):
        if len(w) >= len(suffix) + 3 and w.endswith(suffix):
            w = w[: -len(suffix)]
            break

    # Pass 2: Common derivational adjective/noun suffixes
    for suffix in (
        "ическ",
        "тельн",
        "ованн",
        "альн",
        "ийн",
        "ск",
        "ов",
        "ев",
        "н",
    ):
        if len(w) >= len(suffix) + 3 and w.endswith(suffix):
            w = w[: -len(suffix)]
            break

    return w


stem_word = _stem


def normalize_support_text(text: str) -> str:
    """Normalize text for evidence-boundary claim matching."""
    if not text:
        return ""
    t = text.replace("ё", "е").replace("Ё", "е")
    t = _DASH_RE.sub("-", t)
    t = _SPACES_RE.sub(" ", t).strip().lower()

    # Normalize number words
    t = re.sub(r"\b(?:полутора|полтора|полторы)\b", "1.5", t)

    # Normalize ranges like "10 - 12" -> "10-12"
    t = re.sub(r"(\d+)\s*-\s*(\d+)", r"\1-\2", t)

    # Expand standard date representations into text for dual matching
    # E.g. "30.08" -> "30.08 (30 августа)" or "30 августа" -> "30 августа (30.08)"
    def _repl_date_word(m: re.Match[str]) -> str:
        day = m.group(1).zfill(2)
        month_word = m.group(2)
        month_num = _MONTHS_MAP.get(month_word, "")
        if month_num:
            return f"{m.group(0)} {day}.{month_num}"
        return m.group(0)

    t = re.sub(
        r"\b(\d{1,2})\s+(" + "|".join(_MONTHS_MAP.keys()) + r")\b",
        _repl_date_word,
        t,
    )

    def _repl_date_num(m: re.Match[str]) -> str:
        day = m.group(1).zfill(2)
        month_num = m.group(2)
        # Find matching month word
        for w, num in _MONTHS_MAP.items():
            if num == month_num and w.endswith("а"):
                return f"{m.group(0)} {int(day)} {w}"
        return m.group(0)

    t = re.sub(r"\b(\d{1,2})\.(\d{2})\b", _repl_date_num, t)

    return t


@dataclass(frozen=True)
class ConcreteClaim:
    """A concrete factual statement extracted from article text."""

    kind: ClaimKind
    raw: str
    normalized: str
    excerpt: str


# Regexes for claim extraction
_PHONE_RE = re.compile(r"(?:\+7|8)[\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
_MONEY_RE = re.compile(
    r"\b\d+(?:[\s.,]\d+)*\s*(?:руб(?:л[яей]|ль|\.)?|₽|usd|\$|eur|€|грн|грив(?:ен|на|ны|\.))\b",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"\b\d+(?:[\.,]\d+)?\s*(?:%|процент(?:а|ов|ами|ах)?)\b", re.IGNORECASE)
_QUOTED_NAME_RE = re.compile(r"[«\"“]([^»\"”]{2,80})[»\"”]")
_ACRONYM_RE = re.compile(r"\b[A-ZА-Я]{2,}\b")
_DATE_RE = re.compile(
    r"\b(?:\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)|\d{1,2}\.\d{2}(?:\.\d{2,4})?|(?:до\s+)?конца\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)|в\s+(?:январе|феврале|марте|апреле|мае|июне|июле|августе|сентябре|октябре|ноябре|декабре))\b",
    re.IGNORECASE,
)
_TIME_INTERVAL_RE = re.compile(
    r"\b(?:\d+(?:[.,]\d+)?(?:\s*[\-\–\—]\s*\d+(?:[.,]\d+)?)?\s*(?:минут[а-я]*|мин|часов|часа|час|ч)|с?\s*\d{1,2}:\d{2}(?:\s*до\s*\d{1,2}:\d{2})?|(?:в течение\s+)?полутора\s+(?:минут[а-я]*|часов|часа|суток))\b",
    re.IGNORECASE,
)
_NUMBER_RANGE_RE = re.compile(
    r"\b(?:\d+(?:[\-\–\—]\d+)+|\d+(?:[\.,]\d+)?|\bполутора\b|\bполтора\b|\bполторы\b)\b",
    re.IGNORECASE,
)
_CAUSAL_RE = re.compile(
    r"(?:\b(?:из-за|вследствие|по причине|в результате|в связи с|вызвано|вызвана|вызван|вызваны)\s+)([^,.;\n]+)",
    re.IGNORECASE,
)
_MECHANISM_RE = re.compile(
    r"(?:\b(?:по\s+(?:резервной|временной|обходной|постоянной)\s+схеме|в\s+(?:ручном|автоматическом|аварийном)\s+режиме)\b)",
    re.IGNORECASE,
)
_SPEECH_PRONOUNS_RE = re.compile(
    r"\b(?:я|мы|вы|ты|нам|нас|мне|меня|вам|вас|мой|моя|моё|наш|наша|наше|наши)\b",
    re.IGNORECASE,
)
_SPEECH_VERBS_RE = re.compile(
    r"\b(?:сказал\w*|заявил\w*|отметил\w*|подчеркнул\w*|пояснил\w*|сообщил\w*|уточнил\w*|будем|можем|хотим|ждем|просим|надеемся|сделаем)\b",
    re.IGNORECASE,
)


_COMMON_ACRONYM_EXCLUSIONS = frozenset(
    {"ИЛИ", "ДЛЯ", "ПРИ", "БЕЗ", "ПОД", "НАД", "ИЗ", "ОТ", "ДО", "ПО", "СО"}
)


def extract_concrete_claims(text: str) -> tuple[ConcreteClaim, ...]:
    """Extract concrete, high-risk factual claims from article text."""
    if not text:
        return ()

    claims: list[ConcreteClaim] = []
    phone_spans: list[tuple[int, int]] = []

    # 1. Phone
    for m in _PHONE_RE.finditer(text):
        raw = m.group(0)
        phone_spans.append((m.start(), m.end()))
        norm = re.sub(r"[\s\-\(\)\+]", "", raw)
        claims.append(
            ConcreteClaim(
                kind="phone",
                raw=raw,
                normalized=norm,
                excerpt=text[max(0, m.start() - 10) : min(len(text), m.end() + 10)],
            )
        )

    # 2. Money
    for m in _MONEY_RE.finditer(text):
        raw = m.group(0)
        norm = normalize_support_text(raw)
        claims.append(ConcreteClaim(kind="money", raw=raw, normalized=norm, excerpt=raw))

    # 3. Percent
    for m in _PERCENT_RE.finditer(text):
        raw = m.group(0)
        norm = normalize_support_text(raw)
        claims.append(ConcreteClaim(kind="percent", raw=raw, normalized=norm, excerpt=raw))

    # 4. Quoted terms and direct quotes
    for m in _QUOTED_NAME_RE.finditer(text):
        raw = m.group(0)
        inner = m.group(1).strip()
        if inner:
            kind = _classify_quoted_span(inner)
            norm = (
                normalize_direct_quote(inner)
                if kind == "direct_quote"
                else normalize_support_text(inner)
            )
            claims.append(
                ConcreteClaim(
                    kind=kind,
                    raw=raw,
                    normalized=norm,
                    excerpt=raw,
                )
            )

    # 5. Acronyms (e.g. ONET, РЭС, РТС, АКЗ)
    for m in _ACRONYM_RE.finditer(text):
        raw = m.group(0)
        if raw not in _COMMON_ACRONYM_EXCLUSIONS and len(raw) >= 2:
            claims.append(
                ConcreteClaim(kind="acronym", raw=raw, normalized=raw.lower(), excerpt=raw)
            )

    # 6. Date
    for m in _DATE_RE.finditer(text):
        raw = m.group(0)
        norm = normalize_support_text(raw)
        claims.append(ConcreteClaim(kind="date", raw=raw, normalized=norm, excerpt=raw))

    # 7. Time & intervals
    for m in _TIME_INTERVAL_RE.finditer(text):
        raw = m.group(0)
        norm = normalize_support_text(raw)
        claims.append(ConcreteClaim(kind="time", raw=raw, normalized=norm, excerpt=raw))

    # 8. Numbers & ranges (exclude numbers that are inside phone numbers)
    for m in _NUMBER_RANGE_RE.finditer(text):
        start, end = m.start(), m.end()
        if any(p_start <= start and end <= p_end for p_start, p_end in phone_spans):
            continue
        raw = m.group(0)
        norm = normalize_support_text(raw)
        claims.append(ConcreteClaim(kind="number", raw=raw, normalized=norm, excerpt=raw))

    # 9. Causal relations
    for m in _CAUSAL_RE.finditer(text):
        raw = m.group(0)
        cause_obj = m.group(1).strip()
        if cause_obj:
            norm = normalize_support_text(cause_obj)
            claims.append(
                ConcreteClaim(kind="causal_relation", raw=raw, normalized=norm, excerpt=raw)
            )

    # 10. Mechanism relations
    for m in _MECHANISM_RE.finditer(text):
        raw = m.group(0)
        norm = normalize_support_text(raw)
        claims.append(
            ConcreteClaim(kind="mechanism_relation", raw=raw, normalized=norm, excerpt=raw)
        )

    return tuple(claims)


def _stemmed_text(text: str) -> str:
    words = re.findall(r"[a-zа-яёієїґ0-9]+", text.lower())
    return " ".join(_stem(w) for w in words)


_UA_RU_STEM_EQUIVALENTS: dict[str, set[str]] = {
    "электроснабж": {
        "електропостачан",
        "електропостач",
        "электроснабж",
        "струм",
        "электричеств",
        "свет",
        "світл",
    },
    "электричеств": {
        "електропостачан",
        "електропостач",
        "электроснабж",
        "струм",
        "электричеств",
        "свет",
        "світл",
        "електр",
        "электр",
    },
    "электр": {"електропостачан", "електр", "электр", "струм", "свет", "світл"},
    "электроэнерг": {"електроенерг", "электроэнерг", "струм", "электричеств"},
    "отсутств": {"відсутн", "відсутніст", "відсутність", "отсутств", "немає", "нет"},
    "стабиль": {"стабіль", "стабиль"},
    "водоснабж": {"водопостачан", "водопостач", "водоснабж", "вода", "води"},
    "газоснабж": {"газопостачан", "газопостач", "газоснабж", "газ", "газу"},
    "отоплен": {"опален", "отоплен", "тепло"},
    "интернет": {"інтернет", "интернет", "провайдер"},
    "связ": {"зв'яз", "звяз", "связ", "интернет", "інтернет"},
    "поврежден": {"пошкоджен", "поврежден", "порыв", "порив"},
    "авари": {"аварі", "авари"},
    "ремонт": {"ремонт", "відновлен", "восстановлен"},
    "обстрел": {"обстріл", "обстрел", "прилет", "приліт"},
    "осложн": {"ускладн", "пробл", "трудн", "осложн"},
    "пробл": {"пробл", "ускладн", "трудн"},
    "выезд": {"виїзд", "выезд"},
    "семь": {"родин", "сім", "сем", "родина", "семей"},
    "семе": {"родин", "сім", "сем", "родина", "семей"},
    # ЖКХ (рус.) ↔ ЖКГ / ЖКП (укр.)
    "жкх": {"жкх", "жкг", "жкп", "жилищно", "коммунальн"},
    "жкг": {"жкх", "жкг", "жкп", "жилищно", "коммунальн"},
}


def _token_matches_any_support(tok: str, norm_supports: Sequence[str]) -> bool:
    equivs = set(_UA_RU_STEM_EQUIVALENTS.get(tok, {tok}))
    equivs.add(tok.replace("и", "і").replace("е", "є"))

    for sup in norm_supports:
        if any(eq in sup for eq in equivs):
            return True
        sup_stemmed = _stemmed_text(sup)
        if any(eq in sup_stemmed for eq in equivs):
            return True
    return False


def find_unsupported_claims(
    text: str,
    support_texts: Sequence[str],
    *,
    all_known_draft_supports: Sequence[str] = (),
    allowed_context_terms: Sequence[str] = (),
    direct_quote_source_texts: Sequence[str] | None = None,
    direct_quote_allowlist: Sequence[str] | None = None,
) -> tuple[ConcreteClaim, ...]:
    """Identify concrete claims in text that are not substantiated by cited support texts."""
    if not text:
        return ()

    claims = extract_concrete_claims(text)
    if not claims:
        return ()

    norm_supports = [normalize_support_text(st) for st in support_texts if st]
    combined_support_norm = " ".join(norm_supports)

    unsupported: list[ConcreteClaim] = []

    for claim in claims:
        norm = claim.normalized

        if claim.kind == "phone":
            phone_digits = claim.normalized
            if not any(
                phone_digits in re.sub(r"[\s\-\(\)\+]", "", st) for st in support_texts if st
            ):
                unsupported.append(claim)

        elif claim.kind in ("money", "percent"):
            # Direct normalized substring search
            if not any(norm in sup for sup in norm_supports):
                unsupported.append(claim)

        elif claim.kind == "acronym":
            if not any(norm in sup for sup in norm_supports):
                from src.publication.article_semantic_support import _EDITORIAL_GLUE

                if (
                    any(norm in normalize_support_text(st) for st in all_known_draft_supports)
                    or any(norm == normalize_support_text(t) for t in allowed_context_terms)
                    or any(norm in normalize_support_text(t) for t in allowed_context_terms)
                    or norm in _EDITORIAL_GLUE
                ):
                    continue
                acronym_concept = canonicalize_semantic_token(norm)
                if not (
                    acronym_concept.startswith("concept:")
                    and any(
                        acronym_concept in canonical_semantic_concepts(st)
                        for st in support_texts
                        if st
                    )
                ):
                    unsupported.append(claim)

        elif claim.kind == "direct_quote":
            tokens = re.findall(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9]+", claim.raw)
            is_speech = (
                len(tokens) >= 6
                or bool(_SPEECH_PRONOUNS_RE.search(claim.raw))
                or bool(_SPEECH_VERBS_RE.search(claim.raw))
            )

            if direct_quote_allowlist is not None:
                normalized_allowlist = [
                    normalize_direct_quote(st) for st in direct_quote_allowlist if st
                ]
                if not any(
                    claim.normalized == source or claim.normalized in source
                    for source in normalized_allowlist
                ):
                    # Check if quoted text is a short named entity/title substantiated by support
                    if is_speech or not (
                        claim.normalized.lower() in combined_support_norm
                        or _stemmed_text(claim.normalized) in _stemmed_text(combined_support_norm)
                    ):
                        unsupported.append(claim)
            else:
                quote_sources = (
                    direct_quote_source_texts
                    if direct_quote_source_texts is not None
                    else support_texts
                )
                normalized_sources = [normalize_direct_quote(st) for st in quote_sources if st]
                if not any(claim.normalized in source for source in normalized_sources):
                    if is_speech or not (
                        claim.normalized.lower() in combined_support_norm
                        or _stemmed_text(claim.normalized) in _stemmed_text(combined_support_norm)
                    ):
                        unsupported.append(claim)

        elif claim.kind == "quoted_term":
            term_stemmed = _stemmed_text(claim.normalized)
            term_stem = _stem(claim.normalized)
            if not any(
                norm in sup
                or (term_stem and term_stem in sup)
                or (term_stemmed and term_stemmed in _stemmed_text(sup))
                for sup in norm_supports
            ):
                unsupported.append(claim)

        elif claim.kind == "number":
            # Check if numeric literal or range is in support
            # E.g. "10-12" or "1.5" or "500"
            if norm not in combined_support_norm:
                unsupported.append(claim)

        elif claim.kind in ("date", "time"):
            norm_clean = re.sub(r"\b0(\d:\d{2})\b", r"\1", norm)
            comb_sup_clean = re.sub(r"\b0(\d:\d{2})\b", r"\1", combined_support_norm)
            if claim.kind == "time":
                time_equivs = _TIME_EQUIVALENTS.get(claim.raw.strip(), ())
                if any(
                    eq in comb_sup_clean
                    or any(eq in normalize_support_text(st) for st in norm_supports)
                    or any(eq in normalize_support_text(st) for st in all_known_draft_supports)
                    for eq in time_equivs
                ):
                    continue
            # Check if normalized date/time tokens are supported
            # E.g. "10-12 минут" -> check "10-12" and "минут" or "10-12"
            date_num_match = re.search(r"\b\d{1,2}\.\d{2}\b", norm_clean)
            if date_num_match and date_num_match.group(0) in comb_sup_clean:
                continue
            tokens = [
                t
                for t in norm_clean.split()
                if len(t) > 1
                and not t.startswith("(")
                and t not in ("с", "до", "по", "от", "в", "на")
            ]
            if not tokens:
                continue
            # For date/time, numeric parts and key month words must appear
            from src.publication.article_semantic_support import _EDITORIAL_GLUE

            all_tokens_found = all(
                tok in comb_sup_clean
                or _stem(tok) in _stemmed_text(comb_sup_clean)
                or _token_matches_any_support(_stem(tok), norm_supports)
                or any(tok in normalize_support_text(st) for st in all_known_draft_supports)
                or any(tok in normalize_support_text(t) for t in allowed_context_terms)
                or any(tok == t.lower() for t in allowed_context_terms)
                or tok in _EDITORIAL_GLUE
                for tok in tokens
            )
            if not all_tokens_found:
                unsupported.append(claim)

        elif claim.kind == "causal_relation":
            # Check that content tokens of the causal object exist in support (including UA/RU equivalents)
            cause_tokens = [
                _stem(w)
                for w in re.findall(r"[a-zа-яієїґ0-9]+", norm)
                if len(w) > 2
                and w
                not in (
                    "для",
                    "при",
                    "без",
                    "под",
                    "над",
                    "из",
                    "за",
                    "на",
                    "по",
                    "со",
                    "это",
                    "как",
                    "так",
                    "что",
                    "чем",
                )
            ]
            if not cause_tokens:
                continue
            matching = [
                tok for tok in cause_tokens if _token_matches_any_support(tok, norm_supports)
            ]
            min_required = 1 if len(cause_tokens) <= 2 else max(1, len(cause_tokens) // 2)
            if len(matching) < min_required:
                unsupported.append(claim)

        elif claim.kind == "mechanism_relation":
            # Mechanism phrase tokens (e.g. 'резервн', 'схем') must appear in support
            mech_tokens = [
                _stem(w)
                for w in re.findall(r"[a-zа-я0-9]+", norm)
                if len(w) > 2 and w not in ("по", "в", "на", "из", "за", "со")
            ]
            if not mech_tokens:
                continue
            found = all(any(tok in sup for sup in norm_supports) for tok in mech_tokens)
            if not found:
                unsupported.append(claim)

    return tuple(unsupported)
