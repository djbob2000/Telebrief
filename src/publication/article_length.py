"""Adaptive length profiling and soft target derivation for Event-First articles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from src.config_loader import PublicationEditorialConfig
from src.publication.article_context import ArticleEditorialContext

_STORY_ID_RE = re.compile(r"story:(\d+)")


def _extract_story_id(support_id: str) -> str:
    m = _STORY_ID_RE.search(support_id)
    return m.group(0) if m else support_id


@dataclass(frozen=True)
class ArticleLengthProfile:
    """Adaptive length profile for long-form article synthesis."""

    richness: Literal["thin", "standard", "rich"]
    target_min_words: int
    target_max_words: int
    target_min_sections: int
    target_max_sections: int
    hard_min_words: int
    hard_max_words: int


def derive_article_length_profile(
    context: ArticleEditorialContext,
    config: PublicationEditorialConfig,
) -> ArticleLengthProfile:
    """Derive deterministic article richness bucket and soft editorial targets."""
    supports = context.supports if hasattr(context, "supports") else context.support_index
    publish_supports = [s for s in supports if s.publication_use == "PUBLISH"]

    publish_story_ids = {
        _extract_story_id(s.support_id) for s in publish_supports if _extract_story_id(s.support_id)
    }
    publish_story_count = len(publish_story_ids)
    publish_support_count = len(publish_supports)

    is_thin = publish_story_count <= 4 or publish_support_count <= 8
    is_standard = not is_thin and (publish_story_count <= 10 or publish_support_count <= 24)

    hard_min = 180
    hard_max = config.article_max_words

    if is_thin:
        return ArticleLengthProfile(
            richness="thin",
            target_min_words=300,
            target_max_words=700,
            target_min_sections=2,
            target_max_sections=3,
            hard_min_words=hard_min,
            hard_max_words=hard_max,
        )
    elif is_standard:
        return ArticleLengthProfile(
            richness="standard",
            target_min_words=500,
            target_max_words=1100,
            target_min_sections=2,
            target_max_sections=4,
            hard_min_words=hard_min,
            hard_max_words=hard_max,
        )
    else:
        return ArticleLengthProfile(
            richness="rich",
            target_min_words=800,
            target_max_words=min(1400, hard_max),
            target_min_sections=3,
            target_max_sections=min(5, config.article_max_sections),
            hard_min_words=hard_min,
            hard_max_words=hard_max,
        )
