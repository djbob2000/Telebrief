"""Publication refresh readiness state machine."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import psycopg

from src.publication.readiness_repository import (
    PublicationReadinessRepository,
    PublicationRefreshRun,
)

logger = logging.getLogger(__name__)

ReadinessStatus = Literal[
    "collecting",
    "processing",
    "ready_for_preparation",
    "fallback_ready",
    "failed",
]


@dataclass(frozen=True)
class PublicationReadinessDecision:
    status: ReadinessStatus
    source_cutoff_at: dt.datetime | None
    historical_snapshot_at: dt.datetime | None


class PublicationReadinessService:
    """Coordinate durable collection and Event-First processing barriers."""

    def __init__(self, repo: PublicationReadinessRepository | None = None) -> None:
        self.repo = repo or PublicationReadinessRepository()

    async def create_refresh(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        publication_type: str,
        slot_at: dt.datetime,
        source_ids: Sequence[int],
        requested_at: dt.datetime,
        deadline_minutes: int,
    ) -> PublicationRefreshRun:
        return await self.repo.get_or_create_refresh_run(
            conn,
            edition_id=edition_id,
            publication_type=publication_type,
            slot_at=slot_at,
            requested_at=requested_at,
            normal_source_cutoff_at=slot_at,
            fallback_snapshot_at=requested_at,
            deadline_at=slot_at + dt.timedelta(minutes=deadline_minutes),
            source_ids=source_ids,
        )

    async def reconcile(
        self,
        conn: psycopg.AsyncConnection,
        refresh_run_id: int,
        *,
        now: dt.datetime,
        on_deadline: Literal["fallback", "fail_closed"],
    ) -> PublicationReadinessDecision:
        refresh = await self.repo.get_refresh_run(conn, refresh_run_id)
        if refresh is None:
            raise ValueError(f"refresh run {refresh_run_id} not found")

        if refresh.status == "ready_for_preparation":
            return self._normal_decision(refresh)
        if refresh.status == "fallback_ready":
            return self._fallback_decision(refresh)
        if refresh.status == "failed":
            return PublicationReadinessDecision("failed", None, None)
        if refresh.status in {"preparing", "publication_queued"}:
            raise ValueError(f"refresh run {refresh.id} is not reconcilable: {refresh.status}")

        sources = await self.repo.reconcile_qualifying_collection_runs(conn, refresh.id)
        all_sources_succeeded = all(source.status == "succeeded" for source in sources)

        if all_sources_succeeded:
            unprocessed = await self.repo.count_unprocessed_refresh_revisions(conn, refresh.id)
            if unprocessed == 0:
                await self._transition(
                    conn,
                    refresh.id,
                    status="ready_for_preparation",
                    collection_ready_at=refresh.collection_ready_at or now,
                    processing_ready_at=now,
                    now=now,
                )
                return self._normal_decision(refresh)
            await self._transition(
                conn,
                refresh.id,
                status="processing",
                collection_ready_at=refresh.collection_ready_at or now,
                now=now,
            )
            return PublicationReadinessDecision("processing", None, None)

        if now < refresh.deadline_at:
            await self._transition(conn, refresh.id, status="collecting", now=now)
            return PublicationReadinessDecision("collecting", None, None)

        if on_deadline == "fallback":
            await self._transition(conn, refresh.id, status="fallback_ready", now=now)
            return self._fallback_decision(refresh)

        await self._transition(
            conn,
            refresh.id,
            status="failed",
            error_kind="readiness_deadline",
            now=now,
        )
        return PublicationReadinessDecision("failed", None, None)

    @staticmethod
    def _normal_decision(refresh: PublicationRefreshRun) -> PublicationReadinessDecision:
        return PublicationReadinessDecision(
            "ready_for_preparation", refresh.normal_source_cutoff_at, None
        )

    @staticmethod
    def _fallback_decision(refresh: PublicationRefreshRun) -> PublicationReadinessDecision:
        return PublicationReadinessDecision(
            "fallback_ready", refresh.fallback_snapshot_at, refresh.fallback_snapshot_at
        )

    async def _transition(
        self,
        conn: psycopg.AsyncConnection,
        refresh_run_id: int,
        *,
        status: str,
        now: dt.datetime,
        error_kind: str | None = None,
        collection_ready_at: dt.datetime | None = None,
        processing_ready_at: dt.datetime | None = None,
    ) -> None:
        await self.repo.transition_refresh(
            conn,
            refresh_run_id,
            status=status,
            error_kind=error_kind,
            collection_ready_at=collection_ready_at,
            processing_ready_at=processing_ready_at,
        )
        refresh = await self.repo.get_refresh_run(conn, refresh_run_id)
        if refresh is None:
            return
        extra: dict[str, object] = {
            "edition_id": refresh.edition_id,
            "refresh_run_id": refresh.id,
            "status": status,
            "source_cutoff_at": refresh.normal_source_cutoff_at.isoformat(),
        }
        if refresh.collection_ready_at is not None:
            extra["collection_ready_ms"] = max(
                0,
                int((refresh.collection_ready_at - refresh.requested_at).total_seconds() * 1000),
            )
        if refresh.processing_ready_at is not None:
            extra["processing_ready_ms"] = max(
                0,
                int((refresh.processing_ready_at - refresh.requested_at).total_seconds() * 1000),
            )
        if error_kind is not None:
            extra["retry_error_kind"] = error_kind
        logger.info("publication_refresh_transition", extra=extra)
