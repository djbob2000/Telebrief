"""Unit tests for Event-First article fail-closed rejection semantics."""

from __future__ import annotations

import datetime as dt
import json
import logging
from unittest.mock import AsyncMock

import pytest

from src.article_generator import ArticleGenerator
from src.config_loader import Config, PublicationEditorialConfig, Settings
from src.publication.article_context import ArticleEditorialContext, ArticleSupport
from src.publication.errors import ArticlePublicationRejected

_NOW = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)


class RecordingAttemptObserver:
    def __init__(self) -> None:
        self.started_attempts: list[dict] = []
        self.finished_attempts: dict[int, dict] = []
        self.started_kinds: list[str] = []
        self._next_id = 1

    async def attempt_started(self, kind: str, **kwargs) -> int:
        att_id = self._next_id
        self._next_id += 1
        self.started_attempts.append({"id": att_id, "kind": kind, "kwargs": kwargs})
        self.started_kinds.append(kind)
        return att_id

    async def attempt_finished(self, attempt_id: int, status: str, **kwargs) -> None:
        if isinstance(self.finished_attempts, list):
            self.finished_attempts = {}
        self.finished_attempts[attempt_id] = {"status": status, "kwargs": kwargs}


@pytest.fixture
def sample_article_context() -> ArticleEditorialContext:
    sup = ArticleSupport(
        support_id="story:1:evidence:0:frag:101",
        text="Авария на подстанции: временно обесточен центр.",
        source_text="Авария на подстанции: временно обесточен центр.",
        support_kind="evidence",
        publication_use="PUBLISH",
        source_refs=("ref-1",),
        fragment_ids=(101,),
        source_item_ids=(1,),
        observed_at=_NOW,
        temporal_role="CURRENT_WINDOW",
    )
    return ArticleEditorialContext(
        headline_candidates=("Отключение света в центре",),
        support_index=(sup,),
        support_by_id={sup.support_id: sup},
        evidence_index=(sup,),
        recurring_topics=("utilities",),
    )


@pytest.fixture
def article_generator() -> ArticleGenerator:
    settings = Settings(
        schedule_time="09:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4",
        openai_temperature=0.7,
        ai_provider="openai",
        publication_editorial=PublicationEditorialConfig(
            article_min_words=5,
            article_min_sections=1,
        ),
    )
    config = Config(
        channels=[],
        settings=settings,
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
    )
    gen = ArticleGenerator(config=config, logger=logging.getLogger("test"))
    gen.provider = AsyncMock()
    return gen


def test_article_publication_rejected_exposes_stable_reason_and_metadata():
    exc = ArticlePublicationRejected(
        reason="validation_failed",
        message="draft failed deterministic validation",
        metadata={"violations": ["UNSUPPORTED_CLAIM_ATOM:LEAD"]},
    )

    assert exc.reason == "validation_failed"
    assert exc.error_kind == "article_validation_rejected"
    assert exc.metadata == {"violations": ["UNSUPPORTED_CLAIM_ATOM:LEAD"]}
    assert str(exc) == "draft failed deterministic validation"


def test_article_publication_rejected_maps_writer_failure_to_stable_error_kind():
    exc = ArticlePublicationRejected(
        reason="writer_failed",
        message="provider failed",
        metadata={"exception_type": "TimeoutError"},
    )

    assert exc.error_kind == "article_writer_rejected"


@pytest.mark.asyncio
async def test_event_article_validation_failure_rejects_instead_of_rendering_fallback(
    article_generator,
    sample_article_context,
):
    # Invalid structured article json (contains unsupported hallucinated duration "полтора часа")
    sup_id = "story:1:evidence:0:frag:101"
    invalid_json = json.dumps(
        {
            "title": "Отключение света в центре",
            "title_support_ids": [sup_id],
            "title_claims": [{"text": "Отключение света в центре", "cited_support_ids": [sup_id]}],
            "lead": "В центре города авария на подстанции.",
            "lead_support_ids": [sup_id],
            "lead_claims": [
                {"text": "В центре города авария на подстанции", "cited_support_ids": [sup_id]}
            ],
            "sections": [
                {
                    "heading": "Энергоснабжение",
                    "heading_support_ids": [sup_id],
                    "heading_claims": [{"text": "Энергоснабжение", "cited_support_ids": [sup_id]}],
                    "paragraphs": [
                        {
                            "text": "Бригады восстановили питание в течение полутора часов.",
                            "cited_support_ids": [sup_id],
                            "claims": [
                                {
                                    "text": "Бригады восстановили питание в течение полутора часов.",
                                    "cited_support_ids": [sup_id],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    article_generator.provider.chat_completion.return_value = invalid_json
    observer = RecordingAttemptObserver()

    with pytest.raises(ArticlePublicationRejected) as caught:
        await article_generator.generate_from_event_article_context(
            sample_article_context, attempt_observer=observer
        )

    assert caught.value.reason == "validation_failed"
    assert "violations" in caught.value.metadata
    assert observer.started_kinds == ["writer"]
    assert observer.finished_attempts[1]["status"] == "failed"


@pytest.mark.asyncio
async def test_event_article_writer_error_rejects_instead_of_rendering_fallback(
    article_generator,
    sample_article_context,
):
    article_generator.provider.chat_completion.side_effect = TimeoutError("writer timeout")
    observer = RecordingAttemptObserver()

    with pytest.raises(ArticlePublicationRejected) as caught:
        await article_generator.generate_from_event_article_context(
            sample_article_context, attempt_observer=observer
        )

    assert caught.value.reason == "writer_failed"
    assert caught.value.metadata["exception_type"] == "TimeoutError"
    assert observer.started_kinds == ["writer"]
    assert observer.finished_attempts[1]["status"] == "failed"
