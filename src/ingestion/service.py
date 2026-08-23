"""Transactional ingestion service: one collection batch, one transaction.

The service is the only ingestion component that owns transaction bounds:
``ingest_batch`` wraps a single unit-of-work transaction around
``ingest_batch_in_transaction``, which never commits and exists so later
provider-specific application services can co-commit provider coverage state
with generic source history. Collectors finish their network work in
``scan()`` before persistence starts; nothing provider-specific happens here.

Plan 3 relevance wiring: before the transaction ends, every newly-created
revision fans out to one exact-policy ``evaluate_relevance`` deferral per
bound edition — resolved on the SAME connection, so revisions, their current
relevance policy, and their queued jobs commit (or roll back) together. The
policy id is fixed at queue time; Procrastinate retries never re-resolve it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import psycopg

from src.db.uow import DatabaseUnitOfWork
from src.domain.ingestion import SourceItem
from src.ingestion.models import CollectionBatch, CollectionOutcome, CollectionTrigger
from src.ingestion.repository import IngestionRepository

if TYPE_CHECKING:
    from src.processing.relevance import IngestionRelevanceWiring


@dataclass(frozen=True)
class IngestionResult:
    """Outcome counters of one persisted CollectionBatch."""

    collection_run_id: int
    new_items: int
    new_revisions: int
    new_revision_ids: tuple[int, ...]


class IngestionService:
    """Persist collector batches atomically into generic source history."""

    def __init__(
        self,
        uow: DatabaseUnitOfWork,
        repo: IngestionRepository,
        *,
        relevance_wiring: "IngestionRelevanceWiring | None" = None,
    ) -> None:
        self.uow = uow
        self.repo = repo
        self._relevance_wiring = relevance_wiring

    @property
    def relevance_wiring(self) -> "IngestionRelevanceWiring":
        """Lazily built default wiring (lenient config identity resolution)."""
        if self._relevance_wiring is None:
            from src.processing.relevance import IngestionRelevanceWiring

            self._relevance_wiring = IngestionRelevanceWiring.create()
        return self._relevance_wiring

    async def ingest_batch(
        self,
        source_id: int,
        trigger: CollectionTrigger,
        batch: CollectionBatch,
    ) -> IngestionResult:
        """Ingest one batch inside a single committed transaction."""
        async with self.uow.transaction() as conn:
            return await self.ingest_batch_in_transaction(
                conn, source_id=source_id, trigger=trigger, batch=batch
            )

    async def ingest_batch_in_transaction(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_id: int,
        trigger: CollectionTrigger,
        batch: CollectionBatch,
    ) -> IngestionResult:
        """Persist the batch on ``conn`` without committing; reuses caller's tx."""
        run = await self.repo.start_run(
            conn, source_id=source_id, trigger=trigger.value, started_at=batch.started_at
        )
        new_items = 0
        new_revision_ids: list[int] = []
        current_revision_by_external_id: dict[str, int] = {}

        item_by_external_id: dict[str, SourceItem] = {}
        for observation in batch.items:
            item, created = await self.repo.get_or_create_item_shell(conn, source_id, observation)
            item_by_external_id[observation.external_id] = item
            new_items += int(created)

        for observation in batch.items:
            item = item_by_external_id[observation.external_id]
            await self.repo.ensure_relationships(
                conn,
                source_id=source_id,
                item_id=item.id,
                parent_external_id=observation.parent_external_id,
                root_external_id=observation.root_external_id,
            )
            revision = await self.repo.insert_revision_if_changed(
                conn, item.id, observation, collected_at=observation.observed_at
            )
            current = revision or await self.repo.get_latest_revision(conn, item.id)
            if current is None:
                raise RuntimeError(f"item {observation.external_id!r} has no revision after ingest")
            current_revision_by_external_id[observation.external_id] = current.id
            if revision is not None:
                new_revision_ids.append(revision.id)

        for asset in batch.assets:
            revision_id = current_revision_by_external_id[asset.item_external_id]
            await self.repo.upsert_asset_for_revision(conn, revision_id, asset)

        for state_event in batch.state_events:
            await self.repo.insert_state_event(conn, source_id, state_event)

        await self.repo.update_checkpoint(
            conn,
            source_id=source_id,
            adapter_state=batch.adapter_state,
            last_scan_at=batch.completed_at,
            last_success_at=batch.completed_at
            if batch.outcome == CollectionOutcome.SUCCESS
            else None,
        )
        await self.repo.finish_run(
            conn,
            run_id=run.id,
            outcome=batch.outcome.value,
            completed_at=batch.completed_at,
            error_kind=batch.error_kind,
        )
        await self._defer_relevance_jobs(
            conn, source_id=source_id, new_revision_ids=new_revision_ids
        )
        return IngestionResult(
            collection_run_id=run.id,
            new_items=new_items,
            new_revisions=len(new_revision_ids),
            new_revision_ids=tuple(new_revision_ids),
        )

    async def _defer_relevance_jobs(
        self,
        conn: psycopg.AsyncConnection,
        *,
        source_id: int,
        new_revision_ids: list[int],
    ) -> None:
        """Resolve each bound edition's exact current policy and defer the job.

        The evaluate_relevance task import is lazy on purpose: importing it at
        module scope would build the Procrastinate app (and demand database
        config) for every consumer of this service.
        """
        if not new_revision_ids:
            return
        wiring = self.relevance_wiring
        edition_ids = await self.repo.list_source_edition_ids(conn, source_id)
        if not edition_ids:
            return

        from src.jobs.processing import evaluate_relevance

        relevance_policy_service = wiring.policy_service
        current_config_hash = wiring.config_hash
        for revision_id in new_revision_ids:
            for edition_id in edition_ids:
                policy = await relevance_policy_service.ensure_current(
                    conn,
                    edition_id=edition_id,
                    config_hash=current_config_hash,
                    prompt_version=wiring.prompt_version,
                )
                await evaluate_relevance.configure(connection=conn).defer_async(
                    source_item_revision_id=revision_id,
                    edition_id=edition_id,
                    policy_id=policy.id,
                )
