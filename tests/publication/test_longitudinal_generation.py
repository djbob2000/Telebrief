import datetime as dt

import pytest

from src.publication.generation import ARTICLE_PUBLICATION_TYPES as GEN_ARTICLE_PUBLICATION_TYPES
from src.publication.models import PublicationCandidate, PublicationRun
from src.publication.policies import ARTICLE_PUBLICATION_TYPES
from src.publication.selection import HeuristicSelectionModel

pytestmark = pytest.mark.unit


def _make_run(pub_type: str) -> PublicationRun:
    now = dt.datetime.now(dt.timezone.utc)
    return PublicationRun(
        id=1,
        edition_id=1,
        publication_type=pub_type,
        request_key="test-key",
        snapshot_at=now,
        eligibility_policy_id=1,
        selection_policy_id=1,
        writer_policy_id=1,
        status="candidates_sealed",
        error_kind=None,
        metadata={},
        created_at=now,
        completed_at=None,
    )


def test_article_publication_types_inclusion():
    assert "weekly_article" in ARTICLE_PUBLICATION_TYPES
    assert "monthly_article" in ARTICLE_PUBLICATION_TYPES
    assert "daily_article" in ARTICLE_PUBLICATION_TYPES
    assert "article" in ARTICLE_PUBLICATION_TYPES
    assert GEN_ARTICLE_PUBLICATION_TYPES == ARTICLE_PUBLICATION_TYPES


@pytest.mark.asyncio
async def test_selection_preserves_candidates_for_weekly_article():
    model = HeuristicSelectionModel()
    now = dt.datetime.now(dt.timezone.utc)
    candidates = [
        PublicationCandidate(
            id=1,
            publication_run_id=1,
            story_id=1,
            story_revision_id=10,
            deterministic_rank=1,
            snapshot_features={"lead_summary": "Ремонт напорного коллектора"},
            created_at=now,
        ),
        PublicationCandidate(
            id=2,
            publication_run_id=1,
            story_id=2,
            story_revision_id=20,
            deterministic_rank=2,
            snapshot_features={"lead_summary": "График движения автобуса №4"},
            created_at=now,
        ),
    ]
    run = _make_run("weekly_article")

    proposals = await model.select_stories(run=run, candidates=candidates)
    assert len(proposals) == 2
    for p in proposals:
        assert p.decision == "INCLUDE"
        assert p.presentation_intent in ("lead", "normal", "brief")


@pytest.mark.asyncio
async def test_selection_preserves_candidates_for_monthly_article():
    model = HeuristicSelectionModel()
    now = dt.datetime.now(dt.timezone.utc)
    candidates = [
        PublicationCandidate(
            id=1,
            publication_run_id=2,
            story_id=1,
            story_revision_id=10,
            deterministic_rank=1,
            snapshot_features={"lead_summary": "Системные изменения коммунального хозяйства"},
            created_at=now,
        ),
    ]
    run = _make_run("monthly_article")

    proposals = await model.select_stories(run=run, candidates=candidates)
    assert len(proposals) == 1
    assert proposals[0].decision == "INCLUDE"
