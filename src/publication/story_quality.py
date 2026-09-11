"""Deterministic quality, predicate, and entity grounding guards for publication stories."""

from __future__ import annotations

import re
from typing import Any

RECOGNIZED_CORE_SERVICE_KEYS = frozenset(
    {
        "electricity",
        "power",
        "power_supply",
        "water",
        "water_supply",
        "gas",
        "gas_supply",
        "heating",
        "sewage",
        "refuse",
        "transport",
        "connectivity",
        "telecom",
        "telecommunications",
        "internet",
        "banking",
        "municipal_service",
        "municipal_infrastructure",
        "utilities",
    }
)

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
    r"^(?:по\s+(?:сообщениям|сообщению|словам|информации|данным)\s+(?:жителей|жителя|горожан|горожанина|очевидцев|очевидца)|"
    r"(?:жители|житель|горожане|горожанин|очевидцы|очевидец)\s+(?:сообщают|сообщает|пишут|пишет|отмечают|отмечает)|"
    r"в\s+(?:местных\s+чатах|соцсетях|сетях|пабликах)(?:\s+(?:жители|житель|горожане|горожанин))?(?:\s+(?:подтверждают|подтверждает|сообщают|сообщает|пишут|пишет))?|"
    r"(?:несколько\s+)?(?:горожан|жителей)\s+(?:сообщают|сообщает|пишут|пишет|подтверждают|подтверждает))[,:\s]+",
    re.IGNORECASE,
)

_CHATTER_META_RE = re.compile(
    r"\b(?:"
    r"(?:(?:жители|житель|горожане|горожанин|в\s+соцсетях|в\s+чатах)\s+)?"
    r"(?:обсуждают|выясняют|интересуются|сообщают\s+о|сообщает\s+о|сообщени[ея]\s+о)?(?:\s+текущ\w*)?\s+ситуаци[июей]\w*|"
    r"подробности\s+уточняются|информация\s+уточняется|ситуация\s+уточняется|"
    r"выясняют\s+обстоятельства|жители\s+интересуются|"
    r"конкретный\s+вид\s+сервиса\s+(?:в\s+сообщениях\s+)?не\s+уточняется|"
    r"вид\s+сервиса\s+(?:в\s+сообщениях\s+)?не\s+уточняется"
    r")\b",
    re.IGNORECASE,
)

