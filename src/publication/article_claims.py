"""Concrete claim extraction, text normalization, and evidence support validation."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

ClaimKind = Literal[
    "number",
    "date",
    "time",
    "money",
    "percent",
    "phone",
    "quoted_name",
    "acronym",
    "causal_relation",
    "mechanism_relation",
]

_MONTHS_MAP = {
    "января": "01",
    "январь": "01",
    "январе": "01",
    "февраля": "02",
    "февраль": "02",
    "феврале": "02",
    "марта": "03",
    "март": "03",
    "марте": "03",
    "апреля": "04",
    "апрель": "04",
    "апреле": "04",
    "мая": "05",
    "май": "05",
    "мае": "05",
    "июня": "06",
    "июнь": "06",
    "июне": "06",
    "июля": "07",
    "июль": "07",
    "июле": "07",
    "августа": "08",
    "август": "08",
    "августе": "08",
    "сентября": "09",
    "сентябрь": "09",
    "сентябре": "09",
    "октября": "10",
    "октябрь": "10",
    "октябре": "10",
    "ноября": "11",
    "ноябрь": "11",
    "ноябре": "11",
    "декабря": "12",
    "декабрь": "12",
    "декабре": "12",
}

_DASH_RE = re.compile(r"[\u2010\u2012\u2013\u2014\u2212]")
_SPACES_RE = re.compile(r"\s+")
_QUOTES_RE = re.compile(r"[«»“”\"]")


def _stem(word: str) -> str:
    """Conservative Russian suffix trimmer for entity/relation token matching."""
    w = word.lower()
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
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
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
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
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
    r"\b(?:\d+(?:[\s\-\–\—]\d+)?\s*(?:минут[а-я]*|мин|часов|часа|час|ч)|с?\s*\d{1,2}:\d{2}(?:\s*до\s*\d{1,2}:\d{2})?|(?:в течение\s+)?полутора\s+(?:минут[а-я]*|часов|часа|суток))\b",
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

    # 4. Quoted names
    for m in _QUOTED_NAME_RE.finditer(text):
        raw = m.group(0)
        inner = m.group(1).strip()
        if inner:
            claims.append(
                ConcreteClaim(
                    kind="quoted_name",
                    raw=raw,
                    normalized=normalize_support_text(inner),
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


def find_unsupported_claims(
    text: str,
    support_texts: Sequence[str],
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

        if claim.kind in ("phone", "money", "percent", "quoted_name", "acronym"):
            # Direct normalized substring search
            if not any(norm in sup for sup in norm_supports):
                unsupported.append(claim)

        elif claim.kind == "number":
            # Check if numeric literal or range is in support
            # E.g. "10-12" or "1.5" or "500"
            if norm not in combined_support_norm:
                unsupported.append(claim)

        elif claim.kind in ("date", "time"):
            # Check if normalized date/time tokens are supported
            # E.g. "10-12 минут" -> check "10-12" and "минут" or "10-12"
            tokens = [t for t in norm.split() if len(t) > 1 and not t.startswith("(")]
            if not tokens:
                continue
            # For date/time, numeric parts and key month words must appear
            all_tokens_found = all(tok in combined_support_norm for tok in tokens)
            if not all_tokens_found:
                unsupported.append(claim)

        elif claim.kind == "causal_relation":
            # Check that content tokens of the causal object exist in support
            cause_tokens = [
                _stem(w)
                for w in re.findall(r"[a-zа-я0-9]+", norm)
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
                )
            ]
            if not cause_tokens:
                continue
            # Support must contain the cause tokens (e.g. 'авар', 'подстанци' or 'порыв')
            # If draft introduces new cause like 'гидроудар', 'гидроудар' won't be in support
            found = all(any(tok in sup for sup in norm_supports) for tok in cause_tokens)
            if not found:
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
