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
    """Audit metadata from service-claim and service-state normalization."""

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

_MONTH_PATTERNS: dict[int, re.Pattern[str]] = {
    1: re.compile(r"\b(?:январ[яеьюи]?|січ(?:ень|ня|ні|нем)?)\b", re.IGNORECASE),
    2: re.compile(r"\b(?:феврал[яеьюи]?|лют(?:ий|ого|ому|им)?)\b", re.IGNORECASE),
    3: re.compile(r"\b(?:март[аеуом]?|берез(?:ень|ня|ні|нем)?)\b", re.IGNORECASE),
    4: re.compile(r"\b(?:апрел[яеьюи]?|квіт(?:ень|ня|ні|нем)?)\b", re.IGNORECASE),
    5: re.compile(r"\b(?:ма[яйе]|трав(?:ень|ня|ні|нем)?)\b", re.IGNORECASE),
    6: re.compile(r"\b(?:июн[яеьюи]?|черв(?:ень|ня|ні|нем)?)\b", re.IGNORECASE),
    7: re.compile(r"\b(?:июл[яеьюи]?|лип(?:ень|ня|ні|нем)?)\b", re.IGNORECASE),
    8: re.compile(r"\b(?:август[аеуом]?|серп(?:ень|ня|ні|нем)?)\b", re.IGNORECASE),
    9: re.compile(r"\b(?:сентябр[яеьюи]?|верес(?:ень|ня|ні|нем)?)\b", re.IGNORECASE),
    10: re.compile(r"\b(?:октябр[яеьюи]?|жовт(?:ень|ня|ні|нем)?)\b", re.IGNORECASE),
    11: re.compile(r"\b(?:ноябр[яеьюи]?|листопад(?:а|і|ом)?)\b", re.IGNORECASE),
    12: re.compile(r"\b(?:декабр[яеьюи]?|груд(?:ень|ня|ні|нем)?)\b", re.IGNORECASE),
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
    if minute != 0:
        # Require explicit minute-bearing expression
        time_patterns = [
            rf"\b0?{hour}[:.-]{minute:02d}\b",
            rf"\b0?{hour}\s+{minute:02d}\b",
        ]
    else:
        # For minute == 0, require clock-time context:
        # e.g., '09:00', '9.00', 'в 9', 'с 9', 'до 9', 'к 9', '9 утра', '9 вечера'
        # NOT bare duration '9 часов' or bare counter
        time_patterns = [
            rf"\b0?{hour}[:.-]00\b",
            rf"(?:в|с|до|к|после|около)\s+0?{hour}(?:\s*(?:часов|час|ч\b|утр[ае]?|вечер[ае]?|дня|ноч[ие]?))?\b",
            rf"\b0?{hour}\s*(?:утр[ае]?|вечер[ае]?|дня|ноч[ие]?)\b",
        ]
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

        detected_months = {m for m, pat in _MONTH_PATTERNS.items() if pat.search(text_lower)}
        if detected_months and month_num not in detected_months:
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
    *,
    reply_parent_context_by_fragment_id: Mapping[int, str] | None = None,
    edition_name: str | None = None,
) -> tuple[EventPayload, ServiceStateAudit]:
    """Validate service states against primary text while keeping reply context separate."""
    accepted = 0
    rejected_indexes: list[int] = []
    rejection_reasons: list[str] = []
    normalized_items: list[EvidenceItemPayload] = []

    for index, item in enumerate(payload.evidence_items):
        raw_texts: list[str] = []
        if fragment_texts is not None:
            raw_texts = [
                fragment_texts[fid] for fid in item.source_fragment_ids if fid in fragment_texts
            ]
            item, ungrounded_reason = _normalize_unstructured_service_claim(
                item,
                fragment_texts=fragment_texts,
                reply_parent_context_by_fragment_id=reply_parent_context_by_fragment_id,
                edition_name=edition_name,
            )
            if ungrounded_reason is not None:
                normalized_items.append(item)
                rejected_indexes.append(index)
                rejection_reasons.append(ungrounded_reason)
                continue

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
            family_mismatch_reason = None
            if not evidence_families:
                family_mismatch_reason = "ungrounded_service_family"
            elif subject_families.isdisjoint(evidence_families):
                family_mismatch_reason = "subject_family_conflict"

            if family_mismatch_reason is not None:
                # A dependent answer/report may inherit the service referent
                # from its parent. Retain only the child's exact words; never
                # carry the parent's place, status, or duration into PUBLISH.
                direct_reply_text = _direct_service_reply_text(
                    item=item,
                    raw_texts=raw_texts,
                    subject_families=subject_families,
                    reply_parent_context_by_fragment_id=reply_parent_context_by_fragment_id,
                    edition_name=edition_name,
                )
                if direct_reply_text is not None:
                    normalized_items.append(
                        replace(
                            item,
                            text=direct_reply_text,
                            kind="community_report",
                            publication_use="PUBLISH",
                            service_state=None,
                        )
                    )
                    rejected_indexes.append(index)
                    rejection_reasons.append("reply_context_only_subject")
                    continue
                normalized_items.append(
                    replace(
                        item,
                        kind="community_report",
                        publication_use="CONTEXT",
                        service_state=None,
                    )
                )
                rejected_indexes.append(index)
                rejection_reasons.append(family_mismatch_reason)
                continue

        # Check 3-part proof for SCHEDULED:
        # 1. Service family grounded (checked above across grounding_text)
        # 2. Schedule intent grounded (requires explicit schedule terms, no rumors)
        # 3. Projected temporal value grounded (date/time in raw evidence)
        # ADVERSARIAL HARDENING: All 3 parts (service family + schedule intent + date/time)
        # must co-occur inside a SINGLE grounding unit (single fragment or item.text),
        # preventing cross-fragment Frankenstein proofs where date comes from an unrelated event.
        if state.state == "SCHEDULED" or state.basis == "scheduled_change":
            candidate_units = raw_texts if fragment_texts is not None else [grounding_text]
            has_single_unit_proof = False
            for unit_txt in candidate_units:
                u_fam = _detect_service_families(unit_txt)
                if subject_families and (not u_fam or subject_families.isdisjoint(u_fam)):
                    continue
                if not _has_valid_schedule_grounding(unit_txt, state.effective_from):
                    continue
                has_single_unit_proof = True
                break

            if state.effective_from and re.search(r"[T\s]\d{1,2}:\d{2}", state.effective_from):
                if not _has_grounded_time_value(grounding_text, state.effective_from):
                    date_part = state.effective_from.split("T")[0].split(" ")[0]
                    state = replace(state, effective_from=date_part)
                    item = replace(item, service_state=state)
                    # Re-check single unit proof with updated effective_from without time
                    has_single_unit_proof = False
                    for unit_txt in candidate_units:
                        u_fam = _detect_service_families(unit_txt)
                        if subject_families and (not u_fam or subject_families.isdisjoint(u_fam)):
                            continue
                        if not _has_valid_schedule_grounding(unit_txt, state.effective_from):
                            continue
                        has_single_unit_proof = True
                        break

            if not has_single_unit_proof:
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


