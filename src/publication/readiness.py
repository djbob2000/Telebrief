"""Publication refresh readiness state machine."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Sequence
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
    "ready_waiting_slot",
    "ready_for_preparation",
    "failed",
]


@dataclass(frozen=True)
class PublicationReadinessDecision:
    status: ReadinessStatus
    source_cutoff_at: dt.datetime | None
    failure_kind: str | None = None
    authority_gap_story_ids: tuple[int, ...] = ()


AuthorityGapChecker = Callable[
    [psycopg.AsyncConnection, PublicationRefreshRun, dt.datetime],
    Awaitable[Sequence[int]],
]


class PublicationReadinessService:
    """Coordinate durable collection and Event-First processing barriers."""

    def __init__(
        self,
        repo: PublicationReadinessRepository | None = None,
        *,
        authority_gap_checker: AuthorityGapChecker | None = None,
    ) -> None:
        self.repo = repo or PublicationReadinessRepository()
        self.authority_gap_checker = authority_gap_checker

    async def create_refresh(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        publication_type: str,
        slot_at: dt.datetime,
        source_ids: Sequence[int],
        requested_at: dt.datetime,
        trigger: Literal["manual", "scheduled"],
        request_key: str,
        freshness_cutoff_at: dt.datetime,
        requested_by_user_id: int | None,
        lookback_hours: int,
        deadline_minutes: int,
    ) -> PublicationRefreshRun:
        return await self.repo.get_or_create_refresh_run(
            conn,
            edition_id=edition_id,
            publication_type=publication_type,
            slot_at=slot_at,
            requested_at=requested_at,
            trigger=trigger,
            request_key=request_key,
            freshness_cutoff_at=freshness_cutoff_at,
            deadline_at=slot_at + dt.timedelta(minutes=deadline_minutes),
            requested_by_user_id=requested_by_user_id,
            lookback_hours=lookback_hours,
            source_ids=source_ids,
        )

    async def reconcile(
        self,
        conn: psycopg.AsyncConnection,
        refresh_run_id: int,
        *,
        now: dt.datetime,
    ) -> PublicationReadinessDecision:
        refresh = await self.repo.get_refresh_run(conn, refresh_run_id)
        if refresh is None:
            raise ValueError(f"refresh run {refresh_run_id} not found")

        if refresh.status == "ready_for_preparation":
            # A scheduled intent may have been left in this state by a
            # worker handoff immediately before its slot. Re-apply the slot
            # barrier instead of allowing an early preparation claim.
            if refresh.trigger == "scheduled" and now < refresh.slot_at:
                await self._transition(
                    conn,
                    refresh.id,
                    status="ready_waiting_slot",
                    now=now,
                    collection_ready_at=refresh.collection_ready_at,
                    processing_ready_at=refresh.processing_ready_at,
                )
                return PublicationReadinessDecision("ready_waiting_slot", None)
            return self._normal_decision(refresh)
        if refresh.status == "failed":
            return PublicationReadinessDecision("failed", None, refresh.error_kind)
        if refresh.status in {"preparing", "publication_queued"}:
            raise ValueError(f"refresh run {refresh.id} is not reconcilable: {refresh.status}")
        if refresh.status == "ready_waiting_slot" and now < refresh.slot_at:
            return PublicationReadinessDecision("ready_waiting_slot", None)

        sources = await self.repo.reconcile_qualifying_collection_runs(conn, refresh.id)
        if not sources:
            await self._transition(
                conn, refresh.id, status="failed", error_kind="no_enabled_sources", now=now
            )
            return PublicationReadinessDecision("failed", None, "no_enabled_sources")

        terminal_outcome = self._terminal_source_outcome(sources)
        if terminal_outcome is not None:
            error_kind = f"source_{terminal_outcome}"
            await self._transition(
                conn, refresh.id, status="failed", error_kind=error_kind, now=now
            )
            return PublicationReadinessDecision("failed", None, error_kind)

        if now >= refresh.deadline_at:
            await self._transition(
                conn,
                refresh.id,
                status="failed",
                error_kind="readiness_deadline",
                now=now,
            )
            return PublicationReadinessDecision("failed", None, "readiness_deadline")

        all_sources_succeeded = all(source.status == "succeeded" for source in sources)

        if all_sources_succeeded:
            unprocessed = await self.repo.count_unprocessed_refresh_revisions(conn, refresh.id)
            if unprocessed == 0:
                authority_gap_story_ids: tuple[int, ...] = ()
                if self.authority_gap_checker is not None:
                    authority_gap_story_ids = tuple(
                        int(story_id)
                        for story_id in await self.authority_gap_checker(conn, refresh, now)
                    )
                if authority_gap_story_ids:
                    await self._transition(
                        conn,
                        refresh.id,
                        status="processing",
                        collection_ready_at=refresh.collection_ready_at or now,
                        now=now,
                    )
                    return PublicationReadinessDecision(
                        "processing",
                        None,
                        authority_gap_story_ids=authority_gap_story_ids,
                    )
                await self._transition(
                    conn,
                    refresh.id,
                    status="ready_for_preparation",
                    collection_ready_at=refresh.collection_ready_at or now,
                    processing_ready_at=now,
                    now=now,
                )
                if refresh.trigger == "scheduled" and now < refresh.slot_at:
                    await self._transition(
                        conn,
                        refresh.id,
                        status="ready_waiting_slot",
                        collection_ready_at=refresh.collection_ready_at or now,
                        processing_ready_at=now,
                        now=now,
                    )
                    return PublicationReadinessDecision("ready_waiting_slot", None)
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

        await self._transition(
            conn,
            refresh.id,
            status="failed",
            error_kind="readiness_deadline",
            now=now,
        )
        return PublicationReadinessDecision("failed", None, "readiness_deadline")

    @staticmethod
    def _normal_decision(refresh: PublicationRefreshRun) -> PublicationReadinessDecision:
        return PublicationReadinessDecision(
            "ready_for_preparation", refresh.normal_source_cutoff_at
        )

    @staticmethod
    def _terminal_source_outcome(sources: Sequence) -> str | None:
        retryable = {"transient", "rate_limited"}
        non_terminal = {None, "success", "skipped", *retryable}
        for source in sources:
            outcome = source.collection_outcome
            if outcome not in non_terminal:
                return str(outcome)
        return None

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
        if status == "ready_waiting_slot":
            extra["waiting_for_slot_ms"] = max(
                0, int((refresh.slot_at - refresh.requested_at).total_seconds() * 1000)
            )
        if error_kind is not None:
            extra["retry_error_kind"] = error_kind
        logger.info("publication_refresh_transition", extra=extra)
