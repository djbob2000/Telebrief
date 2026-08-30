from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from src.editorial_models import StoryCard
from src.publication.article_context import ArticleEditorialContext, ArticleSupport

_STORY_ID_RE = re.compile(r"story:(?:[^:]+|\d+)")

ArticleProminence = Literal["DEVELOP", "WEAVE", "BRIEF"]


@dataclass(frozen=True)
class ArticleStoryCoverage:
    story_id: str
    topic: str
    rank: int
    prominence: ArticleProminence
    support_ids: tuple[str, ...]
    detail_support_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArticleCoveragePlan:
    stories: tuple[ArticleStoryCoverage, ...]

    @property
    def story_ids(self) -> tuple[str, ...]:
        return tuple(item.story_id for item in self.stories)


def _story_id_from_support_id(support_id: str) -> str:
    match = _STORY_ID_RE.search(support_id)
    return match.group(0) if match else ""


def _publish_supports_by_story(
    context: ArticleEditorialContext,
) -> dict[str, tuple[ArticleSupport, ...]]:
    grouped: dict[str, list[ArticleSupport]] = defaultdict(list)
    for support in context.support_index:
        if support.publication_use != "PUBLISH":
            continue
        if support.evidence_kind == "resident_question":
            continue
        story_id = support.story_id or _story_id_from_support_id(support.support_id)
        if not story_id:
            continue
        grouped[story_id].append(support)
    return {story_id: tuple(items) for story_id, items in grouped.items()}


def _prominence(card: StoryCard, support_count: int) -> ArticleProminence:
    if card.importance == "high" or support_count >= 4:
        return "DEVELOP"
    if support_count >= 2:
        return "WEAVE"
    return "BRIEF"


def build_article_coverage_plan(
    cards: Sequence[StoryCard],
    context: ArticleEditorialContext,
) -> ArticleCoveragePlan:
    support_map = _publish_supports_by_story(context)
    stories: list[ArticleStoryCoverage] = []
    for rank, card in enumerate(cards, start=1):
        supports = support_map.get(card.id, ())
        if not supports:
            continue
        stories.append(
            ArticleStoryCoverage(
                story_id=card.id,
                topic=card.topic or card.summary or card.id,
                rank=rank,
                prominence=_prominence(card, len(supports)),
                support_ids=tuple(s.support_id for s in supports),
            )
        )
    return ArticleCoveragePlan(stories=tuple(stories))
