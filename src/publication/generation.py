"""Publication generation service producing immutable publications and generation attempt history (Plan 4 Task 5)."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import psycopg

from src.article_generator import ArticleGenerator
from src.config_loader import Config
from src.db.uow import DatabaseUnitOfWork
from src.publication.editorial_adapter import (
    DatabaseGenerationAttemptObserver,
    KnowledgeEditorialAdapter,
)
from src.publication.models import Publication
from src.publication.repository import PublicationRepository

logger = logging.getLogger(__name__)


class PublicationGenerationService:
    """Orchestrates publication generation from frozen inputs with attempt audit."""

    def __init__(
        self,
        *,
        uow: DatabaseUnitOfWork,
        config: Config | None = None,
        repo: PublicationRepository | None = None,
        adapter: KnowledgeEditorialAdapter | None = None,
        generator: ArticleGenerator | None = None,
    ) -> None:
        from src.config_loader import load_config

        self.uow = uow
        self.config = config or load_config()
        self.repo = repo or PublicationRepository()
        self.adapter = adapter or KnowledgeEditorialAdapter(uow=uow, repo=self.repo)
        self.generator = generator or ArticleGenerator(config=self.config, logger=logger)

    async def generate(
        self,
        run_id: int,
        *,
        defer_delivery: bool = True,
        publication_metadata: dict[str, Any] | None = None,
    ) -> Publication:
        async with self.uow.transaction() as conn:
            run = await self.repo.lock_run(conn, run_id)
            if run is None:
                raise ValueError(f"publication run {run_id} not found")

            if run.status == "succeeded":
                existing = await self.repo.get_publication_by_run_id(conn, run_id)
                if existing is not None:
                    return existing

            if run.status not in ("selected_inputs_sealed", "generating"):
                raise RuntimeError(
                    f"cannot generate for publication run {run_id} in status '{run.status}'"
                )

            await self.repo.transition_run(conn, run_id, "generating")

        # Build deterministic frozen input from sealed knowledge
        frozen = await self.adapter.build(run_id)

        # Observer records each attempt into publication_generation_attempts
        observer = DatabaseGenerationAttemptObserver(uow=self.uow, run_id=run_id, repo=self.repo)

        try:
            if run.publication_type in ("digest_grouped", "digest_channel", "digest"):
                from src.publication.renderers import PublicationDigestRenderer

                renderer = PublicationDigestRenderer(
                    output_language=getattr(self.config.settings, "output_language", "Russian"),
                    use_emojis=getattr(self.config.settings, "use_emojis", True),
                    include_statistics=getattr(self.config.settings, "include_statistics", True),
                    custom_rubrics=getattr(self.config.settings, "digest_groups", None),
                )
                att_id = await observer.attempt_started(
                    "story_renderer_fallback", metadata={"renderer": run.publication_type}
                )
                if run.publication_type == "digest_channel":
                    title, lead, body = renderer.render_channel_digest(
                        frozen, snapshot_at=run.snapshot_at
                    )
                else:
                    title, lead, body = renderer.render_grouped_digest(
                        frozen, snapshot_at=run.snapshot_at
                    )
                await observer.attempt_finished(att_id, "succeeded")
            else:
                title, lead, body = await self.generator.generate_from_frozen_input(
                    frozen, attempt_observer=observer
                )
        except Exception as exc:
            logger.error("generation failed completely for run %s: %s", run_id, exc)
            async with self.uow.transaction() as conn:
                await self.repo.transition_run(
                    conn, run_id, "failed", error_kind=type(exc).__name__
                )
            raise

        winning_attempt = observer.last_successful_content_attempt
        if winning_attempt is None:
            # Fallback: find the latest succeeded attempt from DB
            async with self.uow.transaction() as conn:
                cursor = await conn.execute(
                    """
                    SELECT id, publication_run_id, attempt_no, kind, status, error_kind,
                           provider, model, prompt_hash, metadata, started_at, completed_at
                    FROM publication_generation_attempts
                    WHERE publication_run_id = %s AND status = 'succeeded'
                    ORDER BY attempt_no DESC LIMIT 1
                    """,
                    (run_id,),
                )
                row = await cursor.fetchone()
                if row is not None:
                    from src.publication.models import PublicationGenerationAttempt

                    winning_attempt = PublicationGenerationAttempt.from_row(row)

        if winning_attempt is None:
            raise RuntimeError(f"no successful generation attempt recorded for run {run_id}")

        meta: dict[str, Any] = {"winning_kind": winning_attempt.kind}
        if publication_metadata:
            meta.update(publication_metadata)

        async with self.uow.transaction() as conn:
            pub = await self.repo.create_publication(
                conn,
                run_id=run_id,
                winning_attempt_id=winning_attempt.id,
                publication_type=run.publication_type,
                title=title,
                lead=lead,
                body=body,
                metadata=meta,
            )
            await self.repo.transition_run(
                conn, run_id, "succeeded", completed_at=dt.datetime.now(dt.timezone.utc)
            )
            if defer_delivery:
                await self._defer_delivery_payloads(conn, pub.id)
            return pub

    async def _defer_delivery_payloads(
        self, conn: psycopg.AsyncConnection, publication_id: int
    ) -> None:
        try:
            from src.jobs.publication import prepare_delivery_payloads

            await prepare_delivery_payloads.configure(connection=conn).defer_async(
                publication_id=publication_id
            )
        except Exception as err:
            # Re-raise: the transaction must roll back so the run is not left
            # succeeded with no delivery job ever queued.
            logger.error(
                "could not defer prepare_delivery_payloads for publication %s: %s",
                publication_id,
                err,
            )
            raise