def _normalize_unstructured_service_claim(
    item: EvidenceItemPayload,
    *,
    fragment_texts: Mapping[int, str],
    reply_parent_context_by_fragment_id: Mapping[int, str] | None,
    edition_name: str | None,
) -> tuple[EvidenceItemPayload, str | None]:
    """Require each cited support to name its claimed service or answer its parent."""
    if item.publication_use != "PUBLISH" or item.kind not in {"community_report", "service_access"}:
        return item, None

    claimed_families = _detect_service_families(item.text)
    if not claimed_families:
        return item, None
    if not item.source_fragment_ids:
        return replace(item, publication_use="CONTEXT", service_state=None), "missing_raw_grounding"

    for fragment_id in item.source_fragment_ids:
        source_text = fragment_texts.get(fragment_id)
        if source_text is None:
            return replace(
                item, publication_use="CONTEXT", service_state=None
            ), "missing_raw_grounding"

        relation_conflict = _source_claim_relation_conflict(
            source_text,
            item.text,
            parent_text=(reply_parent_context_by_fragment_id or {}).get(fragment_id, ""),
        )
        if relation_conflict is not None:
            return replace(item, publication_use="CONTEXT", service_state=None), relation_conflict

        unsupported_families = claimed_families - _detect_service_families(source_text)
        if not unsupported_families:
            continue

        parent_text = (reply_parent_context_by_fragment_id or {}).get(fragment_id, "")
        parent_families = _detect_service_families(parent_text)
        if not unsupported_families.issubset(parent_families):
            return (
                replace(item, publication_use="CONTEXT", service_state=None),
                "ungrounded_service_family",
            )

        fragment_item = replace(item, source_fragment_ids=(fragment_id,))
        direct_reply = _direct_service_reply_text(
            item=fragment_item,
            raw_texts=[source_text],
            subject_families=unsupported_families,
            reply_parent_context_by_fragment_id=reply_parent_context_by_fragment_id,
            edition_name=edition_name,
        )
        if direct_reply is None:
            return (
                replace(item, publication_use="CONTEXT", service_state=None),
                "ungrounded_service_family",
            )

    return item, None


