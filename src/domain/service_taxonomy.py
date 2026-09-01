"""Domain definitions for service families and rubric mapping taxonomy.

Single public taxonomy for service detection across processing and publication.
"""

from __future__ import annotations

import re

SERVICE_FAMILY_STEMS: dict[str, frozenset[str]] = {
    "power": frozenset(
        {
            "свет",
            "электр",
            "энерг",
            "лэп",
            "питани",
            "обесточ",
            "подстанци",
            "трансформатор",
            "power",
            "electric",
            "electricity",
            "blackout",
            "power_supply",
        }
    ),
    "water": frozenset(
        {
            "вода",
            "воды",
            "воде",
            "воду",
            "водой",
            "водопровод",
            "водоснабж",
            "водоканал",
            "водовод",
            "водозабор",
            "обезвож",
            "скважин",
            "water",
            "aqueduct",
            "water_supply",
        }
    ),
    "gas": frozenset(
        {
            "газ",
            "газа",
            "газу",
            "газом",
            "газов",
            "газоснабж",
            "газопровод",
            "gas",
            "gas_supply",
        }
    ),
    "heating": frozenset({"отоплен", "тепл", "котельн", "теплоснабж", "heat", "heating"}),
    "lift": frozenset({"лифт", "подъемник", "lift", "elevator"}),
    "telecom": frozenset(
        {
            "интернет",
            "связ",
            "провайдер",
            "сеть",
            "wi-fi",
            "wifi",
            "telecom",
            "internet",
            "cellular",
            "mobile",
            "internet_connectivity",
        }
    ),
    "banking": frozenset(
        {"банк", "банкомат", "терминал", "платеж", "bank", "atm", "banking", "payment"}
    ),
    "transport": frozenset(
        {
            "транспорт",
            "автобус",
            "маршрут",
            "поезд",
            "трамвай",
            "троллейбус",
            "рейс",
            "проезд",
            "transport",
            "bus",
            "train",
            "public_transport",
        }
    ),
    "urban_transport": frozenset(
        {
            "городской_транспорт",
            "городской_автобус",
            "маршрутка",
            "трамвай",
            "троллейбус",
        }
    ),
    "logistics": frozenset({"доставк", "почт", "курьер", "parcel", "postal", "delivery"}),
    "municipal": frozenset({"жэк", "мусор", "вывоз", "коммунал", "municipal", "waste", "garbage"}),
}


def semantic_tokens(text: str) -> list[str]:
    """Extract normalized alphanumeric tokens from text."""
    clean = re.sub(r"[^\w\s-]", " ", text.casefold().replace("ё", "е"))
    return [token for token in clean.split() if token]


def matches_any_stem(tokens: list[str], stems: frozenset[str]) -> bool:
    """Return True if any token matches any stem (exact or prefix, avoiding substring false positives)."""
    for token in tokens:
        for stem in stems:
            if token == stem:
                return True
            if len(stem) >= 4 and token.startswith(stem):
                return True
    return False


def detect_service_families(text: str) -> frozenset[str]:
    """Detect core service families mentioned in text."""
    tokens = semantic_tokens(text)
    detected: set[str] = set()
    for family, stems in SERVICE_FAMILY_STEMS.items():
        if matches_any_stem(tokens, stems):
            detected.add(family)
    return frozenset(detected)


def map_family_to_rubric(family: str) -> str | None:
    """Map a detected service family to its canonical digest rubric ID."""
    if family == "telecom":
        return "communications"
    if family in ("transport", "urban_transport"):
        return "mobility"
    if family in ("power", "water", "gas", "heating", "lift", "municipal"):
        return "infrastructure"
    return None
