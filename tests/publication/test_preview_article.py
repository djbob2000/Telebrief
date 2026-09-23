"""Frozen article replay stays read-only and exposes only safe diagnostics."""

from __future__ import annotations

import datetime as dt
import importlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.publication import article_preview

_SNAPSHOT = dt.datetime(2026, 9, 23, 18, 0, tzinfo=dt.timezone.utc)


def _preview_config():
    return SimpleNamespace(
        settings=SimpleNamespace(
            article=SimpleNamespace(save_debug_artifacts=True),
        )
    )


class _Connection:
    def __init__(self, edition_row=("test-edition", "Europe/Kyiv")):
        self.edition_row = edition_row
        self.sql: list[str] = []

    async def execute(self, query, params=()):
        self.sql.append(query.strip())
        assert query.strip().upper().startswith("SELECT")
        cursor = SimpleNamespace(fetchone=AsyncMock(return_value=self.edition_row))
        return cursor


class _UnitOfWork:
    def __init__(self, conn):
        self.conn = conn
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield self.conn


def _setup_preview(monkeypatch, *, publication_type="daily_article", inputs=None, edition_row=None):
    run = SimpleNamespace(
        id=189,
        edition_id=7,
        publication_type=publication_type,
        snapshot_at=_SNAPSHOT,
        status="ready",
    )
    sealed_inputs = [SimpleNamespace(id=41)] if inputs is None else inputs
    conn = _Connection(edition_row or ("test-edition", "Europe/Kyiv"))
    uow = _UnitOfWork(conn)
    runtime = SimpleNamespace(uow=uow)
    repo_calls = []
    adapter_calls = []
    generator_calls = []

    class FakeRepository:
        async def get_run_by_id(self, used_conn, run_id):
            repo_calls.append(("get_run_by_id", used_conn, run_id))
            return run

        async def load_sealed_inputs(self, used_conn, run_id):
            repo_calls.append(("load_sealed_inputs", used_conn, run_id))
            return sealed_inputs

    frozen = SimpleNamespace(
        analysis=SimpleNamespace(article_context=None),
        edition_slug="test-edition",
        run_id=189,
    )

    class FakeAdapter:
        def __init__(self, *, uow, repo):
            assert uow is runtime.uow
            assert isinstance(repo, FakeRepository)

        async def adapt_inputs_on(
            self, used_conn, run_id, *, inputs, include_anchor_publications=True
        ):
            adapter_calls.append((used_conn, run_id, inputs, include_anchor_publications))
            return frozen

    class FakeGenerator:
        def __init__(self, *, config, logger):
            assert config.settings.article.save_debug_artifacts is False
            self.config = config

        async def generate_from_frozen_input(self, used_frozen, *, attempt_observer):
            generator_calls.append((used_frozen, attempt_observer))
            attempt_id = await attempt_observer.attempt_started(
                "writer",
                provider="test-provider",
                model="test-model",
                prompt_hash="safe-prompt-hash",
                metadata={
                    "attempt_number": 1,
                    "prompt_chars": 123,
                    "response_text": "private writer response",
                    "contact": "+79990000000",
                },
            )
            await attempt_observer.attempt_finished(
                attempt_id,
                "succeeded",
                metadata={
                    "status": "writer_success",
                    "writer_status": "passed",
                    "planned_story_count": 2,
                    "final_covered_story_count": 2,
                    "evidence_boundary_passed": True,
                    "quality_gate_passed": True,
                    "quality_after_edit": {
                        "version": "article-reader-quality-v3",
                        "finding_count": 0,
                        "needs_edit": False,
                        "findings": [],
                    },
                    "writer_attempt": {
                        "context_hash": "safe-context-hash",
                        "raw_response": "private response",
                        "source_text": "private source text",
                    },
                    "claim_trace": [{"source_text": "private source text"}],
                },
            )
            return "Вечер в городе", "Короткий подтверждённый лид.", "Текст статьи."

    monkeypatch.setattr(article_preview, "get_runtime", lambda: runtime)
    monkeypatch.setattr(article_preview, "PublicationRepository", FakeRepository)
    monkeypatch.setattr(article_preview, "EventEditorialAdapter", FakeAdapter)
    monkeypatch.setattr(article_preview, "ArticleGenerator", FakeGenerator)
    return run, sealed_inputs, conn, uow, repo_calls, adapter_calls, generator_calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_frozen_run_replay_uses_sealed_inputs_and_returns_safe_diagnostics(monkeypatch):
    run, inputs, conn, uow, repo_calls, adapter_calls, generator_calls = _setup_preview(monkeypatch)
    config = _preview_config()

    preview = await article_preview.build_article_preview_from_run(189, config=config)

    assert preview.run_id == 189
    assert preview.publication_type == "daily_article"
    assert preview.edition_slug == "test-edition"
    assert preview.snapshot_at == _SNAPSHOT
    assert preview.markdown == (
        "# Вечер в городе\n\nКороткий подтверждённый лид.\n\nТекст статьи.\n"
    )
    assert repo_calls == [
        ("get_run_by_id", conn, 189),
        ("load_sealed_inputs", conn, 189),
    ]
    assert adapter_calls == [(conn, 189, inputs, False)]
    assert generator_calls[0][0].run_id == 189
    assert isinstance(generator_calls[0][1], article_preview._MemoryGenerationAttemptObserver)
    assert config.settings.article.save_debug_artifacts is True
    assert uow.transactions == 1
    assert conn.sql and all(statement.upper().startswith("SELECT") for statement in conn.sql)
    assert run.status == "ready"

    diagnostics = preview.diagnostics
    serialized = json.dumps(diagnostics, ensure_ascii=False)
    assert diagnostics["schema_version"] == "article-run-preview-v1"
    assert diagnostics["edition_timezone"] == "Europe/Kyiv"
    assert diagnostics["generation"]["attempt_count"] == 1
    attempt = diagnostics["generation"]["attempts"][0]
    assert attempt["final"]["quality_gate_passed"] is True
    assert attempt["final"]["quality_after_edit"]["version"] == "article-reader-quality-v3"
    for secret in (
        "private writer response",
        "+79990000000",
        "private response",
        "private source text",
    ):
        assert secret not in serialized


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("publication_type", ["weekly_article", "monthly_article"])
async def test_longitudinal_frozen_replay_passes_only_sealed_inputs_without_anchors(
    monkeypatch, publication_type
):
    _run, sealed_inputs, _conn, _uow, _repo_calls, adapter_calls, _generator_calls = _setup_preview(
        monkeypatch, publication_type=publication_type
    )

    preview = await article_preview.build_article_preview_from_run(189, config=_preview_config())

    assert preview.publication_type == publication_type
    assert adapter_calls == [(_conn, 189, sealed_inputs, False)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replay_does_not_construct_publication_or_delivery_services(monkeypatch):
    _setup_preview(monkeypatch)
    forbidden_paths = (
        ("src.bootstrap", "ApplicationInfrastructure"),
        ("src.publication.orchestrator", "PublicationOrchestrator"),
        ("src.publication.selection", "EditorialSelectionService"),
        ("src.publication.generation", "PublicationGenerationService"),
        ("src.publication.delivery", "PublicationDeliveryService"),
        ("src.publication.editorial_adapter", "DatabaseGenerationAttemptObserver"),
        ("src.image_generator", "NewsImageGenerator"),
        ("src.telegraph", "TelegraphPublisher"),
        ("src.publication.adapters", "TelegramChannelDestinationClient"),
    )
    forbidden = []
    for module_name, class_name in forbidden_paths:
        constructor = Mock(side_effect=AssertionError(f"constructed {class_name}"))
        monkeypatch.setattr(importlib.import_module(module_name), class_name, constructor)
        forbidden.append(constructor)

    await article_preview.build_article_preview_from_run(189, config=_preview_config())

    assert all(not constructor.called for constructor in forbidden)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_exists", "publication_type", "inputs", "edition_row", "message"),
    [
        (False, "daily_article", [object()], ("test-edition", "Europe/Kyiv"), "not found"),
        (True, "digest_grouped", [object()], ("test-edition", "Europe/Kyiv"), "unsupported"),
        (True, "daily_article", [], ("test-edition", "Europe/Kyiv"), "no sealed inputs"),
        (True, "daily_article", [object()], ("test-edition", None), "missing timezone"),
        (True, "daily_article", [object()], ("test-edition", "No/Such_Zone"), "invalid timezone"),
    ],
)
async def test_invalid_frozen_run_fails_before_writer(
    monkeypatch, run_exists, publication_type, inputs, edition_row, message
):
    run, _sealed_inputs, conn, _uow, _repo_calls, adapter_calls, generator_calls = _setup_preview(
        monkeypatch,
        publication_type=publication_type,
        inputs=inputs,
        edition_row=edition_row,
    )
    if not run_exists:
        monkeypatch.setattr(
            article_preview.PublicationRepository,
            "get_run_by_id",
            AsyncMock(return_value=None),
        )

    with pytest.raises(ValueError, match=message):
        await article_preview.build_article_preview_from_run(999, config=_preview_config())

    assert not adapter_calls
    assert not generator_calls
    assert run.status == "ready"
    assert all(statement.upper().startswith("SELECT") for statement in conn.sql)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conflicting_requested_edition_fails_before_materialization_or_writer(monkeypatch):
    run, _inputs, conn, _uow, _repo_calls, adapter_calls, generator_calls = _setup_preview(
        monkeypatch
    )

    with pytest.raises(ValueError, match="conflicts with frozen run edition"):
        await article_preview.build_article_preview_from_run(
            189,
            config=_preview_config(),
            expected_edition_slug="another-edition",
        )

    assert run.status == "ready"
    assert not adapter_calls
    assert not generator_calls
    assert len(conn.sql) == 1
