# ruff: noqa: S101
"""Run-level article retry: one repeat of a safety rejection on the same sealed inputs."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from src.publication.errors import ArticlePublicationRejected
from src.publication.generation import PublicationGenerationService


class _ScriptedGenerator:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[Any, Any]] = []

    async def generate_from_frozen_input(self, frozen: Any, *, attempt_observer: Any = None):
        self.calls.append((frozen, attempt_observer))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _service(generator: _ScriptedGenerator) -> PublicationGenerationService:
    service = object.__new__(PublicationGenerationService)
    service.generator = generator
    service.logger = logging.getLogger("test_article_run_retry")
    return service


def _rejected(reason: str) -> ArticlePublicationRejected:
    return ArticlePublicationRejected(reason=reason, message=f"rejected: {reason}")


@pytest.mark.asyncio
async def test_validation_rejection_is_retried_once_with_the_same_frozen_input():
    generator = _ScriptedGenerator([_rejected("validation_failed"), ("Заголовок", "Лид", "Тело")])
    service = _service(generator)
    observer = object()

    result = await service._generate_article_with_run_retry(7, "frozen-input", observer)

    assert result == ("Заголовок", "Лид", "Тело")
    assert [frozen for frozen, _ in generator.calls] == ["frozen-input", "frozen-input"]
    assert all(seen is observer for _, seen in generator.calls)


@pytest.mark.asyncio
async def test_quality_rejection_is_retried_once():
    generator = _ScriptedGenerator([_rejected("quality_failed"), ("T", "L", "B")])
    service = _service(generator)

    assert await service._generate_article_with_run_retry(7, "frozen", None) == ("T", "L", "B")
    assert len(generator.calls) == 2


@pytest.mark.asyncio
async def test_second_rejection_fails_closed_without_a_third_attempt():
    generator = _ScriptedGenerator(
        [_rejected("validation_failed"), _rejected("validation_failed"), ("never", "", "")]
    )
    service = _service(generator)

    with pytest.raises(ArticlePublicationRejected) as raised:
        await service._generate_article_with_run_retry(7, "frozen", None)

    assert raised.value.reason == "validation_failed"
    assert len(generator.calls) == 2
    assert generator.outcomes == [("never", "", "")]


@pytest.mark.asyncio
async def test_writer_failure_is_not_retried_at_run_level():
    generator = _ScriptedGenerator([_rejected("writer_failed"), ("never", "", "")])
    service = _service(generator)

    with pytest.raises(ArticlePublicationRejected) as raised:
        await service._generate_article_with_run_retry(7, "frozen", None)

    assert raised.value.reason == "writer_failed"
    assert len(generator.calls) == 1


@pytest.mark.asyncio
async def test_timeout_is_not_retried_at_run_level():
    generator = _ScriptedGenerator([TimeoutError("deadline"), ("never", "", "")])
    service = _service(generator)

    with pytest.raises(TimeoutError):
        await service._generate_article_with_run_retry(7, "frozen", None)

    assert len(generator.calls) == 1
