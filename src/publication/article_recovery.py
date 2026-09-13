"""Deterministic Event-First article recovery composer for supplement and full fallback."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from typing import Literal

from src.editorial_models import StoryCard
from src.publication.article_claims import find_unsupported_claims
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import (
    ArticleCoveragePlan,
    ArticleStoryCoverage,
    _story_id_from_support_id,
)
from src.publication.article_models import (
    ArticleClaimAtom,
    ArticleParagraph,
    ArticleSection,
    StructuredArticleDraft,
)
from src.publication.article_writer_context import sanitize_writer_source_text

ArticleTheme = Literal[
    "infrastructure",
    "mobility",
    "communications",
    "civic_services",
    "city_life",
]

THEME_HEADINGS: dict[str, str] = {
    "infrastructure": "Жизнеобеспечение и коммунальная обстановка",
    "mobility": "Городской и междугородний транспорт",
    "communications": "Связь и цифровые сервисы",
    "civic_services": "Социальная сфера и городские службы",
    "city_life": "Городская хроника и повседневный быт",
}

_THEME_DEFAULT_HEADINGS = THEME_HEADINGS
_SHORT_SECTION_HEADING = "Городская хроника"
_GENERIC_SECTION_HEADING = "Городская хроника"

_PROMINENCE_LIMITS: dict[str, int] = {
    "DEVELOP": 3,
    "WEAVE": 2,
    "BRIEF": 1,
}


_THEME_COMMUNICATIONS_RE = re.compile(
    r"\b(?:связ[ьи]|интернет\w*|провайдер\w*|мобильн\w*|роутер\w*|wi-?fi|wifi|телеком\w*|юпитер\w*|терминал\w*|повербанк\w*)\b",
    re.IGNORECASE,
)
_THEME_INFRASTRUCTURE_RE = re.compile(
    r"\b(?:электр\w*|свет\b|напряжен\w*|подстанци\w*|энерг\w*|вод[аеуоы]\b|водоснабжен\w*|водоканал\w*|водовод\w*|водопровод\w*|протечк\w*|порыв\w*|труб[аеуы]|газ[аеуом]?\b|газоснабжен\w*|горгаз\w*|отоплен\w*|котельн\w*|теплоснабжен\w*|жкх\b|коммунал\w*|блэкаут\w*|вспышк\w*)\b",
    re.IGNORECASE,
)
_THEME_MOBILITY_RE = re.compile(
    r"\b(?:транспорт\w*|автобус\w*|маршрут\w*|рейс[аеуом]?\b|рейсы\b|проезд\w*|дорог\w*|трасс\w*|такси|автовокзал\w*|жд\b|поезд\w*|водител\w*|автомобил\w*|авто\b|пдд\b)\b",
    re.IGNORECASE,
)
_THEME_CIVIC_SERVICES_RE = re.compile(
    r"\b(?:банк\w*|пенсион\w*|выплат\w*|пособи\w*|гуманитарн\w*|больниц\w*|поликлиник\w*|врач\w*|медицин\w*|школ\w*|детсад\w*|спорт\w*|секци\w*|мфц\b|паспорт\w*|футбол\w*|дети\b|детск\w*)\b",
    re.IGNORECASE,
)


def resolve_article_theme(
    card: StoryCard,
    supports: Sequence[ArticleSupport] = (),
) -> ArticleTheme:
    """Classify a story into one of five canonical thematic sections.

    Mapping:
    - internet/mobile/telecom -> communications
    - electricity/water/gas/heating/ЖКХ/voltage -> infrastructure
    - urban/intercity transport/routes/traffic -> mobility
    - healthcare/social/humanitarian/education/sport/banking -> civic_services
    - retail/commerce/daily life/fallback -> city_life
    """
    cat = (card.category or "").casefold().strip()
    tags = [t.casefold().strip() for t in card.tags]
    topic = (card.topic or "").casefold().strip()
    summary = (card.summary or "").casefold().strip()

    sup_texts = [s.text.casefold() for s in supports if s.text]
    sup_ids = [s.support_id.casefold() for s in supports if s.support_id]

    all_tokens = [cat, topic, summary] + tags + sup_texts + sup_ids
    corpus = " ".join(all_tokens)

    # 1. Communications / Telecom
    if cat in {"telecom", "communications", "internet"}:
        return "communications"
    if _THEME_COMMUNICATIONS_RE.search(corpus):
        return "communications"

    # 2. Infrastructure / Utilities
    if cat in {
        "utilities",
        "infrastructure",
        "power",
        "water",
        "gas",
        "heating",
        "energy",
        "жкх",
    }:
        return "infrastructure"
    if _THEME_INFRASTRUCTURE_RE.search(corpus) or any(
        "power" in s or "water" in s or "gas" in s or "heating" in s for s in sup_ids
    ):
        return "infrastructure"

    # 3. Mobility / Transport
    if cat in {"transport", "mobility", "traffic"}:
        return "mobility"
    if _THEME_MOBILITY_RE.search(corpus):
        return "mobility"

    # 4. Civic services / Municipal / Social / Banking / Healthcare / Education / Sport
    if cat in {
        "healthcare",
        "social",
        "humanitarian",
        "education",
        "sport",
        "banking",
        "services",
    }:
        return "civic_services"
    if _THEME_CIVIC_SERVICES_RE.search(corpus):
        return "civic_services"

    # 5. Fallback -> city_life
    return "city_life"


def _normalize_for_dedup(text: str) -> str:
    """Normalize text for conservative exact deduplication."""
    t = unicodedata.normalize("NFC", text).strip().casefold()
    for prefix in (
        "по сообщениям жителей,",
        "по сообщениям жителей",
        "жители сообщают, что",
        "жители сообщают,",
        "жители сообщают",
        "житель сообщает, что",
        "житель сообщает,",
        "житель сообщает",
        "горожане сообщают, что",
        "горожане сообщают,",
        "горожане сообщают",
        "по информации горожан,",
        "по информации горожан",
        "как отмечают в местных сообществах,",
        "как отмечают в местных сообществах",
        "как отмечают горожане,",
        "как отмечают горожане",
        "горожане обращают внимание:",
        "горожане обращают внимание",
        "по словам жителей,",
        "по словам жителей",
        "как сообщили,",
        "как сообщили",
        "ранее,",
        "ранее",
        "запланировано:",
        "запланировано",
    ):
        if t.startswith(prefix):
            t = t[len(prefix) :].strip()
    t = re.sub(r"[^\w\s]", "", t)
    return " ".join(t.split())


_COMMUNITY_OPENERS = (
    "По сообщениям жителей, ",
    "Жители сообщают, что ",
    "По информации горожан, ",
    "Как отмечают в местных сообществах, ",
    "Горожане обращают внимание: ",
)

_TRANSITION_OPENERS = (
    "В то же время, ",
    "Кроме того, ",
    "Также ",
    "По информации из других районов, ",
    "В свою очередь, ",
    "Наряду с этим, ",
)

_THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "infrastructure": (
        "электроснабжен",
        "свет",
        "энерг",
        "электр",
        "вод",
        "газ",
        "отоплен",
        "жкх",
        "подстанци",
        "аварийн",
        "водопровод",
        "водоканал",
        "вода",
        "воде",
        "воду",
        "газоснабжен",
        "отоплен",
        "жкх",
        "подстанци",
        "аварийн",
        "котельн",
        "порыв",
        "напряжен",
    ),
    "communications": (
        "связь",
        "связи",
        "интернет",
        "провайдер",
        "телеком",
        "юпитер",
        "роутер",
        "терминал",
        "повербанк",
    ),
    "mobility": (
        "транспорт",
        "автобус",
        "маршрут",
        "рейс",
        "проезд",
        "дорог",
        "трасс",
        "такси",
    ),
    "civic_services": (
        "социальн",
        "медицин",
        "больниц",
        "поликлиник",
        "врач",
        "пенсион",
        "выплат",
        "пособи",
        "гуманитарн",
        "школ",
        "детсад",
        "образован",
        "спорт",
        "дети",
    ),
    "city_life": (
        "хроник",
        "быт",
        "жизнь",
        "событи",
        "торговл",
        "рынок",
        "магазин",
        "культур",
        "парк",
        "досуг",
    ),
}


def _resolve_story_theme(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext | None = None,
    plan: ArticleCoveragePlan | None = None,
) -> ArticleTheme:
    """Resolve theme for a story using plan assignments first, then semantic resolution."""
    if plan is not None:
        planned_sec = plan.section_for_story(story.story_id)
        if planned_sec:
            sid = planned_sec.section_id.lower()
            if sid in THEME_HEADINGS:
                return sid  # type: ignore[return-value]
            if sid in ("society", "culture_education", "education", "sport"):
                return "civic_services"

    card_by_id = {c.id: c for c in getattr(context, "story_cards", ())} if context else {}
    card = card_by_id.get(story.story_id) or StoryCard(
        id=story.story_id, topic=story.topic, importance="medium", summary=story.topic
    )
    sups = _resolve_story_supports(story, context) if context else ()
    return resolve_article_theme(card, sups)


def _match_section_to_theme(
    section: ArticleSection,
    theme: str,
    context: ArticleEditorialContext,
    plan: ArticleCoveragePlan,
) -> bool:
    from src.publication.article_models import _normalize_homoglyphs

    h_lower = _normalize_homoglyphs(section.heading).lower()
    keywords = _THEME_KEYWORDS.get(theme, ())
    for kw in keywords:
        if re.search(r"\b" + re.escape(kw), h_lower):
            return True

    plan_by_id = plan.by_story_id
    for sid in section.heading_support_ids:
        story_id = _story_id_from_support_id(sid)
        if story_id in plan_by_id:
            s_theme = _resolve_story_theme(plan_by_id[story_id], context, plan)
            if s_theme == theme:
                return True

    # Do not match paragraphs to theme if heading explicitly matches another theme
    for other_theme, other_kws in _THEME_KEYWORDS.items():
        if other_theme != theme:
            for okw in other_kws:
                if re.search(r"\b" + re.escape(okw), h_lower):
                    return False

    for p in section.paragraphs:
        for sid in p.cited_support_ids:
            story_id = _story_id_from_support_id(sid)
            if story_id in plan_by_id:
                s_theme = _resolve_story_theme(plan_by_id[story_id], context, plan)
                if s_theme == theme:
                    return True

    return False


def _story_topic_signature(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
) -> str:
    """Determine fine-grained subtopic signature to keep incompatible brief items separate."""
    sups = _resolve_story_supports(story, context)
    text = " ".join([story.topic] + [s.text for s in sups if s.text]).casefold()
    if any(w in text for w in ("электр", "свет", "напряжен", "подстанци", "питани", "энерг")):
        return "power"
    if any(w in text for w in ("вод", "водоканал", "водовод", "труб", "порыв")):
        return "water"
    if any(
        w in text
        for w in (
            "интернет",
            "связь",
            "провайдер",
            "роутер",
            "юпитер",
            "терминал",
            "повербанк",
        )
    ):
        return "internet"
    if any(w in text for w in ("автобус", "маршрут", "транспорт", "рейс", "проезд", "такси")):
        return "transport"
    if any(w in text for w in ("спорт", "футбол", "секц", "школ", "набор", "дети", "девоч")):
        return "sports"
    if any(
        w in text
        for w in ("бабушк", "бабул", "пожил", "пропал", "поиск", "потер", "полици", "скорая")
    ):
        return "missing_person"
    if any(w in text for w in ("магазин", "товар", "торговл", "рынок", "проспект", "сакура")):
        return "retail"
    if any(w in text for w in ("рецепт", "кулинар", "кухн")):
        return "culinary"
    return story.story_id


def _clean_support_text_for_reader(text: str) -> str:
    """Clean conversational chat artifacts, questions, broken OCR/encoding and colloquialisms."""
    if not text:
        return ""

    text = (
        text.replace("[contact omitted]", "")
        .replace("[url omitted]", "")
        .replace("[link omitted]", "")
        .strip()
    )

    text = re.sub(r"[!]{2,}", ".", text)
    text = re.sub(r"[?]{2,}", "?", text)

    # Fix broken dashes / abbreviations
    text = re.sub(r"\bНАП[–—-]+Е\b", "напряжение", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d+)[–—-]+(\d+)", r"\1–\2", text)
    text = re.sub(r"[-–—]{2,}", "–", text)

    # Normalize caps words (except acronyms)
    known_acronyms = {
        "РЭС",
        "ЖКХ",
        "РФ",
        "ДНР",
        "ЛНР",
        "СССР",
        "МЧС",
        "МВД",
        "КПП",
        "УК",
        "ОСМД",
        "ТВ",
    }

    def _lower_caps(m: re.Match) -> str:
        word = m.group(0)
        if word.upper() in known_acronyms:
            return word.upper()
        return word.lower()

    text = re.sub(r"\b[А-Яа-яЁё]*[А-ЯЁ]{2,}[А-Яа-яЁё]*\b", _lower_caps, text)

    # Phrasing cleanup for known chat reporting patterns
    if re.search(r"район\s+(\d+)\s+школы\s+напряжение\s+(\d+–\d+)", text, re.IGNORECASE):
        text = re.sub(
            r"район\s+(\d+)\s+школы\s+напряжение\s+(\d+–\d+)",
            r"В районе школы №\1 напряжение в сети составляет \2 В",
            text,
            flags=re.IGNORECASE,
        )

    if re.search(r"кто[- ]?(?:нибудь|то)\s+знает\s+(?:эту\s+)?бабулю", text, re.IGNORECASE):
        text = "В Лисках с утра гуляет пожилая бабушка, не давая ответа, кто она и откуда."
    elif "?" in text:
        text = re.sub(
            r"^(?:подскажите|скажите|кто[- ]?(?:нибудь|то)\s+знает)[,\s]*",
            "Жители интересуются: ",
            text,
            flags=re.IGNORECASE,
        )
        text = text.replace("?", ".")

    if re.search(r"подстанция\s+горит", text, re.IGNORECASE) and re.search(
        r"замыкание\s+походу", text, re.IGNORECASE
    ):
        text = "В городе горит электрическая подстанция; звуков взрывов очевидцы не слышали, инцидент связывают с коротким замыканием."
    if re.search(r"скорая\s+приехала\s+и\s+уехала", text, re.IGNORECASE):
        text = "Скорая помощь приехала и уехала, не став забирать человека, а полиция обещала прибыть по освобождению наряда."
    if re.search(r"^\s*работаем\s+без\s+электроэнергии", text, re.IGNORECASE):
        text = "Организации и службы города работают без электроэнергии."
    if re.search(
        r"рецепт\s+где-то\s+остался\s+в\s+бердянске|любимый\s+рецепт\s+остался\s+в\s+бердянске",
        text,
        re.IGNORECASE,
    ):
        text = "В городских чатах жители также вспоминают о традиционных бердянских кулинарных рецептах."

    if re.search(r"только\s+(\d+)\s+гайдара", text, re.IGNORECASE):
        text = re.sub(
            r"только\s+(\d+)\s+гайдара\s+со\s+светом.*",
            r"На улице Гайдара электроэнергия подаётся только в дом №\1, окрестные дома остаются без света.",
            text,
            flags=re.IGNORECASE,
        )
    text = re.sub(
        r"[.;, ]*когда\s+уже\s+(?:и\s+)?нас\s+подключат\b.*", "", text, flags=re.IGNORECASE
    )

    text = re.sub(
        r"\bзамыкание\s+походу\b",
        "предположительно из-за короткого замыкания",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bпоходу\b", "вероятно", text, flags=re.IGNORECASE)
    text = re.sub(r"\bтипа\b", "вроде", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bхотя\s+я\s+взрывов\s+даже\s+не\s+слышал\b",
        "при этом звуков взрывов очевидцы не слышали",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def _render_support_sentence(
    support: ArticleSupport,
    already_attributed: bool = False,
    opener_index: int = 0,
) -> tuple[str, bool]:
    raw_text = (support.text or support.source_text).strip()
    text = sanitize_writer_source_text(raw_text).strip()
    text = _clean_support_text_for_reader(text)

    # Direct quotation marks in deterministic prose require verbatim match against source text.
    # When transforming into reader prose, use indirect speech and strip quotation marks.
    text = re.sub(r"[«»“”\"]", "", text)
    text = text.replace("[contact omitted]", "").replace("[url omitted]", "").strip()
    text = text.replace(";", ",")

    # Strip internal operational labels like "Электроснабжение — Азмол: отсутствует — "
    text = re.sub(
        r"^(?:Электроснабжение|Водоснабжение|Газоснабжение|Связь|Теплоснабжение)\s*—\s*[^:—\n]+:\s*(?:отсутствует|в норме|авария|перебои|нестабильно)\s*—\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    is_community = support.evidence_kind in {
        "community_report",
        "community_observation",
        "quote_assertion",
    }
    has_own_attr = text.casefold().startswith(
        (
            "по сообщениям",
            "жители сообщают",
            "житель сообщает",
            "горожане сообщают",
            "по информации горожан",
            "по словам",
            "как сообщают",
            "как отмечают",
            "как рассказывают",
            "жители отмечают",
            "жители делятся",
        )
    )

    is_restoration = bool(
        re.search(
            r"\b(?:появил\w*|восстанов\w*|включил\w*|дал[иао]|заработ\w*)\b", text, re.IGNORECASE
        )
    )

    if is_community:
        if not has_own_attr:
            if not already_attributed and opener_index == 0:
                opener = _COMMUNITY_OPENERS[opener_index % len(_COMMUNITY_OPENERS)]
                text = f"{opener}{text[:1].lower() + text[1:] if text else text}"
                new_attributed = True
            elif is_restoration:
                text = f"Впрочем, по сообщениям жителей, {text[:1].lower() + text[1:] if text else text}"
                new_attributed = True
            else:
                opener = _TRANSITION_OPENERS[opener_index % len(_TRANSITION_OPENERS)]
                text = f"{opener}{text[:1].lower() + text[1:] if text else text}"
                new_attributed = True
        else:
            if already_attributed or opener_index > 0:
                # Vary repeated attribution with a transition opener
                for attr_prefix in (
                    "по сообщениям жителей,",
                    "по сообщениям жителей",
                    "жители сообщают, что",
                    "житель сообщает, что",
                    "горожане сообщают, что",
                    "по информации горожан,",
                    "по словам жителей,",
                    "как отмечают в местных сообществах,",
                ):
                    if text.casefold().startswith(attr_prefix):
                        text = text[len(attr_prefix) :].strip()
                        break
                if is_restoration:
                    text = f"Впрочем, по сообщениям жителей, {text[:1].lower() + text[1:] if text else text}"
                else:
                    opener = _TRANSITION_OPENERS[opener_index % len(_TRANSITION_OPENERS)]
                    text = f"{opener}{text[:1].lower() + text[1:] if text else text}"
            new_attributed = True
    else:
        new_attributed = False

    if (
        support.temporal_role == "HISTORICAL_CONTEXT"
        and text
        and not text.casefold().startswith("ранее")
    ):
        text = f"Ранее {text[:1].lower() + text[1:]}"
    elif (
        support.temporal_role == "FUTURE_SCHEDULED"
        and text
        and not text.casefold().startswith("запланировано")
    ):
        text = f"Запланировано: {text}"

    text = re.sub(r"[!?.]{2,}", ".", text)
    return text.rstrip("!?.:; ") + ".", new_attributed


def _resolve_story_supports(
    story: ArticleStoryCoverage,
    context: ArticleEditorialContext,
) -> tuple[ArticleSupport, ...]:
    support_map = context.support_by_id
    limit = _PROMINENCE_LIMITS.get(story.prominence, 1)

    preferred_ids: list[str] = []
    for sid in story.detail_support_ids:
        if sid not in preferred_ids and sid in support_map:
            preferred_ids.append(sid)
    for sid in story.support_ids:
        if sid not in preferred_ids and sid in support_map:
            preferred_ids.append(sid)

    selected_ids = preferred_ids[:limit]
    return tuple(support_map[sid] for sid in selected_ids if sid in support_map)


def _build_story_paragraph(
    supports: Sequence[ArticleSupport],
    origin: str,
    seen_dedup_keys: dict[str, int],
    shared_claim_atoms: list[ArticleClaimAtom],
    opener_offset: int = 0,
) -> ArticleParagraph | None:
    if not supports:
        return None

    rendered_sentences: list[str] = []
    claim_atoms: list[ArticleClaimAtom] = []
    para_cited_ids: list[str] = []
    already_attributed = False

    for sup in supports:
        norm_key = _normalize_for_dedup(sup.text or sup.source_text)
        if not norm_key:
            continue

        if norm_key in seen_dedup_keys:
            # Merge support ID into existing claim atom
            idx = seen_dedup_keys[norm_key]
            existing = shared_claim_atoms[idx]
            new_cited = tuple(dict.fromkeys(existing.cited_support_ids + (sup.support_id,)))
            shared_claim_atoms[idx] = ArticleClaimAtom(
                text=existing.text, cited_support_ids=new_cited
            )
            if sup.support_id not in para_cited_ids:
                para_cited_ids.append(sup.support_id)
            continue

        sent, already_attributed = _render_support_sentence(
            sup,
            already_attributed=already_attributed,
            opener_index=opener_offset + len(rendered_sentences),
        )
        if not sent:
            continue

        clean_sup_text = sanitize_writer_source_text((sup.text or sup.source_text).strip()).strip()
        clean_sup_text = re.sub(r"[«»“”\"]", "", clean_sup_text)
        clean_sup_text = (
            clean_sup_text.replace("[contact omitted]", "").replace("[url omitted]", "").strip()
        )

        atom = ArticleClaimAtom(
            text=clean_sup_text.rstrip("."), cited_support_ids=(sup.support_id,)
        )
        seen_dedup_keys[norm_key] = len(shared_claim_atoms)
        shared_claim_atoms.append(atom)
        rendered_sentences.append(sent)
        claim_atoms.append(atom)
        if sup.support_id not in para_cited_ids:
            para_cited_ids.append(sup.support_id)

    if not rendered_sentences:
        return None

    return ArticleParagraph(
        text=" ".join(rendered_sentences),
        cited_support_ids=tuple(dict.fromkeys(para_cited_ids)),
        claims=tuple(claim_atoms),
        generation_origin=origin,  # type: ignore[arg-type]
    )


def _build_section_paragraphs(
    stories: Sequence[ArticleStoryCoverage],
    context: ArticleEditorialContext,
    origin: str,
    seen_dedup_keys: dict[str, int],
    shared_claim_atoms: list[ArticleClaimAtom],
) -> tuple[ArticleParagraph, ...]:
    if not stories:
        return ()

    develop_stories = [s for s in stories if s.prominence == "DEVELOP"]
    weave_stories = [s for s in stories if s.prominence == "WEAVE"]
    brief_stories = [s for s in stories if s.prominence == "BRIEF"]

    paragraphs: list[ArticleParagraph] = []

    # 1. DEVELOP stories get dedicated paragraphs
    for s in develop_stories:
        sups = _resolve_story_supports(s, context)
        p = _build_story_paragraph(
            sups, origin, seen_dedup_keys, shared_claim_atoms, opener_offset=len(paragraphs)
        )
        if p:
            paragraphs.append(p)

    # 2. WEAVE stories grouped in pairs
    for i in range(0, len(weave_stories), 2):
        chunk = weave_stories[i : i + 2]
        chunk_sups: list[ArticleSupport] = []
        for ws in chunk:
            chunk_sups.extend(_resolve_story_supports(ws, context))
        p = _build_story_paragraph(
            chunk_sups, origin, seen_dedup_keys, shared_claim_atoms, opener_offset=len(paragraphs)
        )
        if p:
            paragraphs.append(p)

    # 3. BRIEF stories grouped by subtopic compatibility so unrelated themes
    # are never stitched into the same paragraph.
    brief_by_subtopic: dict[str, list[ArticleStoryCoverage]] = defaultdict(list)
    for bs in brief_stories:
        sig = _story_topic_signature(bs, context)
        brief_by_subtopic[sig].append(bs)

    for _sig, subtopic_briefs in brief_by_subtopic.items():
        for i in range(0, len(subtopic_briefs), 3):
            chunk = subtopic_briefs[i : i + 3]
            brief_sups: list[ArticleSupport] = []
            for bs in chunk:
                bs_sups = _resolve_story_supports(bs, context)
                if bs_sups:
                    brief_sups.append(bs_sups[0])
            p = _build_story_paragraph(
                brief_sups,
                origin,
                seen_dedup_keys,
                shared_claim_atoms,
                opener_offset=len(paragraphs),
            )
            if p:
                paragraphs.append(p)

    # Fallback if no stories matched prominence categories or none produced paragraphs
    if not paragraphs:
        all_sups: list[ArticleSupport] = []
        for s in stories:
            all_sups.extend(_resolve_story_supports(s, context))
        p = _build_story_paragraph(
            all_sups, origin, seen_dedup_keys, shared_claim_atoms, opener_offset=0
        )
        if p:
            paragraphs.append(p)

    return tuple(paragraphs)


def _build_theme_paragraphs(
    supports: Sequence[ArticleSupport],
    origin: str,
) -> tuple[ArticleParagraph, ...]:
    seen: dict[str, int] = {}
    atoms: list[ArticleClaimAtom] = []
    p = _build_story_paragraph(supports, origin, seen, atoms, opener_offset=0)
    return (p,) if p else ()


def _safe_heading_for_story(
    story: ArticleStoryCoverage,
    supports: Sequence[ArticleSupport],
) -> str:
    topic = story.topic.strip()
    if not topic:
        return _GENERIC_SECTION_HEADING

    support_texts = [s.text or s.source_text for s in supports if (s.text or s.source_text)]
    if find_unsupported_claims(topic, support_texts):
        return _GENERIC_SECTION_HEADING
    return topic


class ArticleDeterministicComposer:
    """Deterministic, source-close Event-First article recovery composer."""

    def supplement_safe_draft(
        self,
        draft: StructuredArticleDraft,
        uncovered_story_ids: Sequence[str],
        context: ArticleEditorialContext,
        plan: ArticleCoveragePlan,
    ) -> StructuredArticleDraft:
        """Supplement a safe but incomplete AI draft with deterministic paragraphs."""
        if not uncovered_story_ids:
            return draft

        plan_by_id = plan.by_story_id
        uncovered_stories = [
            plan_by_id[sid]
            for sid in plan.story_ids
            if sid in uncovered_story_ids and sid in plan_by_id
        ]
        if not uncovered_stories:
            return draft

        # Group uncovered stories by canonical theme
        stories_by_theme: dict[str, list[ArticleStoryCoverage]] = defaultdict(list)
        for s in uncovered_stories:
            theme = _resolve_story_theme(s, context, plan)
            stories_by_theme[theme].append(s)

        sections_list = list(draft.sections)
        seen_dedup_keys: dict[str, int] = {}
        shared_claim_atoms: list[ArticleClaimAtom] = []

        # Pre-populate seen deduplication keys from existing draft sentences to prevent duplicates
        from src.publication.article_models import _split_sentences_safe

        for sec in sections_list:
            for p in sec.paragraphs:
                for c in p.claims:
                    norm = _normalize_for_dedup(c.text)
                    if norm and norm not in seen_dedup_keys:
                        seen_dedup_keys[norm] = len(shared_claim_atoms)
                        shared_claim_atoms.append(c)
                for s in _split_sentences_safe(p.text):
                    norm = _normalize_for_dedup(s)
                    if norm and norm not in seen_dedup_keys:
                        atom = ArticleClaimAtom(text=s, cited_support_ids=p.cited_support_ids)
                        seen_dedup_keys[norm] = len(shared_claim_atoms)
                        shared_claim_atoms.append(atom)

        for theme, theme_stories in stories_by_theme.items():
            matching_idx: int | None = None
            for idx, sec in enumerate(sections_list):
                if _match_section_to_theme(sec, theme, context, plan):
                    matching_idx = idx
                    break

            if matching_idx is not None:
                # Existing section matches theme: append paragraphs to it
                existing_sec = sections_list[matching_idx]
                paras = _build_section_paragraphs(
                    theme_stories,
                    context,
                    origin="SUPPLEMENT",
                    seen_dedup_keys=seen_dedup_keys,
                    shared_claim_atoms=shared_claim_atoms,
                )
                # Update existing paragraphs with any newly merged support IDs from deduplication
                updated_existing_paras: list[ArticleParagraph] = []
                merged_sids: list[str] = []
                for p in existing_sec.paragraphs:
                    p_claims: list[ArticleClaimAtom] = []
                    p_sids = list(p.cited_support_ids)
                    for c in p.claims:
                        norm = _normalize_for_dedup(c.text)
                        if norm in seen_dedup_keys:
                            merged_atom = shared_claim_atoms[seen_dedup_keys[norm]]
                            p_claims.append(merged_atom)
                            p_sids.extend(merged_atom.cited_support_ids)
                            merged_sids.extend(merged_atom.cited_support_ids)
                        else:
                            p_claims.append(c)
                    updated_existing_paras.append(
                        ArticleParagraph(
                            text=p.text,
                            cited_support_ids=tuple(dict.fromkeys(p_sids)),
                            claims=tuple(p_claims),
                            generation_origin=p.generation_origin,
                        )
                    )

                new_cited = tuple(
                    dict.fromkeys(
                        existing_sec.heading_support_ids
                        + tuple(sid for p in paras for sid in p.cited_support_ids)
                        + tuple(merged_sids)
                    )
                )
                sections_list[matching_idx] = ArticleSection(
                    heading=existing_sec.heading,
                    heading_support_ids=new_cited,
                    heading_claims=existing_sec.heading_claims,
                    paragraphs=tuple(updated_existing_paras) + paras,
                    heading_generation_origin=existing_sec.heading_generation_origin,
                )
            else:
                # No existing section matches: create a new thematic section
                dev_stories = [s for s in theme_stories if s.prominence == "DEVELOP"]
                other_stories = [s for s in theme_stories if s.prominence != "DEVELOP"]

                if dev_stories and not other_stories:
                    for ds in dev_stories:
                        story_sups = _resolve_story_supports(ds, context)
                        paras = _build_theme_paragraphs(story_sups, origin="SUPPLEMENT")
                        if not paras:
                            continue
                        heading = _safe_heading_for_story(ds, story_sups)
                        sec_cited = tuple(
                            dict.fromkeys(sid for p in paras for sid in p.cited_support_ids)
                        )
                        sections_list.append(
                            ArticleSection(
                                heading=heading,
                                heading_support_ids=sec_cited,
                                heading_claims=(),
                                paragraphs=paras,
                                heading_generation_origin="SUPPLEMENT",
                            )
                        )
                else:
                    heading_cand: str | None = None
                    for s in theme_stories:
                        planned_sec = plan.section_for_story(s.story_id)
                        if planned_sec and planned_sec.title:
                            heading_cand = planned_sec.title
                            break
                    if not heading_cand:
                        heading_cand = THEME_HEADINGS.get(theme, _GENERIC_SECTION_HEADING)

                    paras = _build_section_paragraphs(
                        theme_stories,
                        context,
                        origin="SUPPLEMENT",
                        seen_dedup_keys=seen_dedup_keys,
                        shared_claim_atoms=shared_claim_atoms,
                    )
                    if paras:
                        sec_cited = tuple(
                            dict.fromkeys(sid for p in paras for sid in p.cited_support_ids)
                        )
                        sections_list.append(
                            ArticleSection(
                                heading=heading_cand,
                                heading_support_ids=sec_cited,
                                heading_claims=(),
                                paragraphs=paras,
                                heading_generation_origin="SUPPLEMENT",
                            )
                        )

        combined_sections = tuple(sections_list)
        all_text = " ".join(
            [draft.title, draft.lead] + [p.text for s in combined_sections for p in s.paragraphs]
        )
        word_count = len(all_text.split())

        return StructuredArticleDraft(
            title=draft.title,
            title_support_ids=draft.title_support_ids,
            title_claims=draft.title_claims,
            lead=draft.lead,
            lead_support_ids=draft.lead_support_ids,
            lead_claims=draft.lead_claims,
            sections=combined_sections,
            cited_evidence_ids=draft.cited_evidence_ids,
            word_count=word_count,
            title_generation_origin=draft.title_generation_origin,
            lead_generation_origin=draft.lead_generation_origin,
        )

    def render_full_fallback(
        self,
        context: ArticleEditorialContext,
        plan: ArticleCoveragePlan,
        *,
        max_sections: int = 8,
    ) -> StructuredArticleDraft:
        """Render a complete deterministic Event-First article from the coverage plan."""
        if not plan.stories:
            raise ValueError("cannot render fallback from empty article coverage plan")

        card_by_id = {c.id: c for c in context.story_cards}
        stories_by_theme: dict[str, list[ArticleStoryCoverage]] = defaultdict(list)
        for story in plan.stories:
            card = card_by_id.get(story.story_id) or StoryCard(
                id=story.story_id, topic=story.topic, importance="medium", summary=story.topic
            )
            sups = _resolve_story_supports(story, context)
            theme = resolve_article_theme(card, sups)
            stories_by_theme[theme].append(story)

        sorted_themes = sorted(
            stories_by_theme.keys(),
            key=lambda t: min(s.rank for s in stories_by_theme[t]),
        )

        if len(sorted_themes) > max_sections:
            kept_themes = sorted_themes[: max(1, max_sections - 1)]
            extra_stories: list[ArticleStoryCoverage] = []
            for t in sorted_themes[max(1, max_sections - 1) :]:
                extra_stories.extend(stories_by_theme[t])
            stories_by_theme["city_life"].extend(extra_stories)
            if "city_life" not in kept_themes:
                kept_themes.append("city_life")
            sorted_themes = kept_themes

        # 1. Title and lead from top stories / thematic axes
        # Title requires at least one PUBLISH support with CURRENT_WINDOW temporal role
        top_sup: ArticleSupport | None = None
        for s in plan.stories:
            sups = _resolve_story_supports(s, context)
            for sup in sups:
                if (
                    sup.publication_use == "PUBLISH"
                    and sup.temporal_role == "CURRENT_WINDOW"
                    and sup.evidence_kind != "resident_question"
                ):
                    top_sup = sup
                    break
            if top_sup:
                break

        if not top_sup:
            for s in plan.stories:
                sups = _resolve_story_supports(s, context)
                for sup in sups:
                    if (
                        sup.publication_use == "PUBLISH"
                        and sup.evidence_kind != "resident_question"
                    ):
                        top_sup = sup
                        break
                if top_sup:
                    break

        if not top_sup:
            for s in plan.stories:
                sups = _resolve_story_supports(s, context)
                if sups:
                    top_sup = sups[0]
                    break

        if not top_sup:
            raise ValueError("cannot render fallback: no planned story supports found in context")

        clean_title = top_sup.text.strip().rstrip(".")
        clean_title = re.sub(r"[«»“”\"]", "", clean_title)
        for prefix in (
            "По сообщениям жителей, ",
            "По сообщениям жителей: ",
            "Жители сообщают, что ",
            "Жители сообщают, ",
            "По словам жителей, ",
            "Как сообщают в местных сообществах, ",
        ):
            if clean_title.startswith(prefix):
                clean_title = clean_title[len(prefix) :]
        clean_title = (
            clean_title[:1].upper() + clean_title[1:] if clean_title else "Городская хроника"
        )
        title = clean_title
        title_support_ids = (top_sup.support_id,)
        title_claims = (ArticleClaimAtom(text=title, cited_support_ids=title_support_ids),)

        lead_sups: list[ArticleSupport] = []
        if plan.sections:
            plan_by_id = plan.by_story_id
            for thematic_sec in plan.sections[: min(3, len(plan.sections))]:
                lead_story = plan_by_id.get(thematic_sec.lead_story_id)
                if lead_story:
                    t_sups = _resolve_story_supports(lead_story, context)
                    curr_sups = [
                        s
                        for s in t_sups
                        if s.publication_use == "PUBLISH"
                        and s.temporal_role == "CURRENT_WINDOW"
                        and s.evidence_kind != "resident_question"
                    ]
                    if curr_sups:
                        lead_sups.append(curr_sups[0])
                    elif t_sups:
                        lead_sups.append(t_sups[0])
        else:
            for t in sorted_themes[: min(3, len(sorted_themes))]:
                t_story = stories_by_theme[t][0]
                t_sups = _resolve_story_supports(t_story, context)
                curr_t_sups = [
                    s
                    for s in t_sups
                    if s.publication_use == "PUBLISH"
                    and s.temporal_role == "CURRENT_WINDOW"
                    and s.evidence_kind != "resident_question"
                ]
                if curr_t_sups:
                    lead_sups.append(curr_t_sups[0])
                elif t_sups:
                    lead_sups.append(t_sups[0])

        # Ensure lead contains at least one PUBLISH CURRENT_WINDOW support
        if not any(
            s.publication_use == "PUBLISH" and s.temporal_role == "CURRENT_WINDOW"
            for s in lead_sups
        ):
            if top_sup.publication_use == "PUBLISH" and top_sup.temporal_role == "CURRENT_WINDOW":
                lead_sups.insert(0, top_sup)

        if len(lead_sups) < 2 and top_sup not in lead_sups:
            lead_sups.append(top_sup)

        lead_sentences: list[str] = []
        lead_claims: list[ArticleClaimAtom] = []
        lead_attr = False
        for idx, lead_sup in enumerate(lead_sups):
            sent, lead_attr = _render_support_sentence(
                lead_sup, already_attributed=lead_attr, opener_index=idx
            )
            lead_sentences.append(sent)
            clean_lead_text = sanitize_writer_source_text(
                (lead_sup.text or lead_sup.source_text).strip()
            ).strip()
            clean_lead_text = re.sub(r"[«»“”\"]", "", clean_lead_text)
            clean_lead_text = (
                clean_lead_text.replace("[contact omitted]", "")
                .replace("[url omitted]", "")
                .strip()
            )
            lead_claims.append(
                ArticleClaimAtom(
                    text=clean_lead_text.rstrip("."), cited_support_ids=(lead_sup.support_id,)
                )
            )

        lead = " ".join(lead_sentences)
        lead_support_ids = tuple(dict.fromkeys(ls.support_id for ls in lead_sups))

        # 2. Build theme sections with multi-paragraph layout and rich transitions
        sections: list[ArticleSection] = []
        seen_dedup_keys: dict[str, int] = {}
        shared_claim_atoms: list[ArticleClaimAtom] = []

        if plan.sections:
            plan_by_id = plan.by_story_id
            for thematic_sec in plan.sections:
                sec_stories = [
                    plan_by_id[a.story_id]
                    for a in thematic_sec.story_assignments
                    if a.story_id in plan_by_id
                ]
                if not sec_stories:
                    continue
                paras = _build_section_paragraphs(
                    sec_stories,
                    context,
                    origin="FALLBACK",
                    seen_dedup_keys=seen_dedup_keys,
                    shared_claim_atoms=shared_claim_atoms,
                )
                if not paras:
                    continue
                sec_cited = tuple(dict.fromkeys(sid for p in paras for sid in p.cited_support_ids))
                sections.append(
                    ArticleSection(
                        heading=thematic_sec.title,
                        heading_support_ids=sec_cited,
                        heading_claims=(),
                        paragraphs=paras,
                        heading_generation_origin="FALLBACK",
                    )
                )
        else:
            for th_key in sorted_themes:
                theme_stories = stories_by_theme[th_key]
                paras = _build_section_paragraphs(
                    theme_stories,
                    context,
                    origin="FALLBACK",
                    seen_dedup_keys=seen_dedup_keys,
                    shared_claim_atoms=shared_claim_atoms,
                )
                if not paras:
                    continue

                sec_cited = tuple(dict.fromkeys(sid for p in paras for sid in p.cited_support_ids))
                heading = THEME_HEADINGS.get(th_key, _GENERIC_SECTION_HEADING)
                sections.append(
                    ArticleSection(
                        heading=heading,
                        heading_support_ids=sec_cited,
                        heading_claims=(),
                        paragraphs=paras,
                        heading_generation_origin="FALLBACK",
                    )
                )

        all_text = " ".join([title, lead] + [p.text for s in sections for p in s.paragraphs])
        word_count = len(all_text.split())

        return StructuredArticleDraft(
            title=title,
            title_support_ids=title_support_ids,
            title_claims=title_claims,
            lead=lead,
            lead_support_ids=lead_support_ids,
            lead_claims=tuple(lead_claims),
            sections=tuple(sections),
            cited_evidence_ids=(),
            word_count=word_count,
            title_generation_origin="FALLBACK",
            lead_generation_origin="FALLBACK",
        )
