"""A batch candidate index must assign fragments exactly as per-fragment search does."""

# ruff: noqa: S101, S311
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import math
import random
from dataclasses import dataclass, field
from typing import Any

from src.domain.event_clusters import ClusterJoinCandidate, StoryClusterState
from src.processing.event_clustering import BatchCandidateIndex, EventClusteringService

DIMENSIONS = 32
MODEL = "test-model"
JOIN_SIMILARITY = 0.84
ACTIVE_WINDOW_HOURS = 72
MAX_CANDIDATES = 20
BASE_TIME = dt.datetime(2026, 11, 1, 0, 0, tzinfo=dt.timezone.utc)


@dataclass
class _Fragment:
    id: int
    vector: list[float]
    timestamp: dt.datetime


@dataclass
class _FakeStory:
    edition_id: int
    centroid: list[float]
    fragment_count: int
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime


@dataclass
class _FakeDb:
    """In-memory stand-in for the cluster repositories, with brute-force search."""

    stories: dict[int, _FakeStory] = field(default_factory=dict)
    assignments: dict[int, int] = field(default_factory=dict)
    next_story_id: int = 1
    next_assignment_id: int = 1

    def _ranked(
        self, edition_id: int, query: list[float], active_since: dt.datetime
    ) -> list[ClusterJoinCandidate]:
        found = [
            ClusterJoinCandidate(
                story_id=story_id,
                similarity=sum(a * b for a, b in zip(story.centroid, query, strict=True)),
                fragment_count=story.fragment_count,
                last_seen_at=story.last_seen_at,
            )
            for story_id, story in self.stories.items()
            if story.edition_id == edition_id and story.last_seen_at >= active_since
        ]
        found.sort(key=lambda candidate: candidate.similarity, reverse=True)
        return found

    # EventClusterRepository surface used by EventClusteringService.
    async def get_fragment_assignment(self, conn: Any, fragment_id: int) -> None:
        return None

    async def find_candidate_clusters(
        self,
        conn: Any,
        edition_id: int,
        query_vector: list[float],
        *,
        model: str,
        dimensions: int,
        active_since: dt.datetime,
        limit: int,
    ) -> list[ClusterJoinCandidate]:
        return self._ranked(edition_id, query_vector, active_since)[:limit]

    async def search_batch_candidates(
        self,
        conn: Any,
        edition_id: int,
        queries: list[tuple[int, list[float], dt.datetime]],
        *,
        model: str,
        dimensions: int,
        limit: int,
    ) -> dict[int, list[ClusterJoinCandidate]]:
        return {
            fragment_id: self._ranked(edition_id, vector, active_since)[:limit]
            for fragment_id, vector, active_since in queries
        }

    async def lock_cluster_state(self, conn: Any, story_id: int) -> StoryClusterState:
        story = self.stories[story_id]
        return StoryClusterState(
            story_id=story_id,
            centroid=list(story.centroid),
            model=MODEL,
            dimensions=DIMENSIONS,
            fragment_count=story.fragment_count,
            unique_source_count=1,
            first_seen_at=story.first_seen_at,
            last_seen_at=story.last_seen_at,
            latest_assignment_id=0,
            last_analyzed_assignment_id=None,
            last_analyzed_at=None,
            analysis_dirty=True,
            updated_at=story.last_seen_at,
        )

    async def assign_fragment_to_story(
        self,
        conn: Any,
        *,
        story_id: int,
        fragment_id: int,
        fragment_embedding_id: int,
        assignment_kind: str,
        similarity: float | None,
    ) -> int:
        assignment_id = self.next_assignment_id
        self.next_assignment_id += 1
        self.assignments[fragment_id] = story_id
        return assignment_id

    async def get_unique_sources_for_story(self, conn: Any, story_id: int) -> int:
        return 1

    async def upsert_cluster_state(
        self,
        conn: Any,
        *,
        story_id: int,
        centroid: list[float],
        model: str,
        dimensions: int,
        fragment_count: int,
        unique_source_count: int,
        first_seen_at: dt.datetime,
        last_seen_at: dt.datetime,
        latest_assignment_id: int,
        analysis_dirty: bool,
    ) -> None:
        story = self.stories[story_id]
        story.centroid = list(centroid)
        story.fragment_count = fragment_count
        story.first_seen_at = first_seen_at
        story.last_seen_at = last_seen_at

    # StoryRepository surface used by EventClusteringService.
    async def create_story_shell(self, conn: Any, *, edition_id: int, knowledge_source: str) -> int:
        story_id = self.next_story_id
        self.next_story_id += 1
        self.stories[story_id] = _FakeStory(
            edition_id=edition_id,
            centroid=[],
            fragment_count=0,
            first_seen_at=BASE_TIME,
            last_seen_at=BASE_TIME,
        )
        return story_id


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


_CENTERS_RNG = random.Random(12345)
_CENTERS = [_normalize([_CENTERS_RNG.gauss(0, 1) for _ in range(DIMENSIONS)]) for _ in range(6)]


def _fragments(seed: int, count: int, *, first_id: int) -> list[_Fragment]:
    # The same topic centers for every seed, so batches join stories from earlier batches.
    rng = random.Random(seed)
    fragments = []
    for offset in range(count):
        center = _CENTERS[rng.randrange(len(_CENTERS))]
        noisy = [c + rng.gauss(0, 0.05) for c in center]
        # Spread over ~150 hours so the 72-hour window actually excludes stories.
        timestamp = BASE_TIME + dt.timedelta(hours=2.5 * offset)
        fragments.append(
            _Fragment(id=first_id + offset, vector=_normalize(noisy), timestamp=timestamp)
        )
    return fragments