_POSITIVE_CONTINUITY_RE = re.compile(
    r"\bкак\s+был(?:а|о|и)?\s*[,—–-]?\s*так\s+и\s+остал(?:ся|ась|ось|ись)\b",
    re.IGNORECASE,
)
_EXPLICIT_ABSENCE_CLAIM_RE = re.compile(
    r"\b(?:отключение|отсутствие)\s+(?:света|воды|электричества|интернета)\s+"
    r"(?:сохраня\w*|продолжа\w*)\b|"
    r"\b(?:света|воды|электричества|интернета)\s+нет(?:у)?\b|"
    r"\bнет(?:у)?\s+(?:света|воды|электричества|интернета)\b",
    re.IGNORECASE,
)
_STAY_DURATION_RE = re.compile(
    r"\b(?:побы|пробы)(?:л|ла|ло|ли)\s+"
    r"(?:\d+|три|трое|два|двое|четыре|пять|шесть|семь)\s+"
    r"(?:дня|дней|суток|часа|часов|минут)\b",
    re.IGNORECASE,
)
_ONCE_WITHIN_PERIOD_RE = re.compile(r"\bодин\s+раз\s+в\s+течение\b", re.IGNORECASE)


def _source_claim_relation_conflict(source: str, claim: str, *, parent_text: str) -> str | None:
    """Detect two explicit relation inversions, not general semantic equivalence.

    Absence of these forms proves nothing. Restrict the check to one service
    and a short primary assertion; the parent supplies only its referent.
    Rejected extraction stays recoverable in Gate, never a noise verdict.
    """
    source_families = _detect_service_families(source)
    resolved_families = source_families or _detect_service_families(parent_text)
    if (
        len(resolved_families) != 1
        or resolved_families != _detect_service_families(claim)
        or len(source.split()) > 24
        or "?" in source
    ):
        return None
    if (
        _POSITIVE_CONTINUITY_RE.search(source)
        and not re.search(r"\b(?:не|нет|нету|без)\b", source, re.IGNORECASE)
        and _EXPLICIT_ABSENCE_CLAIM_RE.search(claim)
    ):
        return "source_availability_conflict"
    if (
        source_families
        and _STAY_DURATION_RE.search(source)
        and _ONCE_WITHIN_PERIOD_RE.search(claim)
        and not re.search(r"\b(?:в\s+течение|за)\b", source, re.IGNORECASE)
    ):
        return "source_duration_relation_conflict"
    return None


