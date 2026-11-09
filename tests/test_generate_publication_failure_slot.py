# ruff: noqa: S101
"""Any terminal generation failure closes the publication slot and tells its owner."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import src.jobs.publication as publication_jobs
from src.publication.errors import ArticlePublicationRejected
from src.publication.generation import PublicationGenerationService


class _Marks:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, run: Any, *, error_kind: str, notify: bool = True) -> None:
        self.calls.append({"run": run, "error_kind": error_kind, "notify": notify})


def _run(status: str, error_kind: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        status=status,
        error_kind=error_kind,
        metadata={"refresh_run_id": 11, "trigger": "scheduled"},
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    loaded: list[SimpleNamespace],
    error: Exception,
) -> _Marks:
    runs = list(loaded)

    async def load_run(_run_id: int) -> SimpleNamespace:
        return runs.pop(0)

    async def failing_generate(_self: PublicationGenerationService, _run_id: int) -> None:
        raise error

    marks = _Marks()
    monkeypatch.setattr(publication_jobs, "_load_publication_run", load_run)
    monkeypatch.setattr(publication_jobs, "_mark_publication_intent_failed", marks)
    monkeypatch.setattr(publication_jobs, "get_runtime", lambda: SimpleNamespace(uow=object()))
    monkeypatch.setattr(PublicationGenerationService, "generate", failing_generate)
    return marks


async def _invoke_job() -> None:
    await publication_jobs.generate_publication.func(SimpleNamespace(), run_id=7)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("late"), RuntimeError("provider cascade")])
async def test_failure_after_run_is_marked_failed_closes_the_slot(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    failed = _run("failed", error_kind=type(error).__name__)
    marks = _install(monkeypatch, loaded=[_run("selected_inputs_sealed"), failed], error=error)

    with pytest.raises(type(error)):
        await _invoke_job()

    assert marks.calls == [{"run": failed, "error_kind": type(error).__name__, "notify": True}]


@pytest.mark.asyncio
async def test_failure_before_the_run_is_marked_keeps_the_slot_open_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed = _run("selected_inputs_sealed")
    marks = _install(monkeypatch, loaded=[sealed, sealed], error=ConnectionError("db blip"))

    with pytest.raises(ConnectionError):
        await _invoke_job()

    assert marks.calls == []


@pytest.mark.asyncio
async def test_article_rejection_still_closes_once_without_reloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run("selected_inputs_sealed")
    rejection = ArticlePublicationRejected.__new__(ArticlePublicationRejected)
    rejection.error_kind = "article_validation_rejected"
    marks = _install(monkeypatch, loaded=[run], error=rejection)

    await _invoke_job()

    assert marks.calls == [
        {"run": run, "error_kind": "article_validation_rejected", "notify": True}
    ]
