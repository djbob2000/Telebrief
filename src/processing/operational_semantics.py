"""Canonical service state normalization and derived operational observations."""

from __future__ import annotations

import re
from collections.abc import Mapping
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


_SCHEDULE_INTENT_STEMS: frozenset[str] = frozenset(
    {
        "график",
        "планов",
        "расписан",
        "предупрежд",
        "уведомлен",
        "анонс",
        "профилактик",
        "планиру",
        "schedule",
        "scheduled",
        "planned",
    }
)

_SPECULATION_RUMOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:говорят|слышал[аи]?|по\s+слухам|вроде|как\s+бы|обещают|увидите|вангую|вырубят\s+всё|отрубят\s+всё|заберут|закроют\s+всё|после\s+\d+[-—]?(?:го|е)?\s+(?:выруб|отруб|отключ))\b",
        re.IGNORECASE,
    ),
)

_RU_MONTH_STEMS: dict[int, str] = {
    1: "январ",
    2: "феврал",
    3: "март",
    4: "апрел",
    5: "ма",
    6: "июн",
    7: "июл",
    8: "август",
    9: "сентябр",
    10: "октябр",
    11: "ноябр",
    12: "декабр",
}


def _has_valid_schedule_intent(text: str) -> bool:
    """Check if evidence contains explicit schedule intent stems and not pure speculation/rumor."""
    tokens = _semantic_tokens(text)
    has_intent = _matches_any_stem(tokens, _SCHEDULE_INTENT_STEMS)
    if not has_intent:
        return False
    for pat in _SPECULATION_RUMOR_PATTERNS:
        if pat.search(text):
            return False
    return True


def _has_grounded_time_value(text: str, effective_from: str) -> bool:
    """Check if the HH:MM time in effective_from is grounded in raw text."""
    m_time = re.search(r"[T\s](\d{1,2}):(\d{2})", effective_from)
    if not m_time:
        return True
    hour, minute = int(m_time.group(1)), int(m_time.group(2))
    text_lower = text.lower()
    time_patterns = [
        rf"\b0?{hour}[:.-]{minute:02d}\b",
        rf"\b0?{hour}\s*(?:часов|час|ч\b|утр|вечер|дня|ноч)",
    ]
    if minute == 0:
        time_patterns.append(rf"(?:в|с|до|к)\s+0?{hour}\b")
        time_patterns.append(rf"\b0?{hour}:00\b")
        time_patterns.append(rf"\b0?{hour}-00\b")
        time_patterns.append(rf"\b0?{hour}\.00\b")
    return any(re.search(p, text_lower) for p in time_patterns)


def _has_grounded_temporal_value(text: str, effective_from: str | None) -> bool:
    """Verify that the projected date/time value is grounded in raw evidence."""
    if not effective_from:
        return False
    text_lower = text.lower()
    date_part = effective_from.split("T")[0].split(" ")[0]
    m_date = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date_part)
    if m_date:
        _, month_num, day_num = int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3))
        day_str = str(day_num)
        day_zero_str = f"{day_num:02d}"
        day_pattern = re.compile(
            rf"(?:^|\D){day_num}(?:-?(?:го|е|й|м|х|я)?\b|[.\/]\d{{1,2}}|\b)",
            re.IGNORECASE,
        )
        if not (
            day_pattern.search(text_lower)
            or f" {day_str} " in f" {text_lower} "
            or f" {day_zero_str} " in f" {text_lower} "
        ):
            return False

        month_stem = _RU_MONTH_STEMS.get(month_num)
        has_any_month = any(
            m_stem in text_lower for m_stem in _RU_MONTH_STEMS.values() if len(m_stem) > 2
        )
        if has_any_month and month_stem and month_stem not in text_lower:
            return False
    else:
        eff_tokens = [t for t in re.split(r"[^\w\d]+", date_part.lower()) if len(t) >= 2]
        if not any(tok in text_lower for tok in eff_tokens):
            return False

    return _has_grounded_time_value(text, effective_from)


