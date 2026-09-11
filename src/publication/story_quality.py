"""Deterministic quality, predicate, and entity grounding guards for publication stories."""

from __future__ import annotations

import re
from typing import Any

_GENERIC_ENTITY_WORDS = frozenset(
    {
        "сервис",
        "проблемный сервис",
        "услуга",
        "приложение",
        "мобильное приложение",
        "компания",
        "организация",
        "провайдер",
        "служба",
        "некий сервис",
        "сторонний сервис",
        "городской сервис",
    }
)

_GENERIC_ENTITY_PATTERN = re.compile(
    r"^(?:проблемн\w*|сторонн\w*|неки\w*|данн\w*|городск\w*)?\s*(?:сервис|услуг\w*|приложен\w*|провайдер\w*|служб\w*)$",
    re.IGNORECASE,
)

_ATTRIBUTION_PREFIX_RE = re.compile(
    r"^(?:по\s+(?:сообщениям|словам|информации|данным)\s+(?:жителей|горожан|очевидцев)|"
    r"(?:жители|горожане|очевидцы)\s+(?:сообщают|пишут|отмечают)|"
    r"в\s+(?:местных\s+чатах|соцсетях|сетях|пабликах)(?:\s+(?:жители|горожане))?(?:\s+(?:подтверждают|сообщают|пишут))?|"
    r"несколько\s+горожан\s+сообщают)[,:\s]+",
    re.IGNORECASE,
)

_CHATTER_META_RE = re.compile(
    r"\b(?:(?:жители|горожане|в\s+соцсетях|в\s+чатах)\s+)?(?:обсуждают(?:\s+текущую)?\s+ситуацию|"
    r"подробности\s+уточняются|информация\s+уточняется|ситуация\s+уточняется|"
    r"выясняют\s+обстоятельства|жители\s+интересуются)\b",
    re.IGNORECASE,
)

_CIVIC_EVENT_TOKENS_RE = re.compile(
    r"\b(?:"
    # Verbs / participles of action, state, change
    r"отключил\w*|пропал\w*|вернул\w*|восстановил\w*|заработал\w*|прорвал\w*|теч[её]т|"
    r"капает|перекрыл\w*|затопил\w*|горит|потушил\w*|сбил\w*|упал\w*|взорвал\w*|повредил\w*|"
    r"ремонтиру\w*|починил\w*|чин\w*|провод\w*|открыл\w*|закрыл\w*|запустил\w*|пода[юе]\w*|"
    r"ход[яи]т|списал\w*|подорожал\w*|подешевел\w*|зафиксирован\w*|замечен\w*|наблюда\w*|"
    r"снизил\w*|повысил\w*|упал\w*|вырос\w*|выплат\w*|начисл\w*|получа\w*|направля\w*|"
    # Event / state nouns
    r"авари[яи]|прорыв\w*|ремонт\w*|отключени[ея]|перебо[яев]|восстановлени[ея]|взрыв\w*|"
    r"обстрел\w*|сирен\w*|пожар\w*|дым\w*|дтп|напряжени[ея]|скачк\w*|график\w*|подвоз\w*|"
    r"задержк\w*|отмен\w*|выплат\w*|пособи[ея]|запрет\w*|штраф\w*|при[её]м\w*|проверк\w*|"
    # Predicates / states
    r"нет|нету|есть|доступен|доступна|доступно|доступны|недоступен|недоступна|недоступно|недоступны|"
    r"отсутству\w*|восстановлен\w*|отключен\w*|перекрыт\w*|открыт\w*|закрыт\w*|"
    r"\d+\s*(?:в|вольт|квт|руб|рублей|р\.|грн|мбит|%|процент\w*)"
    r")\b",
    re.IGNORECASE,
)

_PURE_GEOGRAPHIC_FRAGMENT_RE = re.compile(
    r"^(?:(?:в|на|возле|около|у|вблизи|по|со\s+стороны|в\s+районе)\s+[\w\s«»\"'\.\-]+)+$",
    re.IGNORECASE,
)


