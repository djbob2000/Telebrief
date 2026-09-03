from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9]+")


def _norm(token: str) -> str:
    return (
        token.casefold()
        .replace("ё", "е")
        .replace("і", "и")
        .replace("ї", "и")
        .replace("є", "е")
        .replace("ґ", "г")
    )


_CONCEPT_PREFIXES: dict[str, tuple[str, ...]] = {
    "uav": tuple(_norm(p) for p in ("бпла", "беспилот", "безпілот", "дрон")),
    "internet": tuple(_norm(p) for p in ("интернет", "інтернет", "wifi", "вайфай")),
    "report": tuple(_norm(p) for p in ("сообщ", "повідом")),
    "apply_use": tuple(_norm(p) for p in ("примен", "застос", "поступ", "надійш")),
    "power": tuple(_norm(p) for p in ("свет", "электр", "електр", "струм", "блэкаут", "блекаут")),
    "water": tuple(_norm(p) for p in ("вод", "водопостач", "водоснабж", "подач")),
    "gas": tuple(_norm(p) for p in ("газопостач", "газоснабж", "газопровод", "горгаз")),
    "heating": tuple(_norm(p) for p in ("отоплен", "тепло", "опален")),
    "transport": tuple(_norm(p) for p in ("транспорт", "автобус", "маршрутк")),
    "fuel": tuple(_norm(p) for p in ("топлив", "бензин", "дизел", "палив")),
}

_CRITICAL_CONCEPTS = frozenset(
    {
        "concept:power",
        "concept:water",
        "concept:gas",
        "concept:heating",
        "concept:internet",
        "concept:transport",
        "concept:fuel",
        "concept:uav",
    }
)


def is_critical_semantic_concept(value: str) -> bool:
    return value in _CRITICAL_CONCEPTS


def _normalize_phrases(text: str) -> str:
    t = _norm(text)
    t = re.sub(r"\b(?:wi[ -]?fi|вай[ -]?фай)\b", " wifi ", t)
    return t


def canonicalize_semantic_token(token: str) -> str:
    norm = _norm(token)
    # Exclude false-positive water matches like "водитель" (driver) or "проводной" (wired)
    if norm.startswith("водител") or norm.startswith("водят") or norm.startswith("проводн"):
        return norm
    # Exclude false-positive gas matches like "выхлопные газы" (generator exhaust) or "газовая колонка"
    if norm == "газ" or norm.startswith("газов") or norm.startswith("газовик"):
        return norm
    for concept, prefixes in _CONCEPT_PREFIXES.items():
        if any(norm == prefix or norm.startswith(prefix) for prefix in prefixes):
            return f"concept:{concept}"
    return norm


def canonical_semantic_concepts(text: str) -> frozenset[str]:
    lowered = _normalize_phrases(text)
    concepts = {canonicalize_semantic_token(tok) for tok in _TOKEN_RE.findall(lowered)}
    if re.search(r"\b(?:понад|більше|более|свыше)\s+(?:ста|сто|сотн(?:ю|я|і)?)\b", lowered):
        concepts.add("quantity:>100")
    return frozenset(concepts)
