"""Place evidence repository: explicit SQL over psycopg async connections.

Repositories never commit and never open their own connections; the caller
owns transaction boundaries. Places, aliases, mentions, entities, and
results are append-only; the only UPDATE paths are run status transitions
and the edition pointer maintained by the policy service (both documented
exceptions mirroring Plan 3 Task 1/5).

Canonical results: ``insert_resolution_result`` writes with ON CONFLICT DO
NOTHING against uq_place_resolution_canonical ON (mention_id, policy_id)
WHERE status IN ('resolved','unresolved') and re-reads the winner, so
at-least-once executions converge on one row per (mention, policy) — a NULL
``place_id`` with status 'unresolved' occupies the slot exactly like a
resolved hit.
"""

from __future__ import annotations

import datetime as dt

import psycopg
from psycopg.types.json import Jsonb

from src.domain.places import (
    ClaimEntity,
    ClaimPlaceMention,
    Place,
    PlaceAlias,
    PlaceResolutionPolicyVersion,
    PlaceResolutionResult,
    PlaceResolutionRun,
    normalize_place_text,
)

_PLACE_COLUMNS = """
    id, canonical_name, kind, parent_place_id, latitude, longitude,
    metadata, created_at
"""
_MENTION_COLUMNS = "id, claim_id, role, original_text, created_at"
_ENTITY_COLUMNS = "id, claim_id, normalized_text, entity_kind, metadata, created_at"
_POLICY_COLUMNS = "id, edition_id, version, config_hash, prompt_version, created_at"
_RUN_COLUMNS = """
    id, mention_id, edition_id, policy_id, started_at, completed_at,
    status, error_kind, metadata
"""
_RESULT_COLUMNS = """
    id, run_id, mention_id, policy_id, place_id, status, confidence,
    reason, created_at
"""


