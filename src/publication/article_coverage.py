from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from src.editorial_models import StoryCard
from src.publication.article_claims import extract_concrete_claims
from src.publication.article_context import ArticleEditorialContext, ArticleSupport

_STORY_ID_RE = re.compile(r"story:(?:[^:]+|\d+)")

ArticleProminence = Literal["DEVELOP", "WEAVE", "BRIEF"]

_DETAIL_LIMIT: dict[ArticleProminence, int] = {
    "DEVELOP": 3,
    "WEAVE": 2,
    "BRIEF": 1,
}


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


def score_detail_support(support: ArticleSupport) -> int:
    text = " ".join(part for part in (support.text, support.source_text) if part).strip()
    if not text:
        return 0

    score = 0
    claims = extract_concrete_claims(text)
    if claims:
        score += 3
    if support.evidence_kind in {
        "community_report",
        "service_access",
        "operational_observation",
    }:
        score += 2
    if len(support.source_text.split()) >= 8:
        score += 1
    if any(marker in support.source_text for marker in ("«", "»", '"')):
        score += 1
    return score


def _detail_support_ids(
    supports: tuple[ArticleSupport, ...],
    prominence: ArticleProminence,
) -> tuple[str, ...]:
    ranked = sorted(
        supports,
        key=lambda s: (-score_detail_support(s), s.support_id),
    )
    positive = [s for s in ranked if score_detail_support(s) > 0]
    chosen = positive or list(ranked)
    return tuple(s.support_id for s in chosen[: _DETAIL_LIMIT[prominence]])


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
        prominence = _prominence(card, len(supports))
        stories.append(
            ArticleStoryCoverage(
                story_id=card.id,
                topic=card.topic or card.summary or card.id,
                rank=rank,
                prominence=prominence,
                support_ids=tuple(s.support_id for s in supports),
                detail_support_ids=_detail_support_ids(supports, prominence),
            )
        )
    return ArticleCoveragePlan(stories=tuple(stories))
