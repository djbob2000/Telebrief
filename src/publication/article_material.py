"""Conservative writer-facing projection of article material.

The projection removes contact and promotional payload from the writer input while
leaving the immutable editorial context available to validation and provenance.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
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


def _is_high_confidence_promotion(text: str) -> bool:
    """Require multiple independent classified-ad cues and no useful fact."""
    if _has_useful_fact(text):
        return False
    cues = detect_classified_cues(text)
    return len(cues) >= 2


def _strip_directory_sentences(text: str) -> str:
    """Remove sentences that contain only contact, booking, or CTA payload."""
    parts = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text or "") if part.strip()]
    retained: list[str] = []
    for part in parts:
        if _CONTACT_OR_CTA_RE.search(part) and not _has_useful_fact(part):
            continue
        retained.append(part)
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
    for raw in candidates:
        trimmed = _strip_directory_sentences(raw)
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
    seen = {" ".join(part.split()).casefold() for part in result}

    def is_duplicate(sentence: str) -> bool:
        key = sentence.casefold()
        if key in seen:
            return True
        sentence_words = set(re.findall(r"[\wа-яё]+", key, re.IGNORECASE))
        if not sentence_words:
            return True
        for existing in result:
            existing_words = set(re.findall(r"[\wа-яё]+", existing.casefold(), re.IGNORECASE))
            overlap = len(sentence_words & existing_words) / max(
                1, min(len(sentence_words), len(existing_words))
            )
            if overlap >= 0.8 and abs(len(sentence_words) - len(existing_words)) <= 2:
                return True
        return False

    for candidate in cleaned_candidates[1:]:
        for sentence in _SENTENCE_SPLIT_RE.split(candidate):
            sentence = " ".join(sentence.split()).strip()
            key = sentence.casefold()
            if sentence and not is_duplicate(sentence):
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
        high_promotion_by_support[support.support_id] = _is_high_confidence_promotion(
            _combined_text(support)
        )

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
