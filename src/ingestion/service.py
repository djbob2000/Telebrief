"""Transactional ingestion service: one collection batch, one transaction.

The service is the only ingestion component that owns transaction bounds:
``ingest_batch`` wraps a single unit-of-work transaction around
``ingest_batch_in_transaction``, which never commits and exists so later
provider-specific application services can co-commit provider coverage state
with generic source history. Collectors finish their network work in
``scan()`` before persistence starts; nothing provider-specific happens here.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from src.db.uow import DatabaseUnitOfWork
from src.domain.ingestion import SourceItem
from src.ingestion.models import CollectionBatch, CollectionOutcome, CollectionTrigger
from src.ingestion.repository import IngestionRepository


@dataclass(frozen=True)
class IngestionResult:
    """Outcome counters of one persisted CollectionBatch."""

    collection_run_id: int
    new_items: int
    new_revisions: int
    new_revision_ids: tuple[int, ...]


class IngestionService:
    """Persist collector batches atomically into generic source history."""

    def __init__(self, uow: DatabaseUnitOfWork, repo: IngestionRepository) -> None:
        self.uow = uow
        self.repo = repo

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
        return IngestionResult(
            collection_run_id=run.id,
            new_items=new_items,
            new_revisions=len(new_revision_ids),
            new_revision_ids=tuple(new_revision_ids),
        )