def _has_valid_schedule_grounding(text: str, effective_from: str | None = None) -> bool:
    """Check schedule intent and projected temporal grounding."""
    if not _has_valid_schedule_intent(text):
        return False
    if effective_from is not None and not _has_grounded_temporal_value(text, effective_from):
        return False
    return True


def normalize_service_state_evidence(
    payload: EventPayload,
    fragment_texts: Mapping[int, str] | None = None,
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

        # Determine raw grounding text:
        # When fragment_texts is provided, missing fragment IDs fail-closed.
        # Legacy fallback to item.text is only permitted when fragment_texts is None.
        if fragment_texts is not None:
            raw_texts: list[str] = []
            if item.source_fragment_ids:
                for fid in item.source_fragment_ids:
                    if fid in fragment_texts:
                        raw_texts.append(fragment_texts[fid])
            if not raw_texts:
                normalized_items.append(
                    replace(
                        item,
                        kind="community_report",
                        publication_use="CONTEXT",
                        service_state=None,
                    )
                )
                rejected_indexes.append(index)
                rejection_reasons.append("missing_raw_grounding")
                continue
            grounding_text = " ".join(raw_texts)
        else:
            grounding_text = item.text

        state = item.service_state

        # Check high-confidence private coping false-positive
        if _is_high_confidence_private_coping(grounding_text):
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

        # Check subject-family grounding: must have at least one family marker in raw evidence
        subject_families = _detect_service_families(f"{state.subject_key} {state.subject_label}")
        evidence_families = _detect_service_families(grounding_text)
        if subject_families:
            if not evidence_families:
                normalized_items.append(
                    replace(
                        item,
                        kind="community_report",
                        publication_use="CONTEXT",
                        service_state=None,
                    )
                )
                rejected_indexes.append(index)
                rejection_reasons.append("ungrounded_service_family")
                continue
            if subject_families.isdisjoint(evidence_families):
                normalized_items.append(
                    replace(
                        item,
                        kind="community_report",
                        publication_use="CONTEXT",
                        service_state=None,
                    )
                )
                rejected_indexes.append(index)
                rejection_reasons.append("subject_family_conflict")
                continue

        # Check 3-part proof for SCHEDULED:
        # 1. Service family grounded (checked above)
        # 2. Schedule intent grounded (requires explicit schedule terms, no rumors)
        # 3. Projected temporal value grounded (date/time in raw evidence)
        if state.state == "SCHEDULED" or state.basis == "scheduled_change":
            if state.effective_from and re.search(r"[T\s]\d{1,2}:\d{2}", state.effective_from):
                if not _has_grounded_time_value(grounding_text, state.effective_from):
                    date_part = state.effective_from.split("T")[0].split(" ")[0]
                    state = replace(state, effective_from=date_part)
                    item = replace(item, service_state=state)

            if not _has_valid_schedule_grounding(grounding_text, state.effective_from):
                normalized_items.append(
                    replace(
                        item,
                        kind="community_report",
                        publication_use="CONTEXT",
                        service_state=None,
                    )
                )
                rejected_indexes.append(index)
                rejection_reasons.append("unsupported_scheduled_change")
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
    """Normalize well-known Berdyansk toponym and entity misattributions in Russian-language text (legacy alias)."""
    from src.domain.edition_geography import normalize_edition_toponyms

    return normalize_edition_toponyms(text, edition_slug="berdyansk")


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


def normalize_operational_location_and_entity(
    loc: str, entity: str = "", edition_slug: str = "berdyansk"
) -> tuple[str, str]:
    """Normalize colloquial anomalies in location/entity for an edition."""
    from src.domain.edition_geography import normalize_edition_operational_location_and_entity

    return normalize_edition_operational_location_and_entity(loc, entity, edition_slug=edition_slug)


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
        from src.publication.story_quality import is_generic_service_entity

        if is_generic_service_entity(clean_ent, state.subject_label, state.subject_key):
            continue

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
