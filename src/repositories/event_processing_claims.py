"""Fenced durable claims for Event-First processing work."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Sequence, cast

import psycopg


@dataclass(frozen=True)
class EventProcessingCycleClaim:
    """The fencing token and durable counters for one edition cycle."""

    edition_id: int
    claim_token: str
    owner_id: str
    lease_expires_at: dt.datetime
    rich_calls_started: int
    triage_split_calls_started: int


@dataclass(frozen=True)
class EventProcessingStageClaim:
    """A fencing token for one Story assignment and processing stage."""

    story_id: int
    latest_assignment_id: int
    stage: str
    claim_token: str
    owner_id: str
    lease_expires_at: dt.datetime


class EventProcessingClaimRepository:
    """Acquire, renew, spend, and release PostgreSQL-backed processing claims."""

    _VALID_STAGES = frozenset({"triage", "analysis"})

    @staticmethod
    def _validate_ttl(ttl_seconds: int) -> None:
        if isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise ValueError("claim lease ttl_seconds must be a positive integer")

    @classmethod
    def _validate_stage(cls, stage: str) -> None:
        if stage not in cls._VALID_STAGES:
            raise ValueError(f"unsupported Event-First processing stage: {stage!r}")

    @staticmethod
    def _cycle_from_row(row: Sequence[object]) -> EventProcessingCycleClaim:
        return EventProcessingCycleClaim(
            edition_id=cast(int, row[0]),
            claim_token=str(row[1]),
            owner_id=str(row[2]),
            lease_expires_at=cast(dt.datetime, row[3]),
            rich_calls_started=cast(int, row[4]),
            triage_split_calls_started=cast(int, row[5]),
        )

    @staticmethod
    def _stage_from_row(row: Sequence[object]) -> EventProcessingStageClaim:
        return EventProcessingStageClaim(
            story_id=cast(int, row[0]),
            latest_assignment_id=cast(int, row[1]),
            stage=str(row[2]),
            claim_token=str(row[3]),
            owner_id=str(row[4]),
            lease_expires_at=cast(dt.datetime, row[5]),
        )

    async def acquire_cycle(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        owner_id: str,
        ttl_seconds: int,
    ) -> EventProcessingCycleClaim | None:
        """Acquire an edition cycle, replacing only an expired claim."""
        self._validate_ttl(ttl_seconds)
        token = uuid.uuid4().hex
        cursor = await conn.execute(
            """
            INSERT INTO event_processing_cycle_leases (
                edition_id, claim_token, owner_id, lease_expires_at,
                rich_calls_started, triage_split_calls_started
            )
            VALUES (%s, %s, %s, now() + make_interval(secs => %s), 0, 0)
            ON CONFLICT (edition_id) DO UPDATE SET
                claim_token = EXCLUDED.claim_token,
                owner_id = EXCLUDED.owner_id,
                lease_expires_at = EXCLUDED.lease_expires_at,
                rich_calls_started = 0,
                triage_split_calls_started = 0,
                updated_at = now()
            WHERE event_processing_cycle_leases.lease_expires_at <= now()
            RETURNING edition_id, claim_token, owner_id, lease_expires_at,
                      rich_calls_started, triage_split_calls_started
            """,
            (edition_id, token, owner_id, ttl_seconds),
        )
        row = await cursor.fetchone()
        return None if row is None else self._cycle_from_row(row)

    async def renew_cycle(
        self,
        conn: psycopg.AsyncConnection,
        claim: EventProcessingCycleClaim,
        *,
        ttl_seconds: int,
    ) -> EventProcessingCycleClaim | None:
        """Renew a live cycle only when its exact fencing token still owns it."""
        self._validate_ttl(ttl_seconds)
        cursor = await conn.execute(
            """
            UPDATE event_processing_cycle_leases
            SET lease_expires_at = now() + make_interval(secs => %s),
                updated_at = now()
            WHERE edition_id = %s
              AND claim_token = %s
              AND lease_expires_at > now()
            RETURNING edition_id, claim_token, owner_id, lease_expires_at,
                      rich_calls_started, triage_split_calls_started
            """,
            (ttl_seconds, claim.edition_id, claim.claim_token),
        )
        row = await cursor.fetchone()
        return None if row is None else self._cycle_from_row(row)

    async def get_cycle(
        self,
        conn: psycopg.AsyncConnection,
        claim: EventProcessingCycleClaim,
    ) -> EventProcessingCycleClaim | None:
        """Read durable cycle counters while fencing by the claim token."""
        cursor = await conn.execute(
            """
            SELECT edition_id, claim_token, owner_id, lease_expires_at,
                   rich_calls_started, triage_split_calls_started
            FROM event_processing_cycle_leases
            WHERE edition_id = %s AND claim_token = %s
            """,
            (claim.edition_id, claim.claim_token),
        )
        row = await cursor.fetchone()
        return None if row is None else self._cycle_from_row(row)

    async def _claim_cycle_slot(
        self,
        conn: psycopg.AsyncConnection,
        claim: EventProcessingCycleClaim,
        *,
        counter: str,
        max_calls: int,
        ttl_seconds: int,
    ) -> bool:
        self._validate_ttl(ttl_seconds)
        if isinstance(max_calls, bool) or max_calls < 0:
            raise ValueError("max_calls must be a non-negative integer")
        if counter not in {"rich_calls_started", "triage_split_calls_started"}:
            raise ValueError(f"unsupported cycle counter: {counter!r}")
        cursor = await conn.execute(
            f"""
            UPDATE event_processing_cycle_leases
            SET {counter} = {counter} + 1,
                lease_expires_at = now() + make_interval(secs => %s),
                updated_at = now()
            WHERE edition_id = %s
              AND claim_token = %s
              AND lease_expires_at > now()
              AND {counter} < %s
            RETURNING {counter}
            """,  # noqa: S608 -- counter is selected from a fixed allowlist
            (ttl_seconds, claim.edition_id, claim.claim_token, max_calls),
        )
        return await cursor.fetchone() is not None

    async def claim_rich_slot(
        self,
        conn: psycopg.AsyncConnection,
        claim: EventProcessingCycleClaim,
        *,
        max_calls: int,
        ttl_seconds: int,
    ) -> bool:
        """Spend one shared Rich Analysis slot while renewing the cycle."""
        return await self._claim_cycle_slot(
            conn,
            claim,
            counter="rich_calls_started",
            max_calls=max_calls,
            ttl_seconds=ttl_seconds,
        )

    async def claim_triage_split_slot(
        self,
        conn: psycopg.AsyncConnection,
        claim: EventProcessingCycleClaim,
        *,
        max_calls: int,
        ttl_seconds: int,
    ) -> bool:
        """Spend one shared Gate split slot while renewing the cycle."""
        return await self._claim_cycle_slot(
            conn,
            claim,
            counter="triage_split_calls_started",
            max_calls=max_calls,
            ttl_seconds=ttl_seconds,
        )

    async def release_cycle(
        self,
        conn: psycopg.AsyncConnection,
        claim: EventProcessingCycleClaim,
    ) -> bool:
        """Release only the cycle row fenced by this claim's token."""
        cursor = await conn.execute(
            """
            DELETE FROM event_processing_cycle_leases
            WHERE edition_id = %s AND claim_token = %s
            """,
            (claim.edition_id, claim.claim_token),
        )
        return cursor.rowcount == 1

    async def try_claim_stage(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        latest_assignment_id: int,
        stage: str,
        owner_id: str,
        ttl_seconds: int,
    ) -> EventProcessingStageClaim | None:
        """Claim an assignment stage, taking over only an expired claim."""
        self._validate_stage(stage)
        self._validate_ttl(ttl_seconds)
        token = uuid.uuid4().hex
        cursor = await conn.execute(
            """
            INSERT INTO story_event_processing_claims (
                story_id, latest_assignment_id, stage, claim_token,
                owner_id, lease_expires_at
            )
            VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))
            ON CONFLICT (story_id, latest_assignment_id, stage) DO UPDATE SET
                claim_token = EXCLUDED.claim_token,
                owner_id = EXCLUDED.owner_id,
                lease_expires_at = EXCLUDED.lease_expires_at,
                updated_at = now()
            WHERE story_event_processing_claims.lease_expires_at <= now()
            RETURNING story_id, latest_assignment_id, stage, claim_token,
                      owner_id, lease_expires_at
            """,
            (story_id, latest_assignment_id, stage, token, owner_id, ttl_seconds),
        )
        row = await cursor.fetchone()
        return None if row is None else self._stage_from_row(row)

    async def renew_stage(
        self,
        conn: psycopg.AsyncConnection,
        claim: EventProcessingStageClaim,
        *,
        ttl_seconds: int,
    ) -> EventProcessingStageClaim | None:
        """Renew a stage claim only when its exact fencing token is live."""
        self._validate_stage(claim.stage)
        self._validate_ttl(ttl_seconds)
        cursor = await conn.execute(
            """
            UPDATE story_event_processing_claims
            SET lease_expires_at = now() + make_interval(secs => %s),
                updated_at = now()
            WHERE story_id = %s
              AND latest_assignment_id = %s
              AND stage = %s
              AND claim_token = %s
              AND lease_expires_at > now()
            RETURNING story_id, latest_assignment_id, stage, claim_token,
                      owner_id, lease_expires_at
            """,
            (
                ttl_seconds,
                claim.story_id,
                claim.latest_assignment_id,
                claim.stage,
                claim.claim_token,
            ),
        )
        row = await cursor.fetchone()
        return None if row is None else self._stage_from_row(row)

    async def lock_stage_claim_if_live(
        self,
        conn: psycopg.AsyncConnection,
        claim: EventProcessingStageClaim,
    ) -> bool:
        """Lock the exact live claim before persisting fenced stage output."""
        self._validate_stage(claim.stage)
        cursor = await conn.execute(
            """
            SELECT 1
            FROM story_event_processing_claims
            WHERE story_id = %s
              AND latest_assignment_id = %s
              AND stage = %s
              AND claim_token = %s
              AND lease_expires_at > now()
            FOR UPDATE
            """,
            (claim.story_id, claim.latest_assignment_id, claim.stage, claim.claim_token),
        )
        return await cursor.fetchone() is not None

    async def release_stage(
        self,
        conn: psycopg.AsyncConnection,
        claim: EventProcessingStageClaim,
    ) -> bool:
        """Release only the stage row fenced by this claim's token."""
        self._validate_stage(claim.stage)
        cursor = await conn.execute(
            """
            DELETE FROM story_event_processing_claims
            WHERE story_id = %s
              AND latest_assignment_id = %s
              AND stage = %s
              AND claim_token = %s
            """,
            (claim.story_id, claim.latest_assignment_id, claim.stage, claim.claim_token),
        )
        return cursor.rowcount == 1