_CIVIC_EVENT_TOKENS_RE = re.compile(
    r"\b(?:"
    # Verbs / participles of action, state, change (past, present, future)
    r"отключ\w*|включ\w*|пропа[лв]\w*|исчез\w*|верну\w*|возвращ\w*|"
    r"восстанов\w*|возобнов\w*|заработа\w*|работа\w*|"
    r"выш[ели]\w*|выход\w*|пополн\w*|поступ\w*|"
    r"прорва\w*|прорыв\w*|теч[её]\w*|капа\w*|ут[её]к\w*|утечк\w*|"
    r"перекры\w*|затоп\w*|гор[яеи]\w*|потуш\w*|сби\w*|упа[лд]\w*|пада\w*|"
    r"взорв\w*|взрыв\w*|повред\w*|разруш\w*|"
    r"ремонтир\w*|почин\w*|чин[яи]\w*|провод\w*|прове[лд]\w*|"
    r"откры\w*|закры\w*|запуст\w*|пуск\w*|пода[юе]\w*|подач\w*|"
    r"ход[яи]\w*|езди\w*|курсир\w*|перевоз\w*|"
    r"списа\w*|подорож\w*|подешев\w*|зафиксир\w*|замеч\w*|наблюда\w*|"
    r"сниз\w*|повыс\w*|вырос\w*|раст[еу]\w*|увелич\w*|уменьш\w*|"
    r"выплат\w*|начисл\w*|получ\w*|направ\w*|"
    r"приним\w*|приня\w*|утверд\w*|ввел\w*|ввод\w*|измен\w*|отмен\w*|"
    r"сообщ\w*|подтверд\w*|устрани\w*|ликвиди\w*|"
    r"заверш\w*|оконч\w*|нач[ая]\w*|продолж\w*|"
    r"произош\w*|происход\w*|случи\w*|обнаруж\w*|установ\w*|постро\w*|сдела\w*|"
    r"огранич\w*|перенес\w*|достав\w*|привез\w*|"
    r"прибы\w*|приезжа\w*|приеха\w*|дела\w*|сдела\w*|сто[яи]\w*|появи\w*|появля\w*|"
    r"вед\w*|выполн\w*|осуществл\w*|производ\w*|обеспеч\w*|организов\w*|заяв\w*|предупред\w*|опубликов\w*|планиру\w*|оста[её]тся|сохран\w*|"
    # Event / state nouns
    r"авари[яи]|прорыв\w*|ремонт\w*|отключени[ея]|перебо[яев]|восстановлени[ея]|возобновлени[ея]|"
    r"взрыв\w*|обстрел\w*|сирен\w*|пожар\w*|дым\w*|дтп|сбой\w*|неисправност\w*|проблем\w*|"
    r"напряжени[ея]|скач[ок]\w*|график\w*|подвоз\w*|задержк\w*|отмен\w*|рейс\w*|маршрут\w*|"
    r"выплат\w*|пособи[ея]|запрет\w*|штраф\w*|при[её]м\w*|проверк\w*|"
    # Predicates / states
    r"нет|нету|есть|доступен|доступна|доступно|доступны|недоступен|недоступна|недоступно|недоступны|"
    r"отсутству\w*|восстановлен\w*|отключен\w*|перекрыт\w*|открыт\w*|закрыт\w*|завершен\w*|поврежден\w*|"
    r"0\s+по\s+(?:свету|воде|газу)|"
    # General Russian verb morphology fallback (verbs ending in -лся, -лась, -лось, -лись, -ется, -ются, -ится, -ятся)
    r"[а-яё]{3,}(?:лся|лась|лось|лись|ется|ются|ится|ятся)|"
    # Quantitative facts / measurements
    r"\d+\s*(?:в|вольт|квт|руб|рублей|р\.|грн|мбит|%|процент\w*|автобус\w*|рейс\w*|человек\w*|дом\w*|улиц\w*)"
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

    if not ent:
        if key in RECOGNIZED_CORE_SERVICE_KEYS:
            return False
        if not lbl or lbl in _GENERIC_ENTITY_WORDS or _GENERIC_ENTITY_PATTERN.match(lbl):
            return True
        return False

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
        key_facts = getattr(payload, "key_facts", ()) or ()
        texts = [headline, summary, fallback_text] + list(key_facts)
        if not any(has_meaningful_predicate(t) for t in texts if t):
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

    # Rule 2: Generic anonymous service check
    all_story_text = " ".join(
        [
            getattr(payload, "headline", "") or "",
            getattr(payload, "digest_summary", "") or getattr(payload, "summary", "") or "",
        ]
        + [getattr(item, "text", "") for item in non_question_items]
    ).lower()

    cat = getattr(payload, "category", "") or ""
    tags = {str(t).lower() for t in (getattr(payload, "tags", ()) or ())}
    has_recognized_domain = cat.lower() in RECOGNIZED_CORE_SERVICE_KEYS or bool(
        tags.intersection(RECOGNIZED_CORE_SERVICE_KEYS)
    )
    has_concrete_service_state = any(
        getattr(item, "kind", "") == "service_access"
        and not is_generic_service_entity(
            getattr(getattr(item, "service_state", None), "entity", ""),
            getattr(getattr(item, "service_state", None), "subject_label", ""),
            getattr(getattr(item, "service_state", None), "subject_key", ""),
        )
        for item in non_question_items
    )

    if not has_recognized_domain and not has_concrete_service_state:
        if ("вид сервиса" in all_story_text and "не уточняется" in all_story_text) or re.search(
            r"\b(?:проблемный\s+сервис|сторонний\s+сервис|городской\s+сервис)\b",
            all_story_text,
        ):
            return False, "service_access_without_concrete_entity"

        hl = getattr(payload, "headline", "") or ""
        if re.search(r"\bсервис\b", hl, re.IGNORECASE):
            named_tokens = {
                "водоканал",
                "горгаз",
                "россети",
                "банк",
                "связь",
                "интернет",
                "провайдер",
                "телеком",
                "автобус",
                "такси",
                "почта",
                "нотариус",
            }
            if not any(token in all_story_text for token in named_tokens):
                return False, "service_access_without_concrete_entity"

    service_items = [
        item for item in non_question_items if getattr(item, "kind", "") == "service_access"
    ]
    if service_items and len(service_items) == len(non_question_items):
        all_generic = True
        for s_item in service_items:
            s_state = getattr(s_item, "service_state", None)
            if s_state:
                ent = getattr(s_state, "entity", "")
                lbl = getattr(s_state, "subject_label", "")
                key = getattr(s_state, "subject_key", "")
                if not is_generic_service_entity(ent, lbl, key):
                    all_generic = False
                    break
            else:
                op_obs = getattr(payload, "operational_observations", ()) or ()
                found_valid_op = False
                for obs in op_obs:
                    o_key = getattr(obs, "subject_key", "") or ""
                    o_lbl = getattr(obs, "subject_label", "") or ""
                    o_ent = getattr(obs, "entity", "") or ""
                    if not is_generic_service_entity(o_ent, o_lbl, o_key):
                        found_valid_op = True
                        break
                if found_valid_op:
                    all_generic = False
                    break

                if has_recognized_domain:
                    all_generic = False
                    break

                text_to_test = getattr(s_item, "text", "") or ""
                hl = getattr(payload, "headline", "") or ""
                combined = f"{hl} {text_to_test}".lower()
                has_generic_word = any(w in combined for w in _GENERIC_ENTITY_WORDS)
                if not has_generic_word:
                    all_generic = False
                    break

        if all_generic:
            return False, "service_access_without_concrete_entity"

    # Rule 3: at least one substantive evidence item or story summary must contain a meaningful predicate
    has_predicate = any(
        has_meaningful_predicate(getattr(item, "text", ""))
        or (
            getattr(getattr(item, "service_state", None), "state", "").upper()
            in {"AVAILABLE", "UNAVAILABLE", "DEGRADED", "RESTRICTED"}
        )
        for item in non_question_items
    )
    if not has_predicate:
        hl = getattr(payload, "headline", "") or ""
        sm = getattr(payload, "digest_summary", "") or getattr(payload, "summary", "") or ""
        if not has_meaningful_predicate(hl) and not has_meaningful_predicate(sm):
            return False, "lacks_meaningful_predicate"

    return True, None
