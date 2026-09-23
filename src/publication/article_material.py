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
    r"\b(?:звон(?:ите|ить)?|телефон|подробност|брониров|запис(?:ь|аться)|"
    r"пишите|обращайт(?:есь|еся)|ссылка|личк|самовывоз|доставк)\w*)",
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
    r"\s*[,;—–]\s*|\s+(?=(?:звон(?:ите|ить)?|подробност\w*|"
    r"брониров\w*|запис(?:ь|аться)|пишите|обращайт\w*|ссылка|личк\w*))",
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
    return bool(_USEFUL_FACT_RE.search(text))


def _is_high_confidence_promotion(support: ArticleSupport) -> bool:
    """Require multiple independent classified-ad cues and no useful fact."""
    if support.evidence_kind == "service_access" or support.support_kind == "operational":
        return False
    text = _combined_text(support)
    if _has_useful_fact(text):
        return False
    cues = detect_classified_cues(text)
    return len(cues) >= 2


def _strip_directory_sentences(text: str, *, preserve_operational: bool = False) -> str:
    """Remove sentences that contain only contact, booking, or CTA payload."""
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text or "") if part.strip()]
    retained: list[str] = []
    for part in parts:
        if not _CONTACT_OR_CTA_RE.search(part):
            retained.append(part)
            continue
        if _has_useful_fact(part) and not preserve_operational:
            retained.append(part)
            continue
        if preserve_operational:
            useful_clauses = [
                clause
                for clause in _OPERATIONAL_CLAUSE_SPLIT_RE.split(part)
                if clause.strip()
                and (_OPERATIONAL_FACT_RE.search(clause) or not _CONTACT_OR_CTA_RE.search(clause))
            ]
            if useful_clauses:
                retained.append(", ".join(clause.strip() for clause in useful_clauses))
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
    preserve_operational = (
        support.evidence_kind == "service_access" or support.support_kind == "operational"
    )
    for raw in candidates:
        trimmed = _strip_directory_sentences(raw, preserve_operational=preserve_operational)
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
            reasons[support.support_id] = "high_confidence_promotion_only"
            continue
        if changed:
            actions[support.support_id] = "TRIM_DIRECTORY"
            reasons[support.support_id] = "contact_or_directory_payload_removed"
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
