"""Contract tests for single-call event-first article generation."""

from __future__ import annotations

import datetime as dt
import json
import logging
from unittest.mock import AsyncMock

import pytest

from src.article_generator import ArticleGenerator
from src.config_loader import Config, PublicationEditorialConfig, Settings
from src.editorial_models import EditorialAnalysis, PreparedBundle
from src.publication.article_context import (
    build_article_editorial_context,
)
from src.publication.editorial_adapter import FrozenEditorialInput
from src.publication.evidence import PublicationEvidence

_NOW = dt.datetime(2026, 8, 29, 20, 0, tzinfo=dt.timezone.utc)


def _make_article_config() -> Config:
    settings = Settings(
        schedule_time="09:00",
        timezone="UTC",
        lookback_hours=24,
        openai_model="gpt-4",
        openai_temperature=0.7,
        ai_provider="openai",
        publication_editorial=PublicationEditorialConfig(
            article_min_words=10,
            article_max_words=500,
            article_min_sections=2,
            article_max_sections=5,
        ),
    )
    return Config(
        channels=[],
        settings=settings,
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        openai_api_key="key",
        log_level="INFO",
    )


@pytest.mark.unit
async def test_event_first_article_generation_single_call_and_clean_markdown() -> None:
    config = _make_article_config()
    logger = logging.getLogger("test")
    generator = ArticleGenerator(config=config, logger=logger)

    evi1 = PublicationEvidence(
        evidence_id="story:1:evidence:0:frag:101",
        story_id=1,
        text="Завершена замена 500 метров водопроводных труб",
        source_text="Завершена замена 500 метров водопроводных труб",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="ref-1",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=_NOW,
    )
    evi2 = PublicationEvidence(
        evidence_id="story:2:evidence:0:frag:201",
        story_id=2,
        text="В художественном музее открылась выставка картин",
        source_text="В художественном музее открылась выставка картин",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=201,
        source_ref="ref-2",
        source_id=2,
        source_item_id=2,
        source_role="community",
        observed_at=_NOW,
    )

    art_ctx = build_article_editorial_context(
        cards=[],
        evidence_items=[evi1, evi2],
        operational_observations=[],
    )

    analysis = EditorialAnalysis(
        cards=[],
        article_context=art_ctx,
    )
    frozen_input = FrozenEditorialInput(
        analysis=analysis,
        writer_bundle=PreparedBundle(
            records={}, prompt_text="", total_messages=0, candidate_count=0
        ),
        run_id=1,
    )

    # Mock provider response with valid JSON draft
    llm_draft_response = {
        "title": "Замена водопроводных труб и выставка картин",
        "title_support_ids": ["story:1:evidence:0:frag:101", "story:2:evidence:0:frag:201"],
        "title_claims": [
            {
                "text": "Завершена замена 500 метров водопроводных труб",
                "cited_support_ids": ["story:1:evidence:0:frag:101"],
            },
            {
                "text": "В художественном музее открылась выставка картин",
                "cited_support_ids": ["story:2:evidence:0:frag:201"],
            },
        ],
        "lead": "В городе завершена замена 500 метров водопроводных труб и открылась выставка картин в музее.",
        "lead_support_ids": ["story:1:evidence:0:frag:101", "story:2:evidence:0:frag:201"],
        "lead_claims": [
            {
                "text": "Завершена замена 500 метров водопроводных труб",
                "cited_support_ids": ["story:1:evidence:0:frag:101"],
            },
            {
                "text": "В художественном музее открылась выставка картин",
                "cited_support_ids": ["story:2:evidence:0:frag:201"],
            },
        ],
        "sections": [
            {
                "heading": "Водоснабжение",
                "heading_support_ids": ["story:1:evidence:0:frag:101"],
                "heading_claims": [
                    {
                        "text": "Замена водопроводных труб",
                        "cited_support_ids": ["story:1:evidence:0:frag:101"],
                    }
                ],
                "paragraphs": [
                    {
                        "text": "Завершена замена 500 метров водопроводных труб на городских сетях.",
                        "cited_support_ids": ["story:1:evidence:0:frag:101"],
                        "claims": [
                            {
                                "text": "Завершена замена 500 метров водопроводных труб",
                                "cited_support_ids": ["story:1:evidence:0:frag:101"],
                            }
                        ],
                    },
                ],
            },
            {
                "heading": "Культурные события",
                "heading_support_ids": ["story:2:evidence:0:frag:201"],
                "heading_claims": [
                    {
                        "text": "В художественном музее открылась выставка картин",
                        "cited_support_ids": ["story:2:evidence:0:frag:201"],
                    }
                ],
                "paragraphs": [
                    {
                        "text": "В художественном музее открылась новая выставка картин.",
                        "cited_support_ids": ["story:2:evidence:0:frag:201"],
                        "claims": [
                            {
                                "text": "В художественном музее открылась выставка картин",
                                "cited_support_ids": ["story:2:evidence:0:frag:201"],
                            }
                        ],
                    },
                ],
            },
        ],
    }

    mock_provider = AsyncMock()
    mock_provider.chat_completion.return_value = json.dumps(llm_draft_response)
    generator.provider = mock_provider

    title, lead, body = await generator.generate_from_frozen_input(frozen_input)

    # 1. Exactly 1 chat completion call
    assert mock_provider.chat_completion.call_count == 1

    # 2. Result is properly parsed and formatted
    assert title == "Замена водопроводных труб и выставка картин"
    assert "В городе завершена замена 500 метров водопроводных труб" in lead
    assert "## Водоснабжение" in body
    assert "## Культурные события" in body

    # 3. Clean markdown without internal IDs
    assert "story:" not in body
    assert "frag:" not in body
    assert "evidence:" not in body


@pytest.mark.unit
async def test_event_first_article_generation_falls_back_on_invalid_draft_without_retry() -> None:
    config = _make_article_config()
    logger = logging.getLogger("test")
    generator = ArticleGenerator(config=config, logger=logger)

    evi1 = PublicationEvidence(
        evidence_id="story:1:evidence:0:frag:101",
        story_id=1,
        text="Факт водопровода",
        source_text="Факт водопровода",
        kind="established_fact",
        publication_use="PUBLISH",
        fragment_id=101,
        source_ref="ref-1",
        source_id=1,
        source_item_id=1,
        source_role="official",
        observed_at=_NOW,
    )

    from src.editorial_models import StoryCard

    card = StoryCard(
        id="1",
        topic="Заголовок кандидата",
        importance="medium",
        summary="Краткая сводка о водопроводе",
    )
    art_ctx = build_article_editorial_context(
        cards=[card],
        evidence_items=[evi1],
        operational_observations=[],
    )
    analysis = EditorialAnalysis(cards=[], article_context=art_ctx)
    frozen_input = FrozenEditorialInput(
        analysis=analysis,
        writer_bundle=PreparedBundle(
            records={}, prompt_text="", total_messages=0, candidate_count=0
        ),
        run_id=1,
    )

    # Provider throws or returns invalid JSON
    mock_provider = AsyncMock()
    mock_provider.chat_completion.side_effect = RuntimeError("AI server error")
    generator.provider = mock_provider

    title, lead, body = await generator.generate_from_frozen_input(frozen_input)

    # Assert exactly 1 attempt was made (no infinite retries)
    assert mock_provider.chat_completion.call_count == 1
    # Fallback produced deterministic text from article context
    assert title == "Заголовок кандидата"
    assert "## События и факты" in body
    assert "Факт водопровода" in body