class PlaceRepository:
    """Persistence for places, aliases, claim evidence rows, and results."""

    # -- places -------------------------------------------------------------

    async def insert_place(
        self,
        conn: psycopg.AsyncConnection,
        *,
        canonical_name: str,
        kind: str | None = None,
        parent_place_id: int | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        metadata: dict | None = None,
    ) -> Place:
        cursor = await conn.execute(
            f"""
            INSERT INTO places (
                canonical_name, kind, parent_place_id, latitude, longitude, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING {_PLACE_COLUMNS}
            """,  # noqa: S608 — column list is a module constant; values are bound params
            (canonical_name, kind, parent_place_id, latitude, longitude, Jsonb(metadata or {})),
        )
        return Place.from_row(await cursor.fetchone())

    async def get_place(self, conn: psycopg.AsyncConnection, place_id: int) -> Place | None:
        cursor = await conn.execute(
            f"SELECT {_PLACE_COLUMNS} FROM places WHERE id = %s",  # noqa: S608
            (place_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else Place.from_row(row)

    async def find_by_external_key(
        self,
        conn: psycopg.AsyncConnection,
        *,
        profile_id: str,
        external_id: str,
    ) -> Place | None:
        """Idempotent-import lookup by (profile_id, external_id) metadata key."""
        cursor = await conn.execute(
            f"""
            SELECT {_PLACE_COLUMNS} FROM places
            WHERE metadata->>'profile_id' = %s AND metadata->>'external_id' = %s
            """,  # noqa: S608
            (profile_id, external_id),
        )
        row = await cursor.fetchone()
        return None if row is None else Place.from_row(row)

    async def count_places(self, conn: psycopg.AsyncConnection) -> int:
        cursor = await conn.execute("SELECT count(*) FROM places")
        return int((await cursor.fetchone())[0])

    # -- aliases ------------------------------------------------------------

    async def insert_alias(
        self,
        conn: psycopg.AsyncConnection,
        *,
        place_id: int,
        alias: str,
        normalized_alias: str | None = None,
    ) -> tuple[PlaceAlias, bool]:
        """Insert an alias unless THIS place already owns its normalized form.

        Aliases are deliberately NOT unique across places; idempotency is
        per-place so repeated imports never duplicate rows. The normalized
        key always goes through the domain contract when not supplied.
        """
        key = normalized_alias if normalized_alias is not None else normalize_place_text(alias)
        existing = await conn.execute(
            """
            SELECT id, place_id, alias, normalized_alias
            FROM place_aliases
            WHERE place_id = %s AND normalized_alias = %s
            """,
            (place_id, key),
        )
        row = await existing.fetchone()
        if row is not None:
            return PlaceAlias.from_row(row), False
        cursor = await conn.execute(
            """
            INSERT INTO place_aliases (place_id, alias, normalized_alias)
            VALUES (%s, %s, %s)
            RETURNING id, place_id, alias, normalized_alias
            """,
            (place_id, alias, key),
        )
        return PlaceAlias.from_row(await cursor.fetchone()), True

    async def alias_candidates(
        self, conn: psycopg.AsyncConnection, *, normalized_alias: str
    ) -> list[Place]:
        """All places owning this exact normalized alias (deterministic order)."""
        cursor = await conn.execute(
            """
            SELECT p.id, p.canonical_name, p.kind, p.parent_place_id,
                   p.latitude, p.longitude, p.metadata, p.created_at
            FROM places p
            JOIN place_aliases a ON a.place_id = p.id
            WHERE a.normalized_alias = %s
            ORDER BY p.id
            """,
            (normalized_alias,),
        )
        return [Place.from_row(row) for row in await cursor.fetchall()]

    async def count_aliases(self, conn: psycopg.AsyncConnection) -> int:
        cursor = await conn.execute("SELECT count(*) FROM place_aliases")
        return int((await cursor.fetchone())[0])

    # -- claim evidence -----------------------------------------------------

    async def create_mention(
        self,
        conn: psycopg.AsyncConnection,
        *,
        claim_id: int,
        original_text: str,
        role: str | None = None,
    ) -> tuple[ClaimPlaceMention, bool]:
        """Idempotent per (claim_id, original_text); original text preserved verbatim."""
        existing = await conn.execute(
            f"SELECT {_MENTION_COLUMNS} FROM claim_place_mentions "  # noqa: S608 — static template; values are bound params
            "WHERE claim_id = %s AND original_text = %s",  # noqa: S608
            (claim_id, original_text),
        )
        row = await existing.fetchone()
        if row is not None:
            return ClaimPlaceMention.from_row(row), False
        cursor = await conn.execute(
            f"""
            INSERT INTO claim_place_mentions (claim_id, role, original_text)
            VALUES (%s, %s, %s)
            RETURNING {_MENTION_COLUMNS}
            """,  # noqa: S608
            (claim_id, role, original_text),
        )
        return ClaimPlaceMention.from_row(await cursor.fetchone()), True

    async def get_mention(
        self, conn: psycopg.AsyncConnection, mention_id: int
    ) -> ClaimPlaceMention | None:
        cursor = await conn.execute(
            f"SELECT {_MENTION_COLUMNS} FROM claim_place_mentions WHERE id = %s",  # noqa: S608
            (mention_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else ClaimPlaceMention.from_row(row)

    async def list_mentions_for_claim(
        self, conn: psycopg.AsyncConnection, claim_id: int
    ) -> list[ClaimPlaceMention]:
        cursor = await conn.execute(
            f"SELECT {_MENTION_COLUMNS} FROM claim_place_mentions "  # noqa: S608 — static template; values are bound params
            "WHERE claim_id = %s ORDER BY id",  # noqa: S608
            (claim_id,),
        )
        return [ClaimPlaceMention.from_row(row) for row in await cursor.fetchall()]

    async def create_entity(
        self,
        conn: psycopg.AsyncConnection,
        *,
        claim_id: int,
        normalized_text: str,
        entity_kind: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[ClaimEntity, bool]:
        existing = await conn.execute(
            f"SELECT {_ENTITY_COLUMNS} FROM claim_entities "  # noqa: S608 — static template; values are bound params
            "WHERE claim_id = %s AND normalized_text = %s",  # noqa: S608
            (claim_id, normalized_text),
        )
        row = await existing.fetchone()
        if row is not None:
            return ClaimEntity.from_row(row), False
        cursor = await conn.execute(
            f"""
            INSERT INTO claim_entities (claim_id, normalized_text, entity_kind, metadata)
            VALUES (%s, %s, %s, %s)
            RETURNING {_ENTITY_COLUMNS}
            """,  # noqa: S608
            (claim_id, normalized_text, entity_kind, Jsonb(metadata or {})),
        )
        return ClaimEntity.from_row(await cursor.fetchone()), True

    async def list_entities_for_claim(
        self, conn: psycopg.AsyncConnection, claim_id: int
    ) -> list[ClaimEntity]:
        cursor = await conn.execute(
            f"SELECT {_ENTITY_COLUMNS} FROM claim_entities "  # noqa: S608 — static template; values are bound params
            "WHERE claim_id = %s ORDER BY id",  # noqa: S608
            (claim_id,),
        )
        return [ClaimEntity.from_row(row) for row in await cursor.fetchall()]

    # -- versioned results --------------------------------------------------

    async def insert_resolution_result(
        self,
        conn: psycopg.AsyncConnection,
        *,
        run_id: int,
        mention_id: int,
        policy_id: int,
        place_id: int | None,
        status: str,
        confidence: float | None = None,
        reason: str | None = None,
    ) -> tuple[PlaceResolutionResult, bool]:
        """Write one result under the canonical partial unique index.

        Returns ``(winner, inserted)``; a lost race re-reads the concurrent
        winner so at-least-once execution never duplicates outcomes.
        """
        cursor = await conn.execute(
            f"""
            INSERT INTO place_resolution_results (
                run_id, mention_id, policy_id, place_id, status, confidence, reason
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (mention_id, policy_id)
                WHERE status IN ('resolved', 'unresolved')
            DO NOTHING
            RETURNING {_RESULT_COLUMNS}
            """,  # noqa: S608
            (run_id, mention_id, policy_id, place_id, status, confidence, reason),
        )
        row = await cursor.fetchone()
        if row is not None:
            return PlaceResolutionResult.from_row(row), True
        winner = await self.find_canonical_result(conn, mention_id=mention_id, policy_id=policy_id)
        if winner is None:
            raise RuntimeError(
                f"canonical place resolution vanished for mention={mention_id} policy={policy_id}"
            )
        return winner, False

    async def find_canonical_result(
        self, conn: psycopg.AsyncConnection, *, mention_id: int, policy_id: int
    ) -> PlaceResolutionResult | None:
        cursor = await conn.execute(
            f"""
            SELECT {_RESULT_COLUMNS} FROM place_resolution_results
            WHERE mention_id = %s AND policy_id = %s
              AND status IN ('resolved', 'unresolved')
            ORDER BY id DESC LIMIT 1
            """,  # noqa: S608
            (mention_id, policy_id),
        )
        row = await cursor.fetchone()
        return None if row is None else PlaceResolutionResult.from_row(row)

    async def barrier_satisfied(
        self, conn: psycopg.AsyncConnection, *, claim_id: int, policy_id: int
    ) -> bool:
        """True when EVERY mention of the claim holds a completed current-policy
        result (resolved OR explicit unresolved); vacuously true with none."""
        cursor = await conn.execute(
            """
            SELECT NOT EXISTS (
                SELECT 1 FROM claim_place_mentions m
                WHERE m.claim_id = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM place_resolution_results r
                      WHERE r.mention_id = m.id AND r.policy_id = %s
                        AND r.status IN ('resolved', 'unresolved')
                  )
            )
            """,
            (claim_id, policy_id),
        )
        return bool((await cursor.fetchone())[0])

    async def list_mentions_missing_result(
        self,
        conn: psycopg.AsyncConnection,
        *,
        policy_id: int,
        limit: int = 500,
    ) -> list[ClaimPlaceMention]:
        """Bounded backfill slice: mentions still owing this exact policy a
        completed result (failed runs never cover)."""
        cursor = await conn.execute(
            f"""
            SELECT {_MENTION_COLUMNS} FROM claim_place_mentions m
            WHERE NOT EXISTS (
                SELECT 1 FROM place_resolution_results r
                WHERE r.mention_id = m.id AND r.policy_id = %s
                  AND r.status IN ('resolved', 'unresolved')
            )
            ORDER BY m.id LIMIT %s
            """,  # noqa: S608
            (policy_id, limit),
        )
        return [ClaimPlaceMention.from_row(row) for row in await cursor.fetchall()]

    # -- legacy metadata migration ------------------------------------------

    async def list_claims_with_metadata_evidence(
        self,
        conn: psycopg.AsyncConnection,
        *,
        after_claim_id: int | None = None,
        limit: int = 500,
    ) -> list[tuple[int, dict]]:
        """Claims whose metadata still carries T4-staging mention/entity lists."""
        cursor = await conn.execute(
            """
            SELECT id, metadata FROM claims
            WHERE (metadata ? 'place_mentions' OR metadata ? 'entities')
              AND id > COALESCE(%s, 0)
            ORDER BY id LIMIT %s
            """,
            (after_claim_id, limit),
        )
        return [(int(row[0]), dict(row[1])) for row in await cursor.fetchall()]


class PlaceResolutionPolicyRepository:
    """Persistence for immutable `place_resolution_policy_versions` rows."""

    async def insert(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        version: int,
        config_hash: str,
        prompt_version: str,
    ) -> PlaceResolutionPolicyVersion:
        cursor = await conn.execute(
            f"""
            INSERT INTO place_resolution_policy_versions (
                edition_id, version, config_hash, prompt_version
            )
            VALUES (%s, %s, %s, %s)
            RETURNING {_POLICY_COLUMNS}
            """,  # noqa: S608
            (edition_id, version, config_hash, prompt_version),
        )
        return PlaceResolutionPolicyVersion.from_row(await cursor.fetchone())

    async def get(
        self, conn: psycopg.AsyncConnection, policy_id: int
    ) -> PlaceResolutionPolicyVersion | None:
        cursor = await conn.execute(
            f"SELECT {_POLICY_COLUMNS} FROM place_resolution_policy_versions "  # noqa: S608 — static template; values are bound params
            "WHERE id = %s",  # noqa: S608
            (policy_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else PlaceResolutionPolicyVersion.from_row(row)

    async def list_for_edition(
        self, conn: psycopg.AsyncConnection, edition_id: int
    ) -> list[PlaceResolutionPolicyVersion]:
        cursor = await conn.execute(
            f"SELECT {_POLICY_COLUMNS} FROM place_resolution_policy_versions "  # noqa: S608 — static template; values are bound params
            "WHERE edition_id = %s ORDER BY version",  # noqa: S608
            (edition_id,),
        )
        rows = await cursor.fetchall()
        return [PlaceResolutionPolicyVersion.from_row(row) for row in rows]

    async def set_edition_pointer(
        self, conn: psycopg.AsyncConnection, *, edition_id: int, policy_id: int
    ) -> None:
        """Documented mutable exception: the edition's active place-policy
        pointer. The composite FK keeps it edition-consistent."""
        await conn.execute(
            """
            UPDATE editions SET current_place_policy_id = %s WHERE id = %s
            """,
            (policy_id, edition_id),
        )

    async def get_edition_pointer(
        self, conn: psycopg.AsyncConnection, *, edition_id: int
    ) -> PlaceResolutionPolicyVersion | None:
        cursor = await conn.execute(
            """
            SELECT p.id, p.edition_id, p.version, p.config_hash,
                   p.prompt_version, p.created_at
            FROM place_resolution_policy_versions p
            JOIN editions e ON e.current_place_policy_id = p.id
            WHERE e.id = %s
            """,
            (edition_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else PlaceResolutionPolicyVersion.from_row(row)


class PlaceResolutionRunRepository:
    """Persistence for `place_resolution_runs`; guarded transitions only."""

    async def insert_running(
        self,
        conn: psycopg.AsyncConnection,
        *,
        mention_id: int,
        edition_id: int,
        policy_id: int,
    ) -> PlaceResolutionRun:
        cursor = await conn.execute(
            f"""
            INSERT INTO place_resolution_runs (
                mention_id, edition_id, policy_id, status
            )
            VALUES (%s, %s, %s, 'running')
            RETURNING {_RUN_COLUMNS}
            """,  # noqa: S608
            (mention_id, edition_id, policy_id),
        )
        return PlaceResolutionRun.from_row(await cursor.fetchone())

    async def latest_running(
        self, conn: psycopg.AsyncConnection, *, mention_id: int, policy_id: int
    ) -> PlaceResolutionRun | None:
        cursor = await conn.execute(
            f"""
            SELECT {_RUN_COLUMNS} FROM place_resolution_runs
            WHERE mention_id = %s AND policy_id = %s AND status = 'running'
            ORDER BY id DESC LIMIT 1
            """,  # noqa: S608
            (mention_id, policy_id),
        )
        row = await cursor.fetchone()
        return None if row is None else PlaceResolutionRun.from_row(row)

    async def mark_succeeded(
        self, conn: psycopg.AsyncConnection, run_id: int, *, completed_at: dt.datetime
    ) -> bool:
        """Guarded running -> succeeded."""
        cursor = await conn.execute(
            """
            UPDATE place_resolution_runs SET status = 'succeeded', completed_at = %s
            WHERE id = %s AND status = 'running'
            RETURNING id
            """,
            (completed_at, run_id),
        )
        return await cursor.fetchone() is not None

    async def mark_failed(
        self,
        conn: psycopg.AsyncConnection,
        run_id: int,
        *,
        error_kind: str,
        completed_at: dt.datetime,
    ) -> bool:
        """Guarded running -> failed; NEVER demotes a succeeded winner."""
        cursor = await conn.execute(
            """
            UPDATE place_resolution_runs SET status = 'failed', error_kind = %s,
                   completed_at = %s
            WHERE id = %s AND status = 'running'
            RETURNING id
            """,
            (error_kind, completed_at, run_id),
        )
        return await cursor.fetchone() is not None


__all__ = [
    "PlaceRepository",
    "PlaceResolutionPolicyRepository",
    "PlaceResolutionRunRepository",
]
