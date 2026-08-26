"""Historical context retrieval service (Temporal RAG) for editorial background."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from src.db.uow import DatabaseUnitOfWork
from src.editorial_models import EditorialAnalysis
from src.embedding_providers import EmbeddingProvider
from src.repositories.archive import ArchiveMatchCandidate, ArchiveRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoryHistoricalBackground:
    """Historical context retrieved for one Story Card."""

    story_id: str
    topic: str
    candidates: list[ArchiveMatchCandidate]

    def render_markdown(self) -> str:
        """Render a readable historical reference section for the writer."""
        if not self.candidates:
            return ""

        lines = [f"### Справка к сюжету {self.story_id}: {self.topic}"]
        for idx, item in enumerate(self.candidates, start=1):
            date_str = item.published_at.strftime("%d.%m.%Y")
            excerpt = item.content.strip().replace("\n", " ")
            if len(excerpt) > 350:
                excerpt = excerpt[:347] + "..."
            lines.append(
                f"{idx}. [Архив: {date_str}, {item.source_name}] **{item.title}**\n"
                f"   Контекст: {excerpt}"
            )
        return "\n".join(lines)


class HistoricalContextRetriever:
    """Retrieves relevant historical news context from the archive database for current stories."""

    def __init__(
        self,
        uow: DatabaseUnitOfWork,
        embedding_provider: EmbeddingProvider,
        *,
        model: str = "qwen/qwen3-embedding-8b",
        dimensions: int = 1536,
        repository: ArchiveRepository | None = None,
        lookback_cutoff_days: int = 7,
    ):
        self.uow = uow
        self.embedding_provider = embedding_provider
        self.model = model
        self.dimensions = dimensions
        self.repository = repository or ArchiveRepository()
        self.lookback_cutoff_days = lookback_cutoff_days

    async def retrieve_for_stories(
        self,
        analysis: EditorialAnalysis,
        *,
        edition_slug: str = "berdyansk",
        limit_per_story: int = 2,
    ) -> list[StoryHistoricalBackground]:
        """Search historical background for each Story Card in the editorial analysis."""
        if not analysis.cards:
            return []

        cutoff_date = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=self.lookback_cutoff_days
        )
        results: list[StoryHistoricalBackground] = []

        for card in analysis.cards:
            query_text = f"{card.topic}\n{card.summary}"
            try:
                query_vector = await self.embedding_provider.embed(
                    query_text,
                    purpose="claim_query",
                    model=self.model,
                    dimensions=self.dimensions,
                )
                async with self.uow.transaction() as conn:
                    candidates = await self.repository.search_similar(
                        conn,
                        query_vector=query_vector,
                        model=self.model,
                        dimensions=self.dimensions,
                        edition_slug=edition_slug,
                        before_date=cutoff_date,
                        max_distance=0.55,
                        limit=limit_per_story,
                    )
                if candidates:
                    logger.info(
                        "Found %d historical archive references for story %s (%s)",
                        len(candidates),
                        card.id,
                        card.topic,
                    )
                    results.append(
                        StoryHistoricalBackground(
                            story_id=card.id,
                            topic=card.topic,
                            candidates=candidates,
                        )
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to retrieve historical background for story %s: %s",
                    card.id,
                    exc,
                )

        return results

    def render_context(self, backgrounds: list[StoryHistoricalBackground]) -> str:
        """Render all story historical backgrounds into a structured prompt block."""
        rendered = [b.render_markdown() for b in backgrounds if b.candidates]
        if not rendered:
            return ""
        return "\n\n".join(rendered)
