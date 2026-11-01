# ruff: noqa: S101, S311
"""The in-memory centroid cache ranks candidates exactly like a fresh database search."""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass

import pytest

from src.processing.event_clustering import CandidateCentroidCache
from src.repositories.event_clusters import CandidateMetadata

EDITION = 1
MODEL = "test-model"
DIMENSIONS = 16
T0 = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.UTC)


def _unit(rng: random.Random) -> list[float]:
    raw = [rng.gauss(0, 1) for _ in range(DIMENSIONS)]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


@dataclass
class _StoredCentroid:
    vector: list[float]
    updated_at: dt.datetime
    last_seen_at: dt.datetime
    fragment_count: int


class _FakeClusterRepo:
    """Stands in for EventClusterRepository with the same filters and versioning."""

    def __init__(self) -> None:
        self.rows: dict[int, _StoredCentroid] = {}
        self.centroid_loads: list[list[int]] = []
        self._clock = T0

    def put(self, story_id: int, vector: list[float], last_seen_at: dt.datetime) -> None:
        self._clock += dt.timedelta(seconds=1)
        self.rows[story_id] = _StoredCentroid(
            vector=vector,
            updated_at=self._clock,
            last_seen_at=last_seen_at,
            fragment_count=2,
        )

    async def list_candidate_metadata(
        self, conn, edition_id, *, model, dimensions, active_since
    ) -> list[CandidateMetadata]:
        assert (edition_id, model, dimensions) == (EDITION, MODEL, DIMENSIONS)
        return [
            CandidateMetadata(
                story_id=story_id,
                updated_at=row.updated_at,
                fragment_count=row.fragment_count,
                last_seen_at=row.last_seen_at,
            )
            for story_id, row in self.rows.items()
            if row.last_seen_at >= active_since
        ]

    async def load_centroids(self, conn, story_ids):
        self.centroid_loads.append(sorted(story_ids))
        return {
            story_id: (self.rows[story_id].updated_at, list(self.rows[story_id].vector))
            for story_id in story_ids
            if story_id in self.rows
        }


def _brute_force(repo: _FakeClusterRepo, query: list[float], active_since, limit: int):
    scored = []
    for story_id, row in repo.rows.items():
        if row.last_seen_at < active_since:
            continue
        dot = sum(a * b for a, b in zip(query, row.vector, strict=True))
        scored.append((-dot, story_id))
    scored.sort()
    return [(story_id, -neg) for neg, story_id in scored[:limit]]


@pytest.mark.asyncio
async def test_cache_ranking_matches_brute_force_search():
    rng = random.Random(7)
    repo = _FakeClusterRepo()
    for story_id in range(1, 60):
        repo.put(story_id, _unit(rng), T0 - dt.timedelta(hours=rng.randint(0, 100)))
    cache = CandidateCentroidCache(repo)  # type: ignore[arg-type]
    active_since = T0 - dt.timedelta(hours=72)
    query = _unit(rng)

    found = await cache.find_candidates(
        None,
        EDITION,
        query,
        model=MODEL,
        dimensions=DIMENSIONS,
        active_since=active_since,
        limit=5,
    )

    expected = _brute_force(repo, query, active_since, 5)
    assert [c.story_id for c in found] == [story_id for story_id, _ in expected]
    for candidate, (_, similarity) in zip(found, expected, strict=True):
        assert candidate.similarity == pytest.approx(similarity, abs=1e-6)


@pytest.mark.asyncio
async def test_unchanged_centroids_are_not_read_again():
    rng = random.Random(3)
    repo = _FakeClusterRepo()
    for story_id in range(1, 10):
        repo.put(story_id, _unit(rng), T0)
    cache = CandidateCentroidCache(repo)  # type: ignore[arg-type]
    kwargs = {
        "model": MODEL,
        "dimensions": DIMENSIONS,
        "active_since": T0 - dt.timedelta(hours=72),
        "limit": 3,
    }

    await cache.find_candidates(None, EDITION, _unit(rng), **kwargs)
    await cache.find_candidates(None, EDITION, _unit(rng), **kwargs)

    assert repo.centroid_loads == [list(range(1, 10))]


@pytest.mark.asyncio
async def test_changed_centroid_is_reloaded_and_reranked():
    rng = random.Random(11)
    repo = _FakeClusterRepo()
    for story_id in range(1, 6):
        repo.put(story_id, _unit(rng), T0)
    cache = CandidateCentroidCache(repo)  # type: ignore[arg-type]
    kwargs = {
        "model": MODEL,
        "dimensions": DIMENSIONS,
        "active_since": T0 - dt.timedelta(hours=72),
        "limit": 5,
    }
    query = _unit(rng)
    await cache.find_candidates(None, EDITION, query, **kwargs)

    # A concurrent join moved story 4 onto the query direction.
    repo.put(4, list(query), T0)
    repo.centroid_loads.clear()
    found = await cache.find_candidates(None, EDITION, query, **kwargs)

    assert repo.centroid_loads == [[4]]
    assert found[0].story_id == 4
    assert found[0].similarity == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_stories_outside_the_window_leave_the_cache():
    rng = random.Random(5)
    repo = _FakeClusterRepo()
    repo.put(1, _unit(rng), T0)
    repo.put(2, _unit(rng), T0 - dt.timedelta(hours=200))
    cache = CandidateCentroidCache(repo)  # type: ignore[arg-type]
    kwargs = {
        "model": MODEL,
        "dimensions": DIMENSIONS,
        "active_since": T0 - dt.timedelta(hours=72),
        "limit": 5,
    }

    found = await cache.find_candidates(None, EDITION, _unit(rng), **kwargs)

    assert [c.story_id for c in found] == [1]
    assert set(cache._centroids) == {1}


@pytest.mark.asyncio
async def test_zero_query_vector_scores_zero_instead_of_failing():
    rng = random.Random(2)
    repo = _FakeClusterRepo()
    repo.put(1, _unit(rng), T0)
    cache = CandidateCentroidCache(repo)  # type: ignore[arg-type]

    found = await cache.find_candidates(
        None,
        EDITION,
        [0.0] * DIMENSIONS,
        model=MODEL,
        dimensions=DIMENSIONS,
        active_since=T0 - dt.timedelta(hours=72),
        limit=1,
    )

    assert found[0].similarity == 0.0
