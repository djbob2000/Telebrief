"""Canonical service state normalization and derived operational observations."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from src.domain.event_payload import (
    EventPayload,
    EvidenceItemPayload,
    OperationalObservationPayload,
)
from src.domain.service_taxonomy import (
    SERVICE_FAMILY_STEMS,
    detect_service_families,
    matches_any_stem,
    semantic_tokens,
)

_ALLOWED_BASIS_BY_STATE: dict[str, frozenset[str]] = {
    "AVAILABLE": frozenset({"normal_operation"}),
    "UNAVAILABLE": frozenset({"direct_failure", "explicit_restriction"}),
    "DEGRADED": frozenset({"direct_failure", "degraded_access"}),
    "RESTRICTED": frozenset({"degraded_access", "explicit_restriction"}),
    "SCHEDULED": frozenset({"scheduled_change"}),
    "UNKNOWN": frozenset(
        {
            "normal_operation",
            "direct_failure",
            "degraded_access",
            "explicit_restriction",
            "scheduled_change",
        }
    ),
}

# Generic stems for private actor coping detector (language agnostic / RU + EN)
_PRIVATE_ACTOR_STEMS: frozenset[str] = frozenset(
    {
        "жител",
        "сосед",
        "горожан",
        "люд",
        "дом",
        "квартир",
        "resident",
        "neighbor",
        "household",
        "private",
        "people",
    }
)

_COPING_ACTION_STEMS: frozenset[str] = frozenset(
    {
        "скинул",
        "скидыва",
        "купил",
        "покупа",
        "запустил",
        "запуска",
        "включил",
        "включа",
        "заряжа",
        "зарядил",
        "запас",
        "запаса",
        "подключил",
        "подключа",
        "использ",
        "поставил",
        "ставят",
        "use",
        "using",
        "run",
        "running",
        "buy",
        "bought",
        "pool",
        "pooled",
        "charge",
        "charging",
        "stock",
        "stocking",
        "connect",
        "connected",
    }
)

_COPING_RESOURCE_STEMS: frozenset[str] = frozenset(
    {
        "генератор",
        "аккумулятор",
        "павербанк",
        "повербанк",
        "powerbank",
        "батаре",
        "скважин",
        "солнечн",
        "generator",
        "battery",
        "solar",
        "well",
    }
)

_SERVICE_OUTCOME_STEMS: frozenset[str] = frozenset(
    {
        "вода",
        "водн",
        "водоснабжен",
        "интернет",
        "связь",
        "провайдер",
        "банк",
        "банкомат",
        "транспорт",
        "автобус",
        "маршрутк",
        "лифт",
        "газ",
        "отоплен",
        "тепло",
        "почт",
        "доставк",
        "water",
        "internet",
        "connectivity",
        "telecom",
        "bank",
        "banking",
        "transport",
        "bus",
        "lift",
        "elevator",
        "gas",
        "heating",
        "delivery",
    }
)

_SERVICE_FAMILY_STEMS = SERVICE_FAMILY_STEMS
_semantic_tokens = semantic_tokens
_matches_any_stem = matches_any_stem
_detect_service_families = detect_service_families


def _is_high_confidence_private_coping(text: str) -> bool:
    tokens = _semantic_tokens(text)
    return (
        _matches_any_stem(tokens, _PRIVATE_ACTOR_STEMS)
        and _matches_any_stem(tokens, _COPING_ACTION_STEMS)
        and _matches_any_stem(tokens, _COPING_RESOURCE_STEMS)
        and not _matches_any_stem(tokens, _SERVICE_OUTCOME_STEMS)
    )


@dataclass(frozen=True)
class ServiceStateAudit:
    """Audit metadata from service-state evidence normalization."""

    accepted_count: int = 0
    rejected_count: int = 0
    rejected_evidence_indexes: tuple[int, ...] = ()
    rejection_reasons: tuple[str, ...] = ()


def normalize_service_state_evidence(
    payload: EventPayload,
) -> tuple[EventPayload, ServiceStateAudit]:
    """Validate and normalize service_state projections on EvidenceItemPayloads."""
    accepted = 0
    rejected_indexes: list[int] = []
    rejection_reasons: list[str] = []
    normalized_items: list[EvidenceItemPayload] = []

    for index, item in enumerate(payload.evidence_items):
        if item.service_state is None:
            normalized_items.append(item)
            continue

        # If non-service_access carries service_state, strip it
        if item.kind != "service_access" or item.publication_use != "PUBLISH":
            normalized_items.append(replace(item, service_state=None))
            rejected_indexes.append(index)
            rejection_reasons.append("non_publish_service_access_state")
            continue

        state = item.service_state

        # Check high-confidence private coping false-positive
        if _is_high_confidence_private_coping(item.text):
            normalized_items.append(
                replace(
                    item,
                    kind="community_report",
                    service_state=None,
                )
            )
            rejected_indexes.append(index)
            rejection_reasons.append("private_coping_demoted")
            continue

        # Check negative current state requires expected_now is True
        if (
            state.state in {"UNAVAILABLE", "DEGRADED", "RESTRICTED"}
            and state.expected_now is not True
        ):
            normalized_items.append(replace(item, service_state=None))
            rejected_indexes.append(index)
            rejection_reasons.append("negative_state_not_expected_now")
            continue

        # Check SCHEDULED requires effective_from
        if state.state == "SCHEDULED" and not state.effective_from:
            normalized_items.append(replace(item, service_state=None))
            rejected_indexes.append(index)
            rejection_reasons.append("scheduled_without_effective_from")
            continue

        # Check state/basis compatibility
        allowed_bases = _ALLOWED_BASIS_BY_STATE.get(state.state, frozenset())
        if state.basis not in allowed_bases:
            normalized_items.append(replace(item, service_state=None))
            rejected_indexes.append(index)
            rejection_reasons.append("state_basis_mismatch")
            continue

        # Check subject-family conflict
        subject_families = _detect_service_families(f"{state.subject_key} {state.subject_label}")
        evidence_families = _detect_service_families(item.text)
        if (
            subject_families
            and evidence_families
            and subject_families.isdisjoint(evidence_families)
        ):
            normalized_items.append(replace(item, service_state=None))
            rejected_indexes.append(index)
            rejection_reasons.append("subject_family_conflict")
            continue

        accepted += 1
        normalized_items.append(item)

    normalized_payload = replace(payload, evidence_items=tuple(normalized_items))
    audit = ServiceStateAudit(
        accepted_count=accepted,
        rejected_count=len(rejected_indexes),
        rejected_evidence_indexes=tuple(rejected_indexes),
        rejection_reasons=tuple(rejection_reasons),
    )
    return normalized_payload, audit


_PROFANITY_RE = re.compile(
    r"\b(?:г[іие]вн\w*|хер\w*|ху[йяе]\w*|пизд\w*|бл[яя]т\w*|еба\w*|ёба\w*|сук[аи]\w*|жоп\w*|дерьм\w*|нах[уе]\w*)\b",
    re.IGNORECASE,
)
_CHAT_CHATTER_RE = re.compile(
    r"\b(?:гуляти|читати\s+книжки|перечитати|не\s+очікувал\w*|займати\s+голову|спілкуватися)\b",
    re.IGNORECASE,
)


def normalize_berdyansk_toponyms(text: str) -> str:
    """Normalize well-known Berdyansk toponym and entity misattributions in Russian-language text."""
    if not text:
        return ""
    res = text

    # 1. Юпитер (местный интернет-провайдер, НЕ район и НЕ микрорайон города)
    def _sub_jupiter_in(m: re.Match[str]) -> str:
        is_cap = m.group(0)[0].isupper()
        return "У провайдера «Юпитер»" if is_cap else "у провайдера «Юпитер»"

    res = re.sub(
        r"\b[Вв]\s+(?:бердянском\s+)?районе\s+[«\"“]?Юпитер[»\"”]?",
        _sub_jupiter_in,
        res,
    )
    res = re.sub(
        r"\b[Вв]\s+[«\"“]?Юпитере[»\"”]?\s+пока\s+(?:ещ[её]\s+)?(?:его\s+)?(?:нет|отсутствует)\b",
        lambda m: (
            "У провайдера «Юпитер» пока отсутствует"
            if m.group(0)[0].isupper()
            else "у провайдера «Юпитер» пока отсутствует"
        ),
        res,
    )
    res = re.sub(
        r"\b(?:[Бб]ердянск(?:ий|ом)\s+)?район(?:е)?\s+[«\"“]?Юпитер[»\"”]?",
        lambda m: "Провайдер «Юпитер»" if m.group(0)[0].isupper() else "провайдер «Юпитер»",
        res,
    )
    res = re.sub(
        r"\b[«\"“]?Юпитер[»\"”]?\s+(?:район|микрорайон)\b",
        "провайдер «Юпитер»",
        res,
        flags=re.IGNORECASE,
    )

    # 2. Памятник Самолёт / Літак (пересечение ул. Орджоникидзе/Довганюка и Пролетарского/Восточного пр.)
    def _sub_litak(m: re.Match[str]) -> str:
        prep = m.group(1)
        is_cap = prep[0].isupper()
        return "У памятника Самолёту" if is_cap else "у памятника Самолёту"

    res = re.sub(
        r"\b(У|у|Возле|возле|Около|около|Біля|біля)\s+[«\"“]?[Лл][іiи]так[аa]?[»\"”]?",
        _sub_litak,
        res,
    )
    res = re.sub(
        r"\b(?:біля|возле|у)\s+пам[\'’]ятник(?:а)?\s+[«\"“]?[Лл][іiи]так[»\"”]?",
        "возле памятника Самолёту",
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(
        r"\bпам[\'’]ятник(?:а)?\s+[«\"“]?[Лл][іiи]так[»\"”]?",
        "памятника Самолёту",
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(
        r"\bпамятник(?:а)?\s+[«\"“]?[Лл][іiи]так[»\"”]?",
        "памятника Самолёту",
        res,
        flags=re.IGNORECASE,
    )

    # 3. Лихтари / ліхтарі -> фонари
    res = re.sub(
        r"\b[Лл]ихтар(?:и|ей|ям|ями|ях|я|ь|ем|ём)?\b",
        lambda m: (
            ("Фонар" if m.group(0)[0].isupper() else "фонар")
            + {
                "и": "и",
                "ей": "ей",
                "ям": "ям",
                "ями": "ями",
                "ях": "ях",
                "я": "я",
                "ь": "ь",
                "ем": "ём",
                "ём": "ём",
            }.get(m.group(0)[6:].lower(), "и")
        ),
        res,
    )
    res = re.sub(
        r"\b[Лл]іхтар(?:і|ів|ям|ями|ях|я|ь|ем|ем)?\b",
        lambda m: (
            ("Фонар" if m.group(0)[0].isupper() else "фонар")
            + {
                "і": "и",
                "ів": "ей",
                "ям": "ям",
                "ями": "ями",
                "ях": "ях",
                "я": "я",
                "ь": "ь",
                "ем": "ём",
            }.get(m.group(0)[6:].lower(), "и")
        ),
        res,
    )

    # 4. Осипенко (отдельное село, не посёлок и не микрорайон)
    res = re.sub(
        r"\bв\s+(?:микрорайоне|пос[её]лке)\s+Осипенко\b",
        "в селе Осипенко",
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(
        r"\b(?:микрорайон(?:е)?|пос[её]лок(?:е)?)\s+Осипенко\b",
        "село Осипенко",
        res,
        flags=re.IGNORECASE,
    )

    # 5. Гора (нагорная часть города, не микрорайон Гора)
    res = re.sub(
        r"\bв\s+микрорайоне\s+Гора\b",
        "в нагорной части города (на Горе)",
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(
        r"\bмикрорайон(?:е)?\s+Гора\b",
        "Нагорная часть (Гора)",
        res,
        flags=re.IGNORECASE,
    )

    # 6. Миранда (интернет-провайдер, не гора)
    res = re.sub(
        r"\bна\s+горе\s+Миранда\b",
        "на Горе у провайдера «Миранда»",
        res,
        flags=re.IGNORECASE,
    )
    res = re.sub(
        r"\bгора\s+Миранда\b",
        "провайдер «Миранда» на Горе",
        res,
        flags=re.IGNORECASE,
    )

    # 7. 50 лет СССР (улица 50 лет СССР / Нагорная)
    res = re.sub(
        r"\bв\s+районе\s+50[- ]?летия\b",
        "в районе улицы 50 лет СССР (Нагорной)",
        res,
        flags=re.IGNORECASE,
    )

    return res


def sanitize_operational_detail(text: str) -> str:
    """Strip question clauses, inquiries, profanities, and non-status tails from operational observation detail."""
    if not text:
        return ""
    cleaned = text.strip()
    had_end = cleaned.endswith((".", "!", "…", "?"))
    # 1. Remove parenthetical questions: (где вода?), (кто знает...?), (спрашивает...)
    cleaned = re.sub(
        r"\s*\([^)]*(\?|спрашива|интересу|уточня)[^)]*\)", "", cleaned, flags=re.IGNORECASE
    )
    # 2. Remove trailing question/inquiry clauses: " и спрашивает...", ", спрашивает..."
    cleaned = re.sub(
        r"(?:,\s*|\s+и\s+)(?:спрашива(?:ет|ют|ем|ется)?|интересу(?:ет|ют|ется|ются)?|уточня(?:ет|ют|ется)?).*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # 3. Split into sentences and drop sentences containing questions, question verbs, profanities, or chat chatter
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    kept_sentences: list[str] = []
    for s in sentences:
        s_clean = s.strip()
        if not s_clean:
            continue
        if "?" in s_clean:
            continue
        if _PROFANITY_RE.search(s_clean) or _CHAT_CHATTER_RE.search(s_clean):
            continue
        if re.search(
            r"\b(?:спрашива(?:ет|ют|ем|ется)?|интересу(?:ет|ют|ется|ются)?|уточня(?:ет|ют|ется)?)\b",
            s_clean,
            flags=re.IGNORECASE,
        ):
            continue
        kept_sentences.append(s_clean)
    result = " ".join(kept_sentences).strip()

    result = re.sub(r"[,;\s]+$", "", result)
    if result and had_end and not result.endswith((".", "!", "…")):
        result += "."
    # Check if what remains has actual factual substance (not just reporting boilerplate)
    norm = re.sub(r"[^\w\s]", "", result.casefold())
    boilerplate_words = {
        "житель",
        "жители",
        "жительница",
        "сообщает",
        "сообщают",
        "сообщил",
        "сообщили",
        "пишет",
        "пишут",
        "что",
        "по",
        "сообщениям",
    }
    words = [w for w in norm.split() if w not in boilerplate_words]
    if len(words) < 2:
        return ""
    # Normalize common geographic / entity confusions
    result = normalize_berdyansk_toponyms(result)
    return result


_RETAIL_COMMODITY_SALE_PATTERN = re.compile(
    r"\b(?:розлив|розничн\w* продаж\w*|продаж\w* питьев\w* вод\w*|\d+\s*(?:[₽р]|руб)/л(?:итр)?)\b",
    re.IGNORECASE,
)


def normalize_operational_location_and_entity(loc: str, entity: str = "") -> tuple[str, str]:
    """Normalize well-known Berdyansk colloquial anomalies in location/entity."""
    loc_clean = loc.strip() if loc else ""
    ent_clean = entity.strip() if entity else ""

    # 1. "гора Миранда" -> loc="Нагорная часть (Гора)", entity="Миранда"
    if re.search(r"\bгора\s+миранда\b|\bмиранда\s*\(гора\)", loc_clean, re.IGNORECASE):
        loc_clean = "Нагорная часть (Гора)"
        if not ent_clean:
            ent_clean = "Миранда"
    elif "миранда" in loc_clean.lower() and not ent_clean:
        ent_clean = "Миранда"
        loc_clean = re.sub(r"\bмиранда\b", "", loc_clean, flags=re.IGNORECASE).strip(" ,()")

    # 2. "микрорайон Гора" -> "Нагорная часть (Гора)"
    if re.search(r"\bмикрорайон\s+гора\b|\bмкр\.?\s*гора\b", loc_clean, re.IGNORECASE):
        loc_clean = re.sub(
            r"\b(?:микрорайон|мкр\.?)\s+гора\b",
            "Нагорная часть (Гора)",
            loc_clean,
            flags=re.IGNORECASE,
        )

    # 3. "район 50-летия" / "на 50-летие" / "50 лет" -> "ул. 50 лет СССР (Нагорная)"
    if re.search(
        r"\b(?:район\s+50[- ]?летия|на\s+50[- ]?лет(?:ие)?|50[- ]?летия)\b",
        loc_clean,
        re.IGNORECASE,
    ):
        loc_clean = re.sub(
            r"\b(?:район\s+50[- ]?летия|на\s+50[- ]?лет(?:ие)?|50[- ]?летия)\b",
            "ул. 50 лет СССР (Нагорная)",
            loc_clean,
            flags=re.IGNORECASE,
        )

    # 4. "микрорайон/посёлок Осипенко" -> "село Осипенко"
    if re.search(
        r"\b(?:микрорайон|мкр\.?|пос[её]лок|пос\.?)\s+осипенко\b", loc_clean, re.IGNORECASE
    ):
        loc_clean = re.sub(
            r"\b(?:микрорайон|мкр\.?|пос[её]лок|пос\.?)\s+осипенко\b",
            "село Осипенко",
            loc_clean,
            flags=re.IGNORECASE,
        )

    # 5. "Юпитер" / "район Юпитер" -> ent="Юпитер", loc_clean stripped of Юпитер
    if re.search(r"\b[«\"“]?юпитер[»\"”]?\b", loc_clean, re.IGNORECASE):
        if not ent_clean:
            ent_clean = "Юпитер"
        loc_clean = re.sub(
            r"\b(?:в\s+)?(?:район(?:е|а)?|микрорайон(?:е|а)?|мкр\.?)?\s*[«\"“]?юпитер[»\"”]?\s*(?:район(?:е|а)?|микрорайон(?:е|а)?|мкр\.?)?\b",
            "",
            loc_clean,
            flags=re.IGNORECASE,
        ).strip(" ,()")

    # 6. "Самолёт" / "Літак" (пересечение ул. Довганюка и Восточного пр. vs село Осипенко)
    if re.search(r"\b(?:[Лл][іiи]так(?:а)?|[Сс]амол[её]т(?:а)?)\b", loc_clean, re.IGNORECASE):
        if re.search(r"\bосипенко\b", loc_clean, re.IGNORECASE):
            loc_clean = "село Осипенко (памятник Самолёту)"
        else:
            loc_clean = "памятник Самолёту (пересечение ул. Довганюка и Восточного пр.)"

    return loc_clean, ent_clean


def derive_operational_observations(
    payload: EventPayload,
) -> tuple[OperationalObservationPayload, ...]:
    """Deterministically map validated service_state evidence to OperationalObservationPayloads."""
    observations: list[OperationalObservationPayload] = []
    for item in payload.evidence_items:
        state = item.service_state
        if item.kind != "service_access" or item.publication_use != "PUBLISH" or state is None:
            continue
        # Filter retail commodity sales (e.g. bottled water sales, 3 rub/liter)
        if _RETAIL_COMMODITY_SALE_PATTERN.search(
            state.subject_label
        ) or _RETAIL_COMMODITY_SALE_PATTERN.search(item.text):
            continue
        clean_detail = sanitize_operational_detail(item.text)
        if not clean_detail:
            continue
        clean_loc, clean_ent = normalize_operational_location_and_entity(
            state.location, state.entity
        )
        observations.append(
            OperationalObservationPayload(
                subject_key=state.subject_key,
                subject_label=state.subject_label,
                dimension=state.dimension,
                location=clean_loc,
                entity=clean_ent,
                state=state.state,
                detail=clean_detail,
                source_fragment_ids=item.source_fragment_ids,
                effective_from=state.effective_from,
                effective_until=state.effective_until,
            )
        )
    return tuple(observations)


def has_unstructured_publish_service_access(payload: EventPayload) -> bool:
    """Check if any PUBLISH service_access item lacks structured service_state."""
    return any(
        item.kind == "service_access"
        and item.publication_use == "PUBLISH"
        and item.service_state is None
        for item in payload.evidence_items
    )