_SHORT_DIRECT_REPLY_RE = re.compile(
    r"^\s*(?:да|ага|угу|нет|неа)\b.{0,120}$", re.IGNORECASE | re.DOTALL
)
_DIRECT_SERVICE_REPORT_RE = re.compile(
    r"\b(?:нет(?:у)?|был(?:а|о|и)?|офф(?:лайн)?|не\s+(?:было|будет|работа[лт](?:а|о|и)?|подают|включают)|"
    r"перестал[аио]сь?|появил[аои]сь?|включил[аи]?|отключил[аи]?|"
    r"дают|дали|есть|работает)\b",
    re.IGNORECASE,
)
_ELLIPTICAL_SUPPLY_INTERVAL_RE = re.compile(
    r"\b(?:один|\d+)\s+раз(?:а|ов)?\s+в\s+(?:неделю|день|сутки|месяц)\s+"
    r"на\s+(?:сутки|(?:\d+|один|два|три)\s+час(?:а|ов)?)\b",
    re.IGNORECASE,
)
_DIRECT_DURATION_NUMBER_RE = (
    r"(?:\d{1,2}(?:[-‐‑‒–—]?(?:го|й|е|ий|ая|ое|ого|ому))?|"
    r"один\w*|одна|одно|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|"
    r"перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*|шест\w*|седьм\w*|"
    r"восьм\w*|девят\w*|десят\w*)"
)
_DIRECT_DURATION_UNIT_RE = r"(?:минут\w*|час\w*|дн\w*|день|дня|дней|сутк\w*|недел\w*|месяц\w*)"
_DIRECT_REPLY_DURATION_RE = re.compile(
    rf"\b(?P<number>{_DIRECT_DURATION_NUMBER_RE})\s*(?P<unit>{_DIRECT_DURATION_UNIT_RE})\b",
    re.IGNORECASE,
)
_DIRECT_REPLY_DETAIL_RE = re.compile(
    r"\b(?:сегодня|вчера|недавно|раньше|утром|дн[её]м|вечером|ночью|"
    r"с\s+(?:понедельника|вторника|среды|четверга|пятницы|субботы|воскресенья)|"
    rf"{_DIRECT_REPLY_DURATION_RE.pattern})\b",
    re.IGNORECASE,
)
_RUSSIAN_WEEKDAY_RE = re.compile(
    r"\b(?:понедельник\w*|вторник\w*|сред\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)\b",
    re.IGNORECASE,
)
_LOCAL_PLACE_AFTER_PREPOSITION_RE = re.compile(
    r"\b(?:[Нн]а|[Вв]|[Уу]|[Пп]о)\s+"
    r"(?!(?:сообщению|словам|данным|информации|версии|источникам)\b)"
    r"(?P<place>(?:(?:улиц[аеу]|ул\.?)\s+)?"
    r"(?:\d+\s+[а-яё]+|[а-яё-]+(?:\s+[а-яё-]+){0,2}))\b",
    re.IGNORECASE,
)
_CITYWIDE_SCOPE_RE = re.compile(
    r"\b(?:весь\s+город|во\s+вс[её]м\s+городе|по\s+всему\s+городу|"
    r"на\s+весь\s+город|везде)\b",
    re.IGNORECASE,
)
_CITYWIDE_NAMED_PLACE_RE = re.compile(
    r"\b(?:во\s+вс[её]м|по\s+всему|на\s+всей)\s+"
    r"(?P<place>[а-яё][а-яё-]+(?:\s+[а-яё][а-яё-]+){0,2})\b",
    re.IGNORECASE,
)


