# ruff: noqa: S101, S311
"""Batch candidate index: same ranking and same assignments as the database path."""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import random
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import numpy as np

from src.domain.event_clusters import (
    ActiveCentroid,
    ClusterJoinCandidate,
    ClusterStateChange,
    StoryClusterState,
)
from src.processing.cluster_index import ClusterIndex, load_cluster_index
from src.processing.event_clustering import EventClusteringService

MODEL = "test-embedding"
DIMS = 32
WINDOW_HOURS = 72
BASE = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class _Row:
    story_id: int
    edition_id: int
    centroid: list[float]
    model: str
    dimensions: int
    fragment_count: int
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime


class _Vector:
    """Stand-in for the driver's vector object, which exposes to_numpy()."""

    def __init__(self, values: list[float]) -> None:
        self._values = values

    def to_numpy(self) -> np.ndarray:
        return np.asarray(self._values, dtype=np.float32)


class FakeClusterRepo:
    """In-memory story_cluster_state. Candidate search is the reference: a plain scan, as in SQL."""

    def __init__(self) -> None:
        self.rows: dict[int, _Row] = {}
        self.story_editions: dict[int, int] = {}
        self.assignments: dict[int, tuple[int, int, str, float | None]] = {}
        self._next_assignment = 1

    def _eligible(
        self, edition_id: int, model: str, dimensions: int, active_since: dt.datetime
    ) -> list[_Row]:
        return [
            r
            for r in self.rows.values()
            if self.story_editions[r.story_id] == edition_id
            and r.model == model
            and r.dimensions == dimensions
            and r.last_seen_at >= active_since
        ]

    async def get_fragment_assignment(self, conn: Any, fragment_id: int) -> Any:
        return self.assignments.get(fragment_id)

    async def find_candidate_clusters(
        self,
        conn: Any,
        edition_id: int,
        query_vector: list[float],
        *,
        model: str,
        dimensions: int,
        active_since: dt.datetime,
        limit: int = 20,
    ) -> list[ClusterJoinCandidate]:
        ranked = sorted(
            self._eligible(edition_id, model, dimensions, active_since),
            key=lambda r: -_cosine(query_vector, r.centroid),
        )
        return [
            ClusterJoinCandidate(
                story_id=r.story_id,
                similarity=_cosine(query_vector, r.centroid),
                fragment_count=r.fragment_count,
                last_seen_at=r.last_seen_at,
            )
            for r in ranked[:limit]
        ]

    async def count_active_centroids(
        self,
        conn: Any,
        edition_id: int,
        *,
        model: str,
        dimensions: int,
        active_since: dt.datetime,
    ) -> int:
        return len(self._eligible(edition_id, model, dimensions, active_since))

    async def stream_active_centroids(
        self,
        conn: Any,
        edition_id: int,
        *,
        model: str,
        dimensions: int,
        active_since: dt.datetime,
    ):  # type: ignore[no-untyped-def]
        for r in self._eligible(edition_id, model, dimensions, active_since):
            yield ActiveCentroid(r.story_id, _Vector(r.centroid), r.last_seen_at, r.fragment_count)

    async def lock_cluster_state(self, conn: Any, story_id: int) -> StoryClusterState | None:
        r = self.rows.get(story_id)
        if r is None:
            return None
        return StoryClusterState(
            story_id=r.story_id,
            centroid=list(r.centroid),
            model=r.model,
            dimensions=r.dimensions,
            fragment_count=r.fragment_count,
            unique_source_count=1,
            first_seen_at=r.first_seen_at,
            last_seen_at=r.last_seen_at,
            latest_assignment_id=0,
            last_analyzed_assignment_id=None,
            last_analyzed_at=None,
            analysis_dirty=True,
            updated_at=r.last_seen_at,
        )

    async def assign_fragment_to_story(
        self,
        conn: Any,
        *,
        story_id: int,
        fragment_id: int,
        fragment_embedding_id: int,
        assignment_kind: str,
        similarity: float | None = None,
    ) -> int:
        assignment_id = self._next_assignment
        self._next_assignment += 1
        self.assignments[fragment_id] = (assignment_id, story_id, assignment_kind, similarity)
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
        analysis_dirty: bool = True,
    ) -> None:
        previous = self.rows.get(story_id)
        self.rows[story_id] = _Row(
            story_id=story_id,
            edition_id=self.story_editions[story_id],
            centroid=list(centroid),
            model=model,
            dimensions=dimensions,
            fragment_count=fragment_count,
            first_seen_at=previous.first_seen_at if previous else first_seen_at,
            last_seen_at=last_seen_at,
        )


class FakeStoryRepo:
    def __init__(self, cluster_repo: FakeClusterRepo) -> None:
        self._cluster_repo = cluster_repo
        self._next_story = 1

    async def create_story_shell(
        self, conn: Any, *, edition_id: int, knowledge_source: str = "event_first"
    ) -> int:
        story_id = self._next_story
        self._next_story += 1
        self._cluster_repo.story_editions[story_id] = edition_id
        return story_id


