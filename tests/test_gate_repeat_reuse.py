# ruff: noqa: S101
"""A dropped story that grew only by reposts keeps its drop without a model call."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from src.config_loader import load_config
from src.domain.event_clusters import StoryClusterState
from src.processing.event_triage import (
    GATE_REPEAT_REUSE_MIN_WORDS,
    TRIAGE_VERSION,
    StoryTriageService,
)

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
AD_TEXT = "Ремонт генераторов любой сложности, выезд мастера на дом"
EARLIER_RUN_ID = 55
NEW_RUN_ID = 77


class Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = iter(rows)

    async def fetchone(self) -> tuple[Any, ...] | None:
        return next(self.rows, None)

    def __aiter__(self) -> Cursor:
        return self

    async def __anext__(self) -> tuple[Any, ...]:
        row = next(self.rows, None)
        if row is None:
            raise StopAsyncIteration
        return row


def drop_row(story_id: int, retention: str = "DROP") -> tuple[Any, ...]:
    brief = None
    if retention == "KEEP":
        brief = {
            "publishability": "brief",
            "evidence_items": [
                {
                    "text": "На Павлова нет света.",
                    "kind": "community_report",
                    "publication_use": "PUBLISH",
                    "source_fragment_ids": [100],
                }
            ],
        }
    return (
        story_id,
        "UNCERTAIN",
        0.9,
        "Нет привязки к городу",
        retention,
        "NONE" if retention == "DROP" else "BRIEF",
        "commercial_classified" if retention == "DROP" else None,
        0.97,
        "Объявление о ремонте",
        brief,
        EARLIER_RUN_ID,
    )


class GateConnection:
    """In-memory SQL boundary; no database or provider state is changed."""

    def __init__(self, repeat_rows: list[tuple[Any, ...]]) -> None:
        self.repeat_rows = repeat_rows
        self.repeat_params: tuple[Any, ...] | None = None
        self.decisions: list[tuple[Any, ...]] = []
        self.scope_decisions: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, params: Any = ()) -> Cursor:
        query = " ".join(sql.split())
        if "previous AS" in query:
            self.repeat_params = params
            return Cursor(self.repeat_rows)
        if "JOIN requested r" in query:
            return Cursor([])
        if "FROM story_fragments sf" in query:
            return Cursor(
                [
                    (sid, 100 + sid, AD_TEXT, 1, "Бердянск", "community", NOW, None)
                    for sid in params[0]
                ]
            )
        if "JOIN source_fragments sib" in query:
            return Cursor([])
        if query.startswith("SELECT slug FROM editions"):
            return Cursor([("berdyansk",)])
        if query.startswith("INSERT INTO story_event_triage_runs"):
            return Cursor([(NEW_RUN_ID,)])
        if query.startswith("INSERT INTO story_event_triage_decisions"):
            self.decisions.append(params)
            return Cursor([])
        if query.startswith("INSERT INTO story_edition_scope_decisions"):
            self.scope_decisions.append(params)
            return Cursor([])
        raise AssertionError(f"Unexpected SQL boundary: {query}")


class Provider:
    """Answers with an unlocated drop for the given stories and counts calls."""

    def __init__(self, story_ids: list[int]) -> None:
        self.story_ids = story_ids
        self.calls = 0

    async def chat_completion(self, **kwargs: Any) -> str:
        self.calls += 1
        return json.dumps(
            {
                "results": [
                    {
                        "story_id": sid,
                        "scope": "UNCERTAIN",
                        "scope_confidence": 0.9,
                        "scope_reason": "Недостаточно данных для локальной привязки.",
                        "scope_basis_fragment_ids": [],
                        "retention": "DROP",
                        "enrichment": "NONE",
                        "confidence": 0.97,
                        "reason": "Объявление",
                        "exclusion_reason": "commercial_classified",
                        "brief_payload": None,
                    }
                    for sid in self.story_ids
                ]
            }
        )


def story(story_id: int, assignment_id: int) -> StoryClusterState:
    return StoryClusterState(
        story_id, [], "test", 0, 3, 2, NOW, NOW, assignment_id, None, None, True, NOW
    )


async def run_gate(
    monkeypatch: pytest.MonkeyPatch,
    conn: GateConnection,
    provider: Provider,
    stories: list[StoryClusterState],
    **kwargs: Any,
):
    service = StoryTriageService(provider)

    async def no_hints(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(service, "_load_recent_subject_hints", no_hints)
    return await service.triage_stories_batch(
        conn,
        stories,
        edition_id=1,
        scope_config=load_config("config.yaml").settings.edition_scopes["berdyansk"],
        scope_hash="test",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_repost_keeps_earlier_drop_without_model_call(monkeypatch):
    conn = GateConnection([drop_row(10)])
    provider = Provider([])

    result = await run_gate(monkeypatch, conn, provider, [story(10, 42)])

    assert provider.calls == 0
    assert [(r.story_id, r.retention, r.exclusion_reason) for r in result.results] == [
        (10, "DROP", "commercial_classified")
    ]
    assert result.deferred_story_ids == ()
    # Stored again for the new assignment, attributed to the run that decided it.
    assert conn.decisions[0][:5] == (EARLIER_RUN_ID, 10, 42, TRIAGE_VERSION, "test")
    assert conn.scope_decisions[0][:4] == (EARLIER_RUN_ID, 10, 1, 42)
    assert conn.repeat_params is not None
    assert conn.repeat_params[:2] == ([10], [42])
    assert conn.repeat_params[-1] == GATE_REPEAT_REUSE_MIN_WORDS


@pytest.mark.asyncio
async def test_only_stories_with_new_text_reach_the_model(monkeypatch):
    conn = GateConnection([drop_row(10)])
    provider = Provider([11])

    result = await run_gate(monkeypatch, conn, provider, [story(10, 42), story(11, 43)])

    assert provider.calls == 1
    # Story 10 was not sent: the answer covers only 11 and nothing is missing.
    assert result.deferred_story_ids == ()
    assert result.missing_story_ids == ()
    assert sorted(r.story_id for r in result.results) == [10, 11]
    assert sorted((d[1], d[0]) for d in conn.decisions) == [
        (10, EARLIER_RUN_ID),
        (11, NEW_RUN_ID),
    ]


@pytest.mark.asyncio
async def test_publication_cutoff_never_reuses_a_decision(monkeypatch):
    conn = GateConnection([drop_row(10)])
    provider = Provider([10])

    result = await run_gate(monkeypatch, conn, provider, [story(10, 42)], source_cutoff_at=NOW)

    assert conn.repeat_params is None
    assert provider.calls == 1
    assert [d[0] for d in conn.decisions] == [NEW_RUN_ID]
    assert [r.story_id for r in result.results] == [10]


@pytest.mark.asyncio
async def test_lost_fence_stores_nothing_and_skips_the_model(monkeypatch):
    conn = GateConnection([drop_row(10)])
    provider = Provider([])

    async def fence(*args: Any) -> bool:
        return False

    result = await run_gate(monkeypatch, conn, provider, [story(10, 42)], decision_fence=fence)

    assert provider.calls == 0
    assert result.results == ()
    assert result.fence_lost_story_ids == (10,)
    assert conn.decisions == []
    assert conn.scope_decisions == []


@pytest.mark.asyncio
async def test_earlier_keep_is_never_reused():
    service = StoryTriageService(Provider([]))
    conn = GateConnection([drop_row(10, retention="KEEP")])

    reusable = await service._lookup_repeat_decisions(
        conn, [story(10, 42)], scope_hash="test", edition_id=1
    )

    assert reusable == {}
