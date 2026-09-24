"""Conservative writer-facing projection of article material.

The projection removes contact and promotional payload from the writer input while
leaving the immutable editorial context available to validation and provenance.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal

from src.processing.noise_detection import detect_classified_cues
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_writer_context import sanitize_writer_source_text

ArticleMaterialAction = Literal["KEEP", "TRIM_DIRECTORY", "SUPPRESS_PROMOTION_ONLY"]

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
_CONTACT_OR_CTA_RE = re.compile(
    r"(?:https?://\S+|\bwww\.\S+|\bt\.me/\S+|\+?\d[\d\s()\-–—]{8,}\d|"
    r"\b(?:звон(?:ите|ить)?|телефон|подробност|брониров|"
    r"запис(?:аться|ывайтесь|ываться)|запись\s+(?:по\s+(?:телефону|ссылке)|через)|"
    r"пишите|обращайт(?:есь|еся)|ссылка|личк|самовывоз|доставк|"
    r"qr-код|qr\s*код|сканируйт|приходите|жд[её]м\s+вас|скидк|акци)\w*)",
    re.IGNORECASE,
)
_USEFUL_FACT_RE = re.compile(
    r"(?:нет\s+(?:света|воды|связи)|отключ(?:ен|или|ение)|аварийн|"
    r"работа(?:ет|ют)|ограничен|расписан|маршрут|автобус|рейс|останов|"
    r"бесплатн|набор\s+(?:детей|в)|принима(?:ют|ет)|учебн(?:ый|ого)|"
    r"(?:школ\w*|секци\w*|круж\w*).*(?:набор|запис|бесплатн)|"
    r"(?:откры(?:лась|ли|т)|закры(?:лась|ли|т)).*(?:школ\w*|секци\w*|круж\w*))",
    re.IGNORECASE,
)
_OPERATIONAL_FACT_RE = re.compile(
    r"(?:\b(?:пункт\w*|подвоз\w*|выдач\w*|доставк\w*|подач\w*|"
    r"открыт\w*|закрыт\w*|работа(?:ет|ют)|доступ\w*|обслужив\w*|"
    r"принима(?:ют|ет)|график\w*|режим\w*|адрес\w*|улиц\w*|район\w*|"
    r"до\s+\d|с\s+\d|\d{1,2}:\d{2}|руб\w*|₽|литр\w*|бесплатн\w*|"
    r"восстанов\w*|ограничен\w*|нет\s+(?:света|воды|связи))|\bул\.)",
    re.IGNORECASE,
)
_OPERATIONAL_CLAUSE_SPLIT_RE = re.compile(
    r"\s*[,;]\s*|\s+(?=(?:звон(?:ите|ить)?|подробност\w*|"
    r"брониров\w*|запис(?:аться|ывайтесь|ываться)|"
    r"запись\s+(?:по\s+(?:телефону|ссылке)|через)|пишите|обращайт\w*|ссылка|личк\w*|"
    r"https?://\S+|www\.\S+|t\.me/\S+|\+?\d[\d\s()\-–—]{8,}\d|"
    r"qr[-\s]*код|сканируйт\w*|скидк\w*|акци\w*|приходите|жд[её]м\s+вас|"
    r"(?:и|а|но|однако)\s+(?:пункт\s+выдачи|постамат|магазин\w*|кафе|ресторан|"
    r"салон\w*|бутик\w*|отдел\w*|склад\w*)))",
    re.IGNORECASE,
)

_COMMERCIAL_PICKUP_DELIVERY_DIRECTORY_RE = re.compile(
    r"\b(?:пункт\w*\s+(?:выдачи|получения)\b|пвз\b|постамат\w*|"
    r"выдач\w*\s+(?:заказ\w*|посыл\w*|товар\w*)|"
    r"(?:получить|забрать)\s+(?:свой\s+)?заказ\w*)",
    re.IGNORECASE,
)
_COMMERCIAL_ESTABLISHMENT_RE = re.compile(
    r"\b(?:магазин\w*|кафе|ресторан\w*|салон\w*|бутик\w*|"
    r"торгов\w*\s+центр\w*|мастерск\w*|склад\w*|пункт\s+выдачи|"
    r"постамат\w*|офис\w*)\b",
    re.IGNORECASE,
)
_ROUTINE_HOURS_RE = re.compile(
    r"(?:\b(?:режим|график)\w*\s+работ\w*|"
    r"\b(?:работа\w*|открыт\w*)\b.{0,55}(?:с\s*)?\d{1,2}(?::\d{2})?"
    r"\s*(?:до|[-–—])\s*\d{1,2}(?::\d{2})?|"
    r"\b(?:ежедневно|каждый\s+день|без\s+выходных|"
    r"пн\.?\s*[-–—]\s*пт\.?|пн\.?\s*[-–—]\s*сб\.?)"
    r")",
    re.IGNORECASE,
)
_MEANINGFUL_SCHEDULE_CHANGE_RE = re.compile(
    r"(?:измен\w*.{0,45}(?:график|расписан|режим|часы)|"
    r"(?:график|расписан|режим|часы).{0,45}измен\w*|"
    r"(?:работа\w*|открыт\w*).{0,50}\d{1,2}(?::\d{2})?.{0,30}"
    r"вместо.{0,30}\d{1,2}(?::\d{2})?|"
    r"сократ\w*.{0,35}(?:график|расписан|часы|рейс|маршрут)|"
    r"временно\s+(?:закрыт\w*|не\s+работ\w*|приостанов\w*)|"
    r"(?:сегодня|завтра).{0,40}(?:закрыт\w*|не\s+работ\w*|перенес\w*|отмен\w*)|"
    r"(?:отмен\w*|перенес\w*|приостанов\w*|возобнов\w*).{0,50}"
    r"(?:рейс\w*|маршрут\w*|работ\w*|движен\w*|при[её]м\w*)|"
    r"не\s+буд\w*\s+работ\w*|нов\w*\s+расписан\w*|по\s+измен[её]нн\w*\s+график\w*)",
    re.IGNORECASE,
)
_PUBLIC_AID_ACCESS_RE = re.compile(
    r"(?:\b(?:организован\w*|открыт\w*|создан\w*|работа\w*|действу\w*|появил\w*)"
    r".{0,45}\bпункт\w*\s+выдач\w*.{0,45}\bгуманитарн\w*\s+помощ\w*|"
    r"\bпункт\w*\s+выдач\w*.{0,45}\bгуманитарн\w*\s+помощ\w*.{0,45}"
    r"\b(?:организован\w*|открыт\w*|работа\w*|действу\w*|доступ\w*)|"
    r"\b(?:можно|могут|сможет|сможут)\s+(?:бесплатно\s+)?получ\w*.{0,25}"
    r"\bгуманитарн\w*\s+помощ\w*|"
    r"\bгуманитарн\w*\s+помощ\w*.{0,25}"
    r"\b(?:можно|могут|сможет|сможут)\s+получ\w*)",
    re.IGNORECASE,
)
_PUBLIC_AID_QUESTION_RE = re.compile(
    r"\b(?:где|когда|как|кому|кто|можно\s+ли|подскажите|скажите)\b"
    r".{0,65}\b(?:получ\w*|гуманитарн\w*\s+помощ\w*)",
    re.IGNORECASE,
)
_ESSENTIAL_OR_PUBLIC_ACCESS_RE = re.compile(
    r"(?:нет\s+(?:света|электричеств\w*|воды|водоснабжен\w*|отоплен\w*|"
    r"газа|связи|интернет\w*)|(?:отключ\w*|аварийн\w*|восстанов\w*).{0,55}"
    r"(?:свет\w*|электричеств\w*|вод\w*|отоплен\w*|газ\w*|связ\w*)|"
    r"(?:подвоз|раздач|выдач|точк\w*\s+водоразбор\w*).{0,55}"
    r"(?:питьев\w*\s+вод\w*|вод\w*.{0,15}населен\w*)|"
    r"(?:питьев\w*\s+вод\w*|водоснабжен\w*).{0,55}(?:подвоз|раздач|выдач)|"
    r"вод\w*\s+на\s+розлив|"
    r"(?:муниципальн\w*|городск\w*|коммунальн\w*).{0,55}"
    r"(?:пункт\w*|служб\w*|доступ\w*|подвоз\w*|ограничен\w*)|"
    r"(?:(?:огранич\w*|перекры\w*|закры\w*|запрещ\w*).{0,55}"
    r"(?:проезд\w*|движен\w*|доступ\w*|вход\w*|проход\w*)|"
    r"(?:проезд\w*|движен\w*|доступ\w*|вход\w*|проход\w*|улиц\w*|дорог\w*).{0,55}"
    r"(?:огранич\w*|перекры\w*|закры\w*|запрещ\w*))|"
    r"(?:автобус\w*|маршрут\w*|общественн\w*\s+транспорт\w*|рейс\w*)"
    r".{0,65}(?:не\s+ход\w*|не\s+буд\w*|отмен\w*|задерж\w*|измен\w*|"
    r"расписан\w*|интервал\w*)|"
    r"(?:не\s+ход\w*|отмен\w*|задерж\w*|измен\w*|расписан\w*|интервал\w*)"
    r".{0,65}(?:автобус\w*|маршрут\w*|общественн\w*\s+транспорт\w*|рейс\w*))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ArticleMaterialProjection:
    """Immutable writer-facing text and auditable projection decisions."""

    text_by_support_id: Mapping[str, str]
    actions_by_support_id: Mapping[str, ArticleMaterialAction]
    reasons_by_support_id: Mapping[str, str]
    suppressed_story_ids: tuple[str, ...] = ()
    trimmed_support_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "text_by_support_id", MappingProxyType(dict(self.text_by_support_id))
        )
        object.__setattr__(
            self,
            "actions_by_support_id",
            MappingProxyType(dict(self.actions_by_support_id)),
        )
        object.__setattr__(
            self,
            "reasons_by_support_id",
            MappingProxyType(dict(self.reasons_by_support_id)),
        )
        object.__setattr__(self, "suppressed_story_ids", tuple(self.suppressed_story_ids))
        object.__setattr__(self, "trimmed_support_ids", tuple(self.trimmed_support_ids))

    def to_metadata(self) -> dict[str, object]:
        """Return stable IDs and reason codes without source or contact payload."""
        return {
            "actions_by_support_id": dict(self.actions_by_support_id),
            "reasons_by_support_id": dict(self.reasons_by_support_id),
            "suppressed_story_ids": list(self.suppressed_story_ids),
            "trimmed_support_ids": list(self.trimmed_support_ids),
            "suppressed_story_count": len(self.suppressed_story_ids),
            "trimmed_support_count": len(self.trimmed_support_ids),
        }


def _combined_text(support: ArticleSupport) -> str:
    return " ".join(
        part.strip() for part in (support.text, support.source_text) if part and part.strip()
    )


def _has_useful_fact(text: str) -> bool:
    """Find an editorial fact without letting routine store hours mask it."""
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text or "") if part.strip()]
    for part in parts:
        for clause in _OPERATIONAL_CLAUSE_SPLIT_RE.split(part):
            clause = re.sub(r"^\s*(?:а|и|но|однако)\s+", "", clause, flags=re.IGNORECASE)
            if not clause or _is_commercial_directory_clause(clause):
                continue
            if _USEFUL_FACT_RE.search(clause):
                return True
    return False


def _is_commercial_pickup_delivery_directory(text: str) -> bool:
    return bool(_COMMERCIAL_PICKUP_DELIVERY_DIRECTORY_RE.search(text))


def _has_declarative_public_aid_access(text: str) -> bool:
    """Recognize stated aid distribution/access, excluding questions and ads."""
    # A public-aid phrase inside a classified sale is not evidence of a public
    # distribution point. Check commercial cues across the support so an ad
    # cue in a neighboring sentence cannot borrow the access wording as an
    # exemption. The phone-number cue alone is directory payload, not evidence
    # that the aid itself is being sold; projection strips the contact below.
    commercial_cues = [cue for cue in detect_classified_cues(text) if cue != "cue_4"]
    if commercial_cues:
        return False

    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        sentence = sentence.strip()
        if not sentence or "?" in sentence or _PUBLIC_AID_QUESTION_RE.search(sentence):
            continue
        if _PUBLIC_AID_ACCESS_RE.search(sentence):
            return True
    return False


def _has_essential_or_public_resident_access(text: str) -> bool:
    return bool(
        _ESSENTIAL_OR_PUBLIC_ACCESS_RE.search(text)
        or _MEANINGFUL_SCHEDULE_CHANGE_RE.search(text)
        or _has_declarative_public_aid_access(text)
    )


def _has_useful_fact_embedded_alongside_directory_copy(text: str) -> bool:
    """Recognize editorial facts in a support that also contains directory copy."""
    if not _has_commercial_directory_payload(text):
        return _has_essential_or_public_resident_access(text) or _has_useful_fact(text)
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    for part in parts:
        if _has_essential_or_public_resident_access(part):
            return True
        for clause in _OPERATIONAL_CLAUSE_SPLIT_RE.split(part):
            clause = re.sub(r"^\s*(?:а|и|но|однако)\s+", "", clause, flags=re.IGNORECASE)
            if not clause or _is_commercial_directory_clause(clause):
                continue
            if _has_essential_or_public_resident_access(clause) or _has_useful_fact(clause):
                return True
    return False


def _is_commercial_directory_clause(text: str) -> bool:
    if _is_commercial_pickup_delivery_directory(text):
        return True
    return bool(
        _COMMERCIAL_ESTABLISHMENT_RE.search(text)
        and _ROUTINE_HOURS_RE.search(text)
        and not _MEANINGFUL_SCHEDULE_CHANGE_RE.search(text)
    )


def _has_commercial_directory_payload(text: str) -> bool:
    return _is_commercial_pickup_delivery_directory(text) or _is_commercial_directory_clause(text)


def _is_high_confidence_promotion(support: ArticleSupport) -> bool:
    """Suppress commercial directory/promotional text without civic consequence."""
    text = _combined_text(support)
    if _has_useful_fact_embedded_alongside_directory_copy(text):
        return False
    if _has_commercial_directory_payload(text):
        return True
    cues = detect_classified_cues(text)
    return len(cues) >= 2 and not _has_essential_or_public_resident_access(text)


def _strip_directory_sentences(text: str, *, preserve_public_access: bool = False) -> str:
    """Remove commercial/contact clauses while retaining civic facts nearby."""
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text or "") if part.strip()]
    retained: list[str] = []
    for part in parts:
        if not (
            _CONTACT_OR_CTA_RE.search(part)
            or _is_commercial_directory_clause(part)
            or _is_commercial_pickup_delivery_directory(part)
        ):
            retained.append(part)
            continue
        useful_clauses: list[str] = []
        for raw_clause in _OPERATIONAL_CLAUSE_SPLIT_RE.split(part):
            clause = re.sub(r"^\s*(?:а|и|но|однако)\s+", "", raw_clause, flags=re.IGNORECASE)
            clause = re.sub(
                r"^(?:адрес|телефон|режим\s+работы|график\s+работы)\s*:\s*",
                "",
                clause,
                flags=re.IGNORECASE,
            )
            clause = clause.strip(" ,;:")
            if not clause:
                continue

            public_access = _has_essential_or_public_resident_access(clause)
            if _is_commercial_directory_clause(clause) and not public_access:
                continue
            if _CONTACT_OR_CTA_RE.search(clause) and not (
                public_access
                or (_has_useful_fact(clause) and not _is_commercial_directory_clause(clause))
            ):
                continue
            if preserve_public_access and _OPERATIONAL_FACT_RE.search(clause):
                useful_clauses.append(clause)
            elif not _CONTACT_OR_CTA_RE.search(clause):
                useful_clauses.append(clause)
        if useful_clauses:
            retained.append(", ".join(useful_clauses))
    return " ".join(retained).strip()


def _project_support_text(support: ArticleSupport) -> tuple[str, bool]:
    """Keep citable text plus non-duplicate useful source detail."""
    candidates = [
        value.strip() for value in (support.text, support.source_text) if value and value.strip()
    ]
    if not candidates:
        return "", False

    cleaned_candidates: list[str] = []
    payload_changed = False
    combined = _combined_text(support)
    preserve_public_access = _has_essential_or_public_resident_access(combined)
    for raw in candidates:
        trimmed = _strip_directory_sentences(raw, preserve_public_access=preserve_public_access)
        sanitized = sanitize_writer_source_text(trimmed)
        payload_changed = payload_changed or sanitized != raw
        cleaned = " ".join(sanitized.split()).strip()
        if cleaned:
            cleaned_candidates.append(cleaned)

    if not cleaned_candidates:
        return "", payload_changed

    primary = cleaned_candidates[0]
    primary_sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(primary) if part.strip()]
    result = list(primary_sentences)

    def normalized_sentence_key(sentence: str) -> tuple[str, ...]:
        """Normalize punctuation and spacing without changing word order or meaning."""
        return tuple(re.findall(r"[\wа-яё]+", sentence.casefold(), re.IGNORECASE))

    seen = {normalized_sentence_key(part) for part in result}

    for candidate in cleaned_candidates[1:]:
        for sentence in _SENTENCE_SPLIT_RE.split(candidate):
            sentence = " ".join(sentence.split()).strip()
            key = normalized_sentence_key(sentence)
            if not sentence:
                continue
            if not key:
                continue
            if key in seen:
                continue
            # Near-duplicates are deliberately retained: a token-set comparison
            # can erase scope, time, purpose, negation, or a change in word order.
            result.append(sentence)
            seen.add(key)
    return " ".join(result).strip(), payload_changed


def project_article_material(context: ArticleEditorialContext) -> ArticleMaterialProjection:
    """Project article supports for the writer while preserving source evidence."""
    supports = tuple(context.support_index)
    publishable_by_story: dict[str, list[ArticleSupport]] = {}
    high_promotion_by_support: dict[str, bool] = {}
    for support in supports:
        if support.publication_use != "PUBLISH":
            continue
        if support.story_id:
            publishable_by_story.setdefault(support.story_id, []).append(support)
        high_promotion_by_support[support.support_id] = _is_high_confidence_promotion(support)

    suppressed_stories = tuple(
        sorted(
            story_id
            for story_id, story_supports in publishable_by_story.items()
            if story_supports
            and all(high_promotion_by_support.get(s.support_id, False) for s in story_supports)
        )
    )
    suppressed_set = set(suppressed_stories)

    text_by_id: dict[str, str] = {}
    actions: dict[str, ArticleMaterialAction] = {}
    reasons: dict[str, str] = {}
    trimmed_ids: list[str] = []
    for support in supports:
        projected_text, changed = _project_support_text(support)
        text_by_id[support.support_id] = projected_text
        if support.story_id in suppressed_set and support.publication_use == "PUBLISH":
            actions[support.support_id] = "SUPPRESS_PROMOTION_ONLY"
            reasons[support.support_id] = (
                "commercial_directory_only_removed"
                if _has_commercial_directory_payload(_combined_text(support))
                else "high_confidence_promotion_only"
            )
            continue
        if changed:
            actions[support.support_id] = "TRIM_DIRECTORY"
            reasons[support.support_id] = (
                "commercial_directory_payload_removed"
                if _has_commercial_directory_payload(_combined_text(support))
                else "contact_or_directory_payload_removed"
            )
            trimmed_ids.append(support.support_id)
        else:
            actions[support.support_id] = "KEEP"
            reasons[support.support_id] = "supported_material_retained"

    return ArticleMaterialProjection(
        text_by_support_id=text_by_id,
        actions_by_support_id=actions,
        reasons_by_support_id=reasons,
        suppressed_story_ids=suppressed_stories,
        trimmed_support_ids=tuple(trimmed_ids),
    )


def materialize_article_validation_context(
    context: ArticleEditorialContext,
    projection: ArticleMaterialProjection,
) -> ArticleEditorialContext:
    """Build the projected support view used for strict claim validation.

    The original context remains the provenance source for traces and quote
    allowlists.  Validation itself must see only citable projected text, so a
    suppressed support or a support whose projection became empty cannot be
    reattached by an editor patch or a deterministic repair.
    """
    projected_supports: list[ArticleSupport] = []
    suppressed_story_ids = set(projection.suppressed_story_ids)
    for support in context.support_index:
        if support.publication_use == "EXCLUDE":
            continue
        support_story_id = support.story_id
        if not support_story_id:
            match = re.match(r"(story:[^:]+)", support.support_id)
            support_story_id = match.group(1) if match else ""
        if support_story_id in suppressed_story_ids:
            continue
        if projection.actions_by_support_id.get(support.support_id) == "SUPPRESS_PROMOTION_ONLY":
            continue
        projected_text = projection.text_by_support_id.get(support.support_id, "").strip()
        if not projected_text:
            continue
        projected_supports.append(replace(support, text=projected_text, source_text=projected_text))

    support_index = tuple(projected_supports)
    return replace(
        context,
        support_index=support_index,
        support_by_id={support.support_id: support for support in support_index},
    )