def is_generic_service_entity(entity: str, subject_label: str = "", subject_key: str = "") -> bool:
    """Check if an entity represents a generic placeholder without concrete service identity."""
    ent = (entity or "").strip().lower()
    lbl = (subject_label or "").strip().lower()
    key = (subject_key or "").strip().lower()

    if ent in _GENERIC_ENTITY_WORDS or _GENERIC_ENTITY_PATTERN.match(ent):
        return True

    recognized_core_keys = {"electricity", "water", "gas", "heating", "sewage", "refuse"}
    if not ent:
        if key in recognized_core_keys:
            return False
        if not lbl or lbl in _GENERIC_ENTITY_WORDS or _GENERIC_ENTITY_PATTERN.match(lbl):
            return True

    return False


def has_meaningful_predicate(text: str) -> bool:
    """Check if text contains a concrete civic action, state change, event, or consequence.

    Rejects predicateless geographic prepositional fragments (e.g. 'В районе Водоканала на Пролетарском')
    and chat meta-inquiries ('жители обсуждают текущую ситуацию... подробности уточняются').
    """
    if not text or not text.strip():
        return False

    cleaned = text.strip()
    # Strip leading attribution phrases
    cleaned = _ATTRIBUTION_PREFIX_RE.sub("", cleaned).strip()
    # Strip chatter meta / filler
    without_chatter = _CHATTER_META_RE.sub("", cleaned).strip()
    without_chatter = re.sub(r"[,\s\.\!\?\–—\-]+$", "", without_chatter).strip()

    if not without_chatter:
        return False

    # Check if what remains is purely a geographic phrase without verbs/states
    if _PURE_GEOGRAPHIC_FRAGMENT_RE.match(without_chatter):
        # Even if it matches pure geographic pattern, verify if any civic event token is present
        if not _CIVIC_EVENT_TOKENS_RE.search(without_chatter):
            return False

    # Must contain at least one civic event / action / state token
    if not _CIVIC_EVENT_TOKENS_RE.search(without_chatter):
        return False

    return True


def validate_story_publication_eligibility(
    payload: Any, fallback_text: str = ""
) -> tuple[bool, str | None]:
    """Validate whether an event payload is eligible to produce a publishable story card."""
    if payload is None:
        if fallback_text and has_meaningful_predicate(fallback_text):
            return True, None
        return False, "empty_payload"

    evidence_items = getattr(payload, "evidence_items", ()) or ()
    if not evidence_items:
        # Fallback to headline / summary / key facts if no evidence items
        headline = getattr(payload, "headline", "") or ""
        summary = getattr(payload, "digest_summary", "") or getattr(payload, "summary", "") or ""
        if (
            not has_meaningful_predicate(headline)
            and not has_meaningful_predicate(summary)
            and not has_meaningful_predicate(fallback_text)
        ):
            return False, "lacks_meaningful_predicate"
        return True, None

    publish_items = [
        item for item in evidence_items if getattr(item, "publication_use", "PUBLISH") != "EXCLUDE"
    ]
    if not publish_items:
        return False, "no_publishable_evidence"

    # Rule 1: resident_question cannot standalone produce a publishable story
    non_question_items = [
        item for item in publish_items if getattr(item, "kind", "") != "resident_question"
    ]
    if not non_question_items:
        return False, "resident_question_only"

    # Rule 2: service_access requires concrete named entity or recognized utility
    service_items = [
        item for item in non_question_items if getattr(item, "kind", "") == "service_access"
    ]
    if service_items and len(service_items) == len(non_question_items):
        all_generic = True
        for s_item in service_items:
            s_state = getattr(s_item, "service_state", None)
            ent = getattr(s_state, "entity", "") if s_state else ""
            lbl = getattr(s_state, "subject_label", "") if s_state else ""
            key = getattr(s_state, "subject_key", "") if s_state else ""
            if not is_generic_service_entity(ent, lbl, key):
                all_generic = False
                break
        if all_generic:
            return False, "service_access_without_concrete_entity"

    # Rule 3: at least one substantive evidence item or story summary must have a meaningful predicate
    has_predicate = any(
        has_meaningful_predicate(getattr(item, "text", "")) for item in non_question_items
    )
    if not has_predicate:
        hl = getattr(payload, "headline", "") or ""
        sm = getattr(payload, "digest_summary", "") or getattr(payload, "summary", "") or ""
        if not has_meaningful_predicate(hl) and not has_meaningful_predicate(sm):
            return False, "lacks_meaningful_predicate"

    return True, None
