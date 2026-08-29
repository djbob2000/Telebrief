"""Story repository: explicit SQL over psycopg async connections.

Repositories never commit and never open their own connections; the caller
owns transaction boundaries. Story revisions are immutable; lifecycle changes
always append a `story_state_events` row before updating `stories`. The
composite DEFERRABLE FK stories(id, current_revision_id) ->
story_revisions(story_id, id) makes the database authoritative that the
current revision belongs to the same story. The repository accepts an
explicit revision payload only when the application service already decided a
meaningful change exists — no semantic judgment happens here.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import psycopg
from psycopg.types.json import Jsonb

from src.domain.stories import (
    NewStoryRevision,
    Story,
    StoryRevision,
    StoryStateEvent,
    StoryWithRevision,
)


class ClaimAlreadyAttachedError(RuntimeError):
    """Raised when an operation attempts to attach a claim already owned by another story."""


class StoryRepository:
    """Persistence for the story aggregate: stories, immutable revisions,
    lifecycle state events, and exclusive claim membership.

    Lifecycle transitions are deliberately loose (any state to any state) per
    YAGNI: reopening a resolved story is represented exactly like every other
    transition — an explicit `story_state_events` row — so history stays
    reconstructable and `reopened` stories remain retrieval-eligible.
    """

    async def get_claim_story_id(self, conn: psycopg.AsyncConnection, claim_id: int) -> int | None:
        """Get the story_id this claim is attached to, or None if not attached."""
        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s",
            (claim_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else int(row[0])

    async def is_claim_attached(self, conn: psycopg.AsyncConnection, claim_id: int) -> bool:
        """Return True if claim is already attached to any story."""
        return (await self.get_claim_story_id(conn, claim_id)) is not None

    async def create_story_with_revision(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        claim_id: int,
        revision: NewStoryRevision,
    ) -> StoryWithRevision:
        """Create a story shell, its first revision (revision_no = 1), flip it
        to `active` with the pointer set, and attach the founding claim — all
        inside the caller's transaction."""
        story_id = await self._insert_story_shell(conn, edition_id=edition_id)
        story_revision = await self._insert_revision(conn, story_id=story_id, revision=revision)
        await conn.execute(
            "UPDATE stories SET current_revision_id=%s, lifecycle_state='active' WHERE id=%s",
            (story_revision.id, story_id),
        )
        attached = await self.attach_claim(
            conn, story_id=story_id, claim_id=claim_id, attached_at=revision.created_at
        )
        if not attached:
            raise ClaimAlreadyAttachedError(
                f"cannot create story: claim {claim_id} is already attached to another story"
            )
        return StoryWithRevision(story_id=story_id, revision=story_revision)

    async def attach_claim(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        claim_id: int,
        attached_at: dt.datetime,
    ) -> bool:
        """Attach one claim to one story, idempotently.

        UNIQUE(claim_id) enforces spec §19 exclusivity (a claim belongs to at
        most ONE story forever); ON CONFLICT DO NOTHING makes replayed
        identical attachments no-ops and never lets a later story steal an
        already-attached claim. Returns True if attached or already owned by
        story_id; False if attached to another story or insert failed.
        """
        cursor = await conn.execute(
            """
            INSERT INTO story_claims (story_id, claim_id, edition_id, attached_at)
            SELECT s.id, %s, s.edition_id, %s
            FROM stories s
            WHERE s.id = %s
            ON CONFLICT DO NOTHING
            RETURNING story_id
            """,
            (claim_id, attached_at, story_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return True
        # Check if already attached to this same story (idempotent replay) vs another story (conflict)
        owner_cur = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s",
            (claim_id,),
        )
        owner_row = await owner_cur.fetchone()
        return owner_row is not None and owner_row[0] == story_id

    async def list_attached_claim_ids(
        self, conn: psycopg.AsyncConnection, story_id: int
    ) -> list[int]:
        """List all claim ids attached to this story, ordered by id."""
        cursor = await conn.execute(
            "SELECT claim_id FROM story_claims WHERE story_id = %s ORDER BY claim_id ASC",
            (story_id,),
        )
        return [row[0] for row in await cursor.fetchall()]

    async def create_revision_if_semantic_change(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        semantic_changed: bool,
        revision: NewStoryRevision | None,
    ) -> StoryRevision | None:
        """Append the next revision only when the service says semantics
        changed AND supplies the explicit payload; otherwise this is a no-op
        returning None. The new revision becomes the current one."""
        if not semantic_changed or revision is None:
            return None
        story_revision = await self._insert_revision(conn, story_id=story_id, revision=revision)
        await conn.execute(
            "UPDATE stories SET current_revision_id=%s WHERE id=%s",
            (story_revision.id, story_id),
        )
        return story_revision

    async def set_state(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        state: str,
        reason: str | None = None,
        evidence: dict | None = None,
    ) -> StoryStateEvent:
        """Record an explicit state event, then move `lifecycle_state`.

        The event row is written FIRST so the transition is always auditable
        inside the caller's transaction; observed_at/created_at default to
        now() in the database.
        """
        cursor = await conn.execute(
            """
            INSERT INTO story_state_events (story_id, type, reason, evidence)
            VALUES (%s, %s, %s, %s)
            RETURNING id, story_id, type, observed_at, reason, evidence, created_at
            """,
            (story_id, state, reason, Jsonb(evidence or {})),
        )
        event = StoryStateEvent.from_row(await cursor.fetchone())
        await conn.execute(
            "UPDATE stories SET lifecycle_state=%s WHERE id=%s",
            (state, story_id),
        )
        return event

    async def get(self, conn: psycopg.AsyncConnection, story_id: int) -> Story | None:
        cursor = await conn.execute(
            """
            SELECT id, edition_id, current_revision_id, lifecycle_state, created_at, knowledge_source
            FROM stories WHERE id = %s
            """,
            (story_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else Story.from_row(row)

    async def current_revision_id(self, conn: psycopg.AsyncConnection, story_id: int) -> int | None:
        """The story's current revision pointer, or None when unknown/unset."""
        cursor = await conn.execute(
            "SELECT current_revision_id FROM stories WHERE id = %s",
            (story_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else row[0]

    async def get_revision(
        self, conn: psycopg.AsyncConnection, revision_id: int
    ) -> StoryRevision | None:
        cursor = await conn.execute(
            """
            SELECT id, story_id, revision_no, title, summary, current_state,
                semantic_text, content_hash, reason, created_at, event_payload
            FROM story_revisions WHERE id = %s
            """,
            (revision_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else StoryRevision.from_row(row)

    async def get_revisions(
        self, conn: psycopg.AsyncConnection, revision_ids: Sequence[int]
    ) -> list[StoryRevision]:
        """Batch-load immutable revisions by id (deduplicated, ordered by id)."""
        unique_ids = sorted(set(revision_ids))
        if not unique_ids:
            return []
        cursor = await conn.execute(
            """
            SELECT id, story_id, revision_no, title, summary, current_state,
                semantic_text, content_hash, reason, created_at, event_payload
            FROM story_revisions WHERE id = ANY(%s)
            ORDER BY id
            """,
            (unique_ids,),
        )
        return [StoryRevision.from_row(row) async for row in cursor]

    async def _insert_story_shell(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        knowledge_source: str = "legacy_claims",
    ) -> int:
        """Insert a candidate story with a NULL pointer; the composite FK is
        satisfied because MATCH SIMPLE ignores NULL current_revision_id."""
        cursor = await conn.execute(
            "INSERT INTO stories (edition_id, knowledge_source) VALUES (%s, %s) RETURNING id",
            (edition_id, knowledge_source),
        )
        row = await cursor.fetchone()
        if row is None:  # pragma: no cover - RETURNING always yields one row
            raise RuntimeError("INSERT INTO stories RETURNING produced no row")
        return int(row[0])

    async def create_story_shell(
        self,
        conn: psycopg.AsyncConnection,
        *,
        edition_id: int,
        knowledge_source: str = "event_first",
    ) -> int:
        """Public helper to insert an unrevised Story shell."""
        return await self._insert_story_shell(
            conn, edition_id=edition_id, knowledge_source=knowledge_source
        )

    async def _insert_revision(
        self,
        conn: psycopg.AsyncConnection,
        *,
        story_id: int,
        revision: NewStoryRevision,
    ) -> StoryRevision:
        """Append one immutable revision with revision_no = MAX+1 computed
        inside the caller's transaction; uq_story_revisions_story_no is the
        concurrency backstop."""
        cursor = await conn.execute(
            """
            INSERT INTO story_revisions (
                story_id, revision_no, title, summary, current_state,
                semantic_text, content_hash, reason, event_payload, created_at
            )
            SELECT %s, COALESCE(MAX(revision_no), 0) + 1, %s, %s, %s, %s, %s, %s, %s, %s
            FROM story_revisions
            WHERE story_id = %s
            RETURNING id, story_id, revision_no, title, summary, current_state,
                semantic_text, content_hash, reason, created_at, event_payload
            """,
            (
                story_id,
                revision.title,
                revision.summary,
                revision.current_state,
                revision.semantic_text,
                revision.content_hash,
                revision.reason,
                Jsonb(revision.event_payload or {}),
                revision.created_at,
                story_id,
            ),
        )
        return StoryRevision.from_row(await cursor.fetchone())
