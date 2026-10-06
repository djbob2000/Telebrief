# ruff: noqa: S101
"""A failed factual extraction must not become a cached noise decision."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from src.config_loader import load_config
from src.domain.event_clusters import StoryClusterState
from src.processing.event_triage import StoryTriageService


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


class GateConnection:
    """In-memory SQL boundary; no database or provider state is changed."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.decisions: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Cursor:
        query = " ".join(sql.split())
        if "FROM story_fragments sf" in query:
            return Cursor([(10, 100, self.text, 1, "Бердянск", "community", NOW, None)])
        if query.startswith("SELECT slug FROM editions"):
            return Cursor([("berdyansk",)])
        if query.startswith("INSERT INTO story_event_triage_runs"):
            return Cursor([(1,)])
        if query.startswith("INSERT INTO story_event_triage_decisions"):
            self.decisions.append(params)
            return Cursor([])
        if query.startswith("INSERT INTO story_edition_scope_decisions"):
            return Cursor([])
        raise AssertionError(f"Unexpected SQL boundary: {query}")


class Provider:
    def __init__(self, evidence: list[dict[str, Any]], overrides: dict[str, Any]) -> None:
        self.evidence = evidence
        self.overrides = overrides

    async def chat_completion(self, **kwargs: Any) -> str:
        return json.dumps(
            {
                "results": [
                    {
                        "story_id": 10,
                        "scope": "LOCAL",
                        "scope_confidence": 1.0,
                        "scope_reason": "Primary source names Berdyansk",
                        "scope_basis_fragment_ids": [100],
                        "retention": "KEEP",
                        "enrichment": "BRIEF",
                        "confidence": 1.0,
                        "reason": "Local report",
                        "exclusion_reason": None,
                        "brief_payload": {
                            "publishability": "brief",
                            "evidence_items": self.evidence,
                        },
                        **self.overrides,
                    }
                ]
            }
        )


NOW = dt.datetime(2026, 10, 6, tzinfo=dt.timezone.utc)


async def run_gate(
    monkeypatch: pytest.MonkeyPatch, source: str, items: list[dict[str, Any]], **overrides: Any
):
    service = StoryTriageService(Provider(items, overrides))

    async def no_hints(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(service, "_load_recent_subject_hints", no_hints)
    conn = GateConnection(source)
    story = StoryClusterState(10, [], "test", 0, 1, 1, NOW, NOW, 1, None, None, True, NOW)
    result = await service.triage_stories_batch(
        conn,
        [story],
        edition_id=1,
        scope_config=load_config("config.yaml").settings.edition_scopes["berdyansk"],
        scope_hash="test",
        source_cutoff_at=NOW,
    )
    return result, conn


def evidence(text: str, kind: str = "community_report", use: str = "PUBLISH") -> dict[str, Any]:
    return {"text": text, "kind": kind, "publication_use": use, "source_fragment_ids": [100]}


@pytest.mark.asyncio
async def test_failed_service_extraction_defers_instead_of_dropping_as_noise(monkeypatch):
    result, conn = await run_gate(
        monkeypatch,
        "В Бердянске на Павлова свет есть.",
        [evidence("На Павлова нет воды.")],
    )
    assert result.results == ()
    assert result.invalid_story_ids == (10,)
    assert result.deferred_story_ids == (10,)
    assert conn.decisions == []


@pytest.mark.asyncio
async def test_genuine_question_remains_noise_without_extraction_recovery(monkeypatch):
    result, _ = await run_gate(
        monkeypatch,
        "В Бердянске на Павлова свет есть?",
        [evidence("На Павлова свет есть?", "resident_question", "CONTEXT")],
    )
    assert result.deferred_story_ids == ()
    assert result.results[0].retention == "DROP"


@pytest.mark.asyncio
async def test_valid_local_report_survives_bad_secondary_extraction(monkeypatch):
    result, _ = await run_gate(
        monkeypatch,
        "В Бердянске на Павлова свет есть.",
        [evidence("На Павлова свет есть."), evidence("На Павлова нет воды.")],
    )
    assert result.deferred_story_ids == ()
    assert result.results[0].retention == "KEEP"
    assert [
        item.text
        for item in result.results[0].brief_payload.evidence_items
        if item.publication_use == "PUBLISH"
    ] == ["На Павлова свет есть."]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "Нет текста фрагмента и нет привязки к фокусной зоне.",
        "No story content provided.",
    ],
)
async def test_missing_input_claim_with_delivered_source_is_processing_failure(monkeypatch, reason):
    result, conn = await run_gate(
        monkeypatch,
        "В Бердянске на Павлова свет есть.",
        [],
        scope="UNCERTAIN",
        scope_reason=reason,
        scope_basis_fragment_ids=[],
        retention="DROP",
        enrichment="NONE",
        brief_payload=None,
    )
    assert result.results == ()
    assert result.invalid_story_ids == (10,)
    assert result.deferred_story_ids == (10,)
    assert conn.decisions == []


@pytest.mark.asyncio
async def test_genuine_unlocated_report_is_not_a_processing_failure(monkeypatch):
    result, _ = await run_gate(
        monkeypatch,
        "У меня свет есть.",
        [],
        scope="UNCERTAIN",
        scope_reason="Недостаточно данных для локальной привязки.",
        scope_basis_fragment_ids=[],
        retention="DROP",
        enrichment="NONE",
        brief_payload=None,
    )
    assert result.deferred_story_ids == ()
    assert result.results[0].scope == "UNCERTAIN"


@pytest.mark.asyncio
async def test_changed_duration_relation_is_recoverable_not_noise(monkeypatch):
    result, conn = await run_gate(
        monkeypatch,
        "В Бердянске Слободка со светом побыла 3 дня",
        [evidence("На Слободке свет появился один раз в течение трёх дней.")],
    )
    assert result.results == ()
    assert result.invalid_story_ids == (10,)
    assert result.deferred_story_ids == (10,)
    assert conn.decisions == []
