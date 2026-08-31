"""Tests for historical context retrieval and prompt rendering."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.editorial_models import EditorialAnalysis, StoryCard
from src.historical_context import HistoricalContextRetriever, StoryHistoricalBackground
from src.repositories.archive import ArchiveMatchCandidate


def test_story_historical_background_rendering():
    candidate = ArchiveMatchCandidate(
        article_id=1,
        title="Авария на подстанции в Бердянске",
        content="В 2023 году на центральной подстанции произошла крупная авария сетей.",
        published_at=dt.datetime(2023, 11, 15, 12, 0, tzinfo=dt.timezone.utc),
        source_name="Бердянск 24",
        source_url="https://example.com/news/1",
        vector_distance=0.25,
    )
    bg = StoryHistoricalBackground(
        story_id="SC001",
        topic="Энергетический кризис",
        candidates=[candidate],
    )
    markdown = bg.render_markdown()
    assert "Справка к сюжету SC001: Энергетический кризис" in markdown
    assert "15.11.2023" in markdown
    assert "Бердянск 24" in markdown
    assert "Авария на подстанции" in markdown


@pytest.mark.asyncio
async def test_retriever_for_stories_success():
    uow = MagicMock()
    conn_mock = AsyncMock()
    uow.transaction.return_value.__aenter__.return_value = conn_mock

    emb_provider = AsyncMock()
    emb_provider.embed.return_value = [0.1] * 1536

    repo = AsyncMock()
    candidate = ArchiveMatchCandidate(
        article_id=10,
        title="Историческая новость",
        content="Текст архивной статьи",
        published_at=dt.datetime(2024, 1, 10, tzinfo=dt.timezone.utc),
        source_name="Архив",
        source_url=None,
        vector_distance=0.15,
    )
    repo.search_similar.return_value = [candidate]

    retriever = HistoricalContextRetriever(
        uow=uow,
        embedding_provider=emb_provider,
        repository=repo,
    )

    card = StoryCard(
        id="SC001",
        topic="Проблемы со светом",
        importance="high",
        summary="В городе блэкаут",
    )
    analysis = EditorialAnalysis(cards=[card])

    backgrounds = await retriever.retrieve_for_stories(analysis)
    assert len(backgrounds) == 1
    assert backgrounds[0].story_id == "SC001"
    assert len(backgrounds[0].candidates) == 1

    rendered = retriever.render_context(backgrounds)
    assert "Справка к сюжету SC001" in rendered
    assert "Историческая новость" in rendered