def _centers(rng: random.Random, count: int) -> list[list[float]]:
    return [[rng.gauss(0.0, 1.0) for _ in range(DIMS)] for _ in range(count)]


def _fragments(rng: random.Random, centers: list[list[float]], count: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(count):
        center = centers[rng.randrange(len(centers))]
        vec = [c + rng.gauss(0.0, 0.25) for c in center]
        out.append(
            {
                "id": i + 1,
                "edition": 1 + (i % 2),
                "vector": vec,
                "ts": BASE + dt.timedelta(minutes=30 * i),
            }
        )
    return out


async def _process_sequence(
    service: EventClusteringService,
    repo: FakeClusterRepo,
    fragments: list[dict[str, Any]],
    *,
    indexes: dict[int, ClusterIndex] | None,
) -> list[tuple[int, str]]:
    decisions: list[tuple[int, str]] = []
    for f in fragments:
        index = indexes[f["edition"]] if indexes is not None else None
        result = await service.process_fragment(
            None,  # type: ignore[arg-type]
            SimpleNamespace(id=f["id"]),  # type: ignore[arg-type]
            edition_id=f["edition"],
            fragment_embedding_id=f["id"],
            vector=f["vector"],
            model=MODEL,
            dimensions=DIMS,
            item_timestamp=f["ts"],
            join_similarity=0.84,
            active_window_hours=WINDOW_HOURS,
            max_cluster_candidates=20,
            candidate_index=index,
        )
        if result.state_change is not None and index is not None:
            index.apply(result.state_change)
        decisions.append((result.story_id, result.assignment_kind))
    return decisions


def test_nearest_matches_reference_ranking() -> None:
    rng = random.Random(7)
    index = ClusterIndex(dimensions=DIMS, capacity=1)
    rows: list[tuple[int, list[float], dt.datetime, int]] = []
    for story_id in range(1, 301):
        centroid = [rng.gauss(0.0, 1.0) for _ in range(DIMS)]
        last_seen = BASE + dt.timedelta(hours=rng.uniform(0, 200))
        rows.append((story_id, centroid, last_seen, rng.randint(1, 5)))
        index.set_row(story_id, centroid, last_seen, rows[-1][3])

    for _ in range(40):
        query = [rng.gauss(0.0, 1.0) for _ in range(DIMS)]
        active_since = BASE + dt.timedelta(hours=rng.uniform(0, 150))
        expected = sorted(
            (
                (story_id, _cosine(query, centroid))
                for story_id, centroid, last_seen, _count in rows
                if last_seen >= active_since
            ),
            key=lambda pair: -pair[1],
        )[:20]
        actual = index.nearest(query, active_since=active_since, limit=20)
        assert [c.story_id for c in actual] == [story_id for story_id, _ in expected]
        for candidate, (_story_id, sim) in zip(actual, expected, strict=True):
            assert math.isclose(candidate.similarity, sim, rel_tol=1e-4, abs_tol=1e-5)


def test_index_path_assigns_like_database_path() -> None:
    rng = random.Random(11)
    centers = _centers(rng, 60)
    fragments = _fragments(rng, centers, 400)
    split = 150

    async def run() -> tuple[list[tuple[int, str]], FakeClusterRepo, FakeClusterRepo]:
        repo_db = FakeClusterRepo()
        repo_index = FakeClusterRepo()
        svc_db = EventClusteringService(cluster_repo=repo_db, story_repo=FakeStoryRepo(repo_db))  # type: ignore[arg-type]
        svc_index = EventClusteringService(
            cluster_repo=repo_index,  # type: ignore[arg-type]
            story_repo=FakeStoryRepo(repo_index),  # type: ignore[arg-type]
        )

        # Phase 1 runs the database path on both, so the two stores start identical.
        first = fragments[:split]
        await _process_sequence(svc_db, repo_db, first, indexes=None)
        await _process_sequence(svc_index, repo_index, first, indexes=None)

        # Phase 2: one index per edition, loaded once for the batch, as the worker does.
        second = fragments[split:]
        active_since = min(f["ts"] for f in second) - dt.timedelta(hours=WINDOW_HOURS)
        indexes: dict[int, ClusterIndex] = {}
        for edition in (1, 2):
            count = sum(1 for f in second if f["edition"] == edition)
            indexes[edition] = await load_cluster_index(
                None,  # type: ignore[arg-type]
                repo_index,  # type: ignore[arg-type]
                edition,
                model=MODEL,
                dimensions=DIMS,
                active_since=active_since,
                extra_capacity=count,
            )

        db_decisions = await _process_sequence(svc_db, repo_db, second, indexes=None)
        index_decisions = await _process_sequence(svc_index, repo_index, second, indexes=indexes)
        assert index_decisions == db_decisions
        return db_decisions, repo_db, repo_index

    decisions, repo_db, repo_index = asyncio.run(run())

    kinds = [kind for _story, kind in decisions]
    assert kinds.count("vector_join") > 50
    assert kinds.count("new_story") > 5
    assert repo_db.rows.keys() == repo_index.rows.keys()
    for story_id, db_row in repo_db.rows.items():
        index_row = repo_index.rows[story_id]
        assert index_row.centroid == db_row.centroid
        assert index_row.fragment_count == db_row.fragment_count
        assert index_row.last_seen_at == db_row.last_seen_at


def test_persistent_cache_across_batches_matches_database_path() -> None:
    """A cache kept between batches (pruned each time) assigns exactly like the database path."""
    from src.processing.cluster_index import ClusterIndexCache

    rng = random.Random(23)
    centers = _centers(rng, 60)
    fragments = _fragments(rng, centers, 600)
    split = 150

    async def run() -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        repo_db = FakeClusterRepo()
        repo_cache = FakeClusterRepo()
        svc_db = EventClusteringService(cluster_repo=repo_db, story_repo=FakeStoryRepo(repo_db))  # type: ignore[arg-type]
        svc_cache = EventClusteringService(
            cluster_repo=repo_cache,  # type: ignore[arg-type]
            story_repo=FakeStoryRepo(repo_cache),  # type: ignore[arg-type]
        )
        await _process_sequence(svc_db, repo_db, fragments[:split], indexes=None)
        await _process_sequence(svc_cache, repo_cache, fragments[:split], indexes=None)

        cache = ClusterIndexCache(clock=lambda: 0.0)
        db_all: list[tuple[int, str]] = []
        cache_all: list[tuple[int, str]] = []
        # Batches of 50 fragments; the same cache object serves all of them.
        for start in range(split, len(fragments), 50):
            batch = fragments[start : start + 50]
            floor = min(f["ts"] for f in batch) - dt.timedelta(hours=WINDOW_HOURS)
            indexes = {}
            for edition in (1, 2):
                count = sum(1 for f in batch if f["edition"] == edition)
                indexes[edition] = await cache.get(
                    None,  # type: ignore[arg-type]
                    repo_cache,  # type: ignore[arg-type]
                    edition,
                    model=MODEL,
                    dimensions=DIMS,
                    active_since=floor,
                    extra_capacity=count,
                )
            db_all += await _process_sequence(svc_db, repo_db, batch, indexes=None)
            cache_all += await _process_sequence(svc_cache, repo_cache, batch, indexes=indexes)
        return db_all, cache_all

    db_decisions, cache_decisions = asyncio.run(run())
    assert cache_decisions == db_decisions
    kinds = [kind for _story, kind in db_decisions]
    assert kinds.count("vector_join") > 50
    assert kinds.count("new_story") > 5


def test_cache_reuses_reloads_and_invalidates() -> None:
    from src.processing.cluster_index import ClusterIndexCache

    class CountingRepo(FakeClusterRepo):
        def __init__(self) -> None:
            super().__init__()
            self.loads = 0

        async def count_active_centroids(self, conn: Any, edition_id: int, **kwargs: Any) -> int:
            self.loads += 1
            return await super().count_active_centroids(conn, edition_id, **kwargs)

    repo = CountingRepo()
    repo.story_editions[1] = 1
    repo.rows[1] = _Row(1, 1, [1.0] + [0.0] * (DIMS - 1), MODEL, DIMS, 1, BASE, BASE)
    now = [0.0]
    cache = ClusterIndexCache(reload_seconds=100.0, clock=lambda: now[0])

    async def get(active_since: dt.datetime) -> ClusterIndex:
        return await cache.get(
            None,  # type: ignore[arg-type]
            repo,  # type: ignore[arg-type]
            1,
            model=MODEL,
            dimensions=DIMS,
            active_since=active_since,
            extra_capacity=4,
        )

    async def main() -> None:
        first = await get(BASE)
        assert await get(BASE + dt.timedelta(hours=1)) is first  # reused, pruned
        assert repo.loads == 1
        await get(BASE - dt.timedelta(hours=5))  # older than the floor: reload
        assert repo.loads == 2
        now[0] = 150.0  # past reload_seconds
        await get(BASE)
        assert repo.loads == 3
        cache.invalidate()
        await get(BASE)
        assert repo.loads == 4

    asyncio.run(main())


def test_prune_keeps_rows_in_order_and_lookup_consistent() -> None:
    index = ClusterIndex(dimensions=DIMS, capacity=2)
    rng = random.Random(5)
    for story_id in range(1, 9):
        index.set_row(
            story_id,
            [rng.gauss(0, 1) for _ in range(DIMS)],
            BASE + dt.timedelta(hours=story_id),
            story_id,
        )
    removed = index.prune(BASE + dt.timedelta(hours=5))
    assert removed == 4
    assert len(index) == 4
    query = [1.0] + [0.0] * (DIMS - 1)
    ranked = index.nearest(query, active_since=BASE, limit=10)
    assert {c.story_id for c in ranked} == {5, 6, 7, 8}
    # A row updated after pruning is still found by its story id.
    index.apply(
        ClusterStateChange(
            story_id=6, centroid=query, fragment_count=9, last_seen_at=BASE + dt.timedelta(hours=20)
        )
    )
    top = index.nearest(query, active_since=BASE, limit=1)
    assert top[0].story_id == 6
    assert top[0].fragment_count == 9
