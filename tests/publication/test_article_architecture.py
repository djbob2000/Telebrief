from __future__ import annotations

import datetime as dt

import pytest

from src.editorial_models import StoryCard
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.article_coverage import build_article_coverage_plan
from src.publication.article_writer_context import (
    ARTICLE_WRITER_CONTEXT_MAX_CHARS,
    render_article_writer_context,
)
from src.publication.models import PublicationCandidate, PublicationRun
from src.publication.selection_ai import FailOpenSelectionModel, HeuristicSelectionModel

pytestmark = pytest.mark.unit

_NOW = dt.datetime(2026, 9, 16, 18, 0, tzinfo=dt.timezone.utc)


class _UnexpectedPrimary:
    def __init__(self) -> None:
        self.calls = 0

    async def select_stories(self, *, run, candidates):
        self.calls += 1
        raise AssertionError("large article selection must not call the AI selector")


def test_large_article_selection_uses_bounded_deterministic_priority_overlay():
    candidates = [
        PublicationCandidate(
            id=index,
            publication_run_id=1,
            story_id=index,
            story_revision_id=index,
            deterministic_rank=index,
            snapshot_features={},
            created_at=_NOW,
        )
        for index in range(1, 251)
    ]
    run = PublicationRun(
        id=1,
        edition_id=1,
        publication_type="article",
        request_key="large-article",
        snapshot_at=_NOW,
        eligibility_policy_id=1,
        selection_policy_id=1,
        writer_policy_id=1,
        status="candidates_sealed",
        error_kind=None,
        metadata={},
        created_at=_NOW,
    )
    primary = _UnexpectedPrimary()

    selector = FailOpenSelectionModel(
        primary=primary,
        fallback=HeuristicSelectionModel(),
    )

    import asyncio

    proposals = asyncio.run(selector.select_stories(run=run, candidates=candidates))

    assert primary.calls == 0
    assert len(proposals) == len(candidates)
    assert all(proposal.decision == "INCLUDE" for proposal in proposals)


def test_article_writer_materialization_is_bounded_without_losing_story_packets():
    cards = [
        StoryCard(
            id=f"story:{index}",
            topic=f"Городская история {index}: конкретное изменение",
            summary=f"Жители сообщили о событии {index} в городе",
            importance="medium",
        )
        for index in range(250)
    ]
    supports = tuple(
        ArticleSupport(
            support_id=f"story:{index}:evidence:0:frag:{index}",
            text=(
                f"Сюжет {index}: жители сообщили о конкретном событии в городе, "
                "его месте, времени и практическом последствии для жителей."
            ),
            source_text=(
                f"Первичное сообщение для сюжета {index}. "
                "Подробности наблюдения и контекст события. " * 12
            ),
            support_kind="evidence",
            publication_use="PUBLISH",
            source_refs=(f"ref-{index}",),
            fragment_ids=(index,),
            source_item_ids=(index,),
            observed_at=_NOW,
            evidence_kind="community_report",
            story_id=f"story:{index}",
        )
        for index in range(250)
    )
    context = ArticleEditorialContext(
        headline_candidates=tuple(card.topic for card in cards),
        support_index=supports,
        support_by_id={support.support_id: support for support in supports},
        recurring_topics=(),
        edition_name="Бердянск",
    )
    plan = build_article_coverage_plan(cards, context)

    rendered = render_article_writer_context(
        context,
        plan,
        include_coverage_plan=False,
    )

    assert len(rendered) <= ARTICLE_WRITER_CONTEXT_MAX_CHARS
    assert rendered.count("[ARTICLE STORY PACKET") == len(plan.stories)
    for index in range(250):
        assert f"story:{index}" in rendered
