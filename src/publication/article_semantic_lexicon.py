from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9]+")

_CONCEPT_PREFIXES: dict[str, tuple[str, ...]] = {
    "uav": ("бпла", "беспилот", "безпілот", "дрон"),
    "internet": ("интернет", "інтернет", "wifi", "вайфай"),
    "report": ("сообщ", "повідом"),
    "apply_use": ("примен", "застос", "поступ", "надійш"),
    "power": ("свет", "электр", "електр"),
    "water": ("вод", "водопостач", "водоснабж"),
    "gas": ("газ", "газопостач", "газоснабж"),
    "heating": ("отоплен", "тепло", "опален"),
    "transport": ("транспорт", "автобус", "маршрутк"),
    "fuel": ("топлив", "бензин", "дизел", "палив"),
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


def _norm(token: str) -> str:
    return token.casefold().replace("ё", "е")


def _normalize_phrases(text: str) -> str:
    t = _norm(text)
    t = re.sub(r"\b(?:wi[ -]?fi|вай[ -]?фай)\b", " wifi ", t)
    return t


def canonicalize_semantic_token(token: str) -> str:
    norm = _norm(token)
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