def _direct_service_reply_text(
    *,
    item: EvidenceItemPayload,
    raw_texts: list[str],
    subject_families: set[str] | frozenset[str],
    reply_parent_context_by_fragment_id: Mapping[int, str] | None,
    edition_name: str | None,
) -> str | None:
    """Preserve a direct dependent reply while refusing to borrow parent claims."""
    if len(item.source_fragment_ids) != 1 or len(raw_texts) != 1:
        return None
    fragment_id = item.source_fragment_ids[0]
    parent_text = (reply_parent_context_by_fragment_id or {}).get(fragment_id, "")
    reply_text = raw_texts[0].strip()
    if not parent_text.strip():
        return None
    parent_families = _detect_service_families(parent_text)
    if not subject_families.intersection(parent_families):
        return None

    if "?" in parent_text and len(reply_text.split()) <= 12:
        if _SHORT_DIRECT_REPLY_RE.fullmatch(reply_text):
            return reply_text

    elliptical_interval = (
        len(parent_families) == 1
        and _LOCAL_PLACE_AFTER_PREPOSITION_RE.search(reply_text)
        and _ELLIPTICAL_SUPPLY_INTERVAL_RE.search(reply_text)
        and _elliptical_interval_action_matches(parent_text, item.text)
    )
    if len(reply_text.split()) > 24 or not (
        _DIRECT_SERVICE_REPORT_RE.search(reply_text) or elliptical_interval
    ):
        return None
    has_own_detail = (
        _DIRECT_REPLY_DETAIL_RE.search(reply_text)
        or (_LOCAL_PLACE_AFTER_PREPOSITION_RE.search(reply_text))
        or (_CITYWIDE_SCOPE_RE.search(reply_text))
    )
    if not has_own_detail or not _reply_detail_preserved(
        reply_text, item.text, edition_name=edition_name
    ):
        return None

    return reply_text


def _elliptical_interval_action_matches(parent: str, claim: str) -> bool:
    """An interval answer inherits only a uniquely asked action, not a status.

    A parent comparing a house with power and a house without power does not
    say whether 'once a week for a day' describes supply or an interruption.
    """
    if "?" not in parent:
        return False
    positive = re.compile(
        r"\b(?:дают|дали|давал\w*|включа\w*|включил\w*|подают|подавал\w*)\b", re.IGNORECASE
    )
    negative = re.compile(r"\b(?:отключ\w*|выключ\w*|нет(?:у)?|без|не)\b", re.IGNORECASE)
    parent_positive = bool(positive.search(parent))
    parent_negative = bool(negative.search(parent))
    if parent_positive == parent_negative:
        return False
    return not (negative.search(claim) if parent_positive else positive.search(claim))


def _reply_detail_preserved(
    source_text: str, claim_text: str, *, edition_name: str | None = None
) -> bool:
    """Require the generated claim to retain a concrete detail from the reply."""
    claim_folded = claim_text.casefold().replace("ё", "е")
    source_weekdays = {
        match.group(0).casefold().replace("ё", "е")
        for match in _RUSSIAN_WEEKDAY_RE.finditer(source_text)
    }
    claim_weekdays = {
        match.group(0).casefold().replace("ё", "е")
        for match in _RUSSIAN_WEEKDAY_RE.finditer(claim_text)
    }
    if claim_weekdays and not claim_weekdays.issubset(source_weekdays):
        return False

    for match in _DIRECT_REPLY_DETAIL_RE.finditer(source_text):
        detail = match.group(0).casefold().replace("ё", "е")
        if detail in claim_folded:
            return True

    source_durations = {
        _normalized_duration(match.group("number"), match.group("unit"))
        for match in _DIRECT_REPLY_DURATION_RE.finditer(source_text)
    }
    claim_durations = {
        _normalized_duration(match.group("number"), match.group("unit"))
        for match in _DIRECT_REPLY_DURATION_RE.finditer(claim_text)
    }
    if source_durations & claim_durations:
        return True

    # A citywide reply such as "Весь город офф" may be expanded to the
    # configured edition name, but never to another city or a smaller area.
    if _CITYWIDE_SCOPE_RE.search(source_text):
        if _CITYWIDE_SCOPE_RE.search(claim_text):
            return True
        named_place = _CITYWIDE_NAMED_PLACE_RE.search(claim_text)
        if named_place is None or not edition_name:
            return False
        return _place_matches_edition(named_place.group("place"), edition_name)

    source_place = _LOCAL_PLACE_AFTER_PREPOSITION_RE.search(source_text)
    claim_place = _LOCAL_PLACE_AFTER_PREPOSITION_RE.search(claim_text)
    if source_place is None or claim_place is None:
        return False

    ignored = {
        "на",
        "в",
        "у",
        "по",
        "улица",
        "улице",
        "ул",
        "3",
        "третий",
        "третьем",
        "нет",
        "нету",
        "свет",
        "света",
        "светом",
        "электричество",
        "электричества",
        "вода",
        "воды",
        "интернет",
        "связь",
        "связи",
        "отключение",
        "отключили",
        "работает",
        "работал",
        "работала",
        "работают",
        "день",
        "дней",
        "неделя",
        "неделю",
        "сутки",
        "часов",
        "сегодня",
        "вчера",
        "раньше",
        "тоже",
        "уже",
        "первый",
    }
    source_tokens = set(_semantic_tokens(source_place.group("place"))) - ignored
    claim_tokens = set(_semantic_tokens(claim_place.group("place"))) - ignored
    return bool(source_tokens & claim_tokens)