async def _assign_sequentially(
    db: _FakeDb, fragments: list[_Fragment], *, use_index: bool
) -> dict[int, int]:
    service = EventClusteringService(
        cluster_repo=db,  # type: ignore[arg-type]
        story_repo=db,  # type: ignore[arg-type]
    )
    index: BatchCandidateIndex | None = None
    if use_index:
        index = BatchCandidateIndex()
        index.add_snapshot(
            await db.search_batch_candidates(
                None,
                1,
                [
                    (
                        fragment.id,
                        fragment.vector,
                        fragment.timestamp - dt.timedelta(hours=ACTIVE_WINDOW_HOURS),
                    )
                    for fragment in fragments
                ],
                model=MODEL,
                dimensions=DIMENSIONS,
                limit=MAX_CANDIDATES + len(fragments),
            )
        )
    for fragment in fragments:
        await service.process_fragment(
            None,  # type: ignore[arg-type]
            fragment,  # type: ignore[arg-type]
            edition_id=1,
            fragment_embedding_id=fragment.id,
            vector=fragment.vector,
            model=MODEL,
            dimensions=DIMENSIONS,
            item_timestamp=fragment.timestamp,
            join_similarity=JOIN_SIMILARITY,
            active_window_hours=ACTIVE_WINDOW_HOURS,
            max_cluster_candidates=MAX_CANDIDATES,
            candidate_index=index,
        )
        if index is not None:
            index.commit()
    return dict(db.assignments)


def test_batch_index_assigns_like_per_fragment_search() -> None:
    seed_fragments = _fragments(seed=1, count=40, first_id=1)
    batch_fragments = _fragments(seed=2, count=60, first_id=1000)

    async def run() -> None:
        reference = _FakeDb()
        await _assign_sequentially(reference, seed_fragments, use_index=False)
        seed_story_ids = set(reference.stories)
        batched = copy.deepcopy(reference)

        expected = await _assign_sequentially(reference, batch_fragments, use_index=False)
        actual = await _assign_sequentially(batched, batch_fragments, use_index=True)

        assert actual == expected
        # The batch must join stories that existed before it, so the changed-story
        # path (a story joined earlier in the same batch) is exercised.
        batch_ids = {fragment.id for fragment in batch_fragments}
        assert sum(expected[fragment_id] in seed_story_ids for fragment_id in batch_ids) > 30
        for story_id, story in reference.stories.items():
            assert batched.stories[story_id].fragment_count == story.fragment_count
            assert batched.stories[story_id].centroid == story.centroid

    asyncio.run(run())


def test_batch_index_matches_on_empty_snapshot() -> None:
    fragments = _fragments(seed=3, count=30, first_id=1)

    async def run() -> None:
        expected = await _assign_sequentially(_FakeDb(), fragments, use_index=False)
        actual = await _assign_sequentially(_FakeDb(), fragments, use_index=True)
        assert actual == expected

    asyncio.run(run())


def test_candidates_after_a_join_match_a_fresh_search() -> None:
    seed_fragments = _fragments(seed=4, count=40, first_id=1)
    probe_fragments = _fragments(seed=5, count=10, first_id=2000)
    joining = _fragments(seed=6, count=1, first_id=3000)[0]

    async def run() -> None:
        db = _FakeDb()
        await _assign_sequentially(db, seed_fragments, use_index=False)
        queries = [
            (
                fragment.id,
                fragment.vector,
                fragment.timestamp - dt.timedelta(hours=ACTIVE_WINDOW_HOURS),
            )
            for fragment in [joining, *probe_fragments]
        ]
        index = BatchCandidateIndex()
        index.add_snapshot(
            await db.search_batch_candidates(
                None, 1, queries, model=MODEL, dimensions=DIMENSIONS, limit=MAX_CANDIDATES + 11
            )
        )
        service = EventClusteringService(
            cluster_repo=db,  # type: ignore[arg-type]
            story_repo=db,  # type: ignore[arg-type]
        )
        await service.process_fragment(
            None,  # type: ignore[arg-type]
            joining,  # type: ignore[arg-type]
            edition_id=1,
            fragment_embedding_id=joining.id,
            vector=joining.vector,
            model=MODEL,
            dimensions=DIMENSIONS,
            item_timestamp=joining.timestamp,
            join_similarity=JOIN_SIMILARITY,
            active_window_hours=ACTIVE_WINDOW_HOURS,
            max_cluster_candidates=MAX_CANDIDATES,
            candidate_index=index,
        )
        index.commit()

        for probe in probe_fragments:
            active_since = probe.timestamp - dt.timedelta(hours=ACTIVE_WINDOW_HOURS)
            from_index = index.candidates(
                probe.id,
                edition_id=1,
                vector=_normalize(probe.vector),
                active_since=active_since,
                limit=MAX_CANDIDATES,
            )
            fresh = await db.find_candidate_clusters(
                None,
                1,
                _normalize(probe.vector),
                model=MODEL,
                dimensions=DIMENSIONS,
                active_since=active_since,
                limit=MAX_CANDIDATES,
            )
            assert [c.story_id for c in from_index] == [c.story_id for c in fresh]
            for got, want in zip(from_index, fresh, strict=True):
                assert math.isclose(got.similarity, want.similarity, abs_tol=1e-9)

    asyncio.run(run())


def test_discarded_change_is_not_visible_to_later_fragments() -> None:
    index = BatchCandidateIndex()
    index.stage(
        7,
        edition_id=1,
        centroid=[1.0, 0.0],
        fragment_count=1,
        last_seen_at=BASE_TIME,
    )
    index.discard()
    found = index.candidates(99, edition_id=1, vector=[1.0, 0.0], active_since=BASE_TIME, limit=5)
    assert found == []