def _place_matches_edition(place: str, edition_name: str) -> bool:
    """Match a named city in a grammatical case to the configured edition."""

    def canonicalize(token: str) -> str:
        folded = token.casefold().replace("ё", "е")
        if len(folded) > 4 and folded[-2:] in {"ом", "ой"}:
            folded = folded[:-2]
        elif len(folded) > 4 and folded[-1:] in {"е", "а", "у", "ы", "и"}:
            folded = folded[:-1]
        return folded.removesuffix("ь")

    place_tokens = [canonicalize(token) for token in _semantic_tokens(place)]
    edition_tokens = [canonicalize(token) for token in _semantic_tokens(edition_name)]
    if not place_tokens or not edition_tokens or len(place_tokens) != len(edition_tokens):
        return False
    return all(
        place_token == edition_token
        or (len(place_token) >= 5 and place_token.startswith(edition_token))
        or (len(edition_token) >= 5 and edition_token.startswith(place_token))
        for place_token, edition_token in zip(place_tokens, edition_tokens, strict=True)
    )


def _normalized_duration(number: str, unit: str) -> tuple[int | str, str]:
    """Normalize simple Russian digit/word durations for reply-detail matching."""
    folded_number = number.casefold().replace("ё", "е")
    numeric_match = re.search(r"\d+", folded_number)
    if numeric_match:
        quantity: int | str = int(numeric_match.group(0))
    else:
        quantity = folded_number
        if not re.search(r"(?:надцат|дцать)", folded_number):
            for stem, value in (
                ("один", 1),
                ("одна", 1),
                ("одно", 1),
                ("перв", 1),
                ("два", 2),
                ("две", 2),
                ("втор", 2),
                ("три", 3),
                ("трет", 3),
                ("четыр", 4),
                ("четверт", 4),
                ("пять", 5),
                ("пят", 5),
                ("шесть", 6),
                ("шест", 6),
                ("семь", 7),
                ("седьм", 7),
                ("восемь", 8),
                ("восьм", 8),
                ("девять", 9),
                ("девят", 9),
                ("десять", 10),
                ("десят", 10),
            ):
                if folded_number.startswith(stem):
                    quantity = value
                    break

    folded_unit = unit.casefold().replace("ё", "е")
    if folded_unit.startswith(("минут",)):
        unit_key = "minute"
    elif folded_unit.startswith("час"):
        unit_key = "hour"
    elif folded_unit.startswith(("д", "сутк")):
        unit_key = "day"
    elif folded_unit.startswith("недел"):
        unit_key = "week"
    else:
        unit_key = "month"
    return quantity, unit_key


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
    # 0. Strip in_reply_to annotations
    cleaned = re.sub(r"\s*\(in_reply_to:[^)]*\)", "", cleaned, flags=re.IGNORECASE).strip()
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
