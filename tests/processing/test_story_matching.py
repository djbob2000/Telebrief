"""Story aggregate persistence tests (Plan 3 Task 6).

Covers spec §18 shapes: immutable story revisions with the composite
DEFERRABLE FK proving the current revision belongs to the same story,
meaningful-change-only revisions, explicit lifecycle state events, and the
exclusive/idempotent claim membership (`story_claims`, spec §19).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from types import SimpleNamespace

import psycopg
import pytest

from src.domain.claims import NewClaim
from src.domain.stories import (
    NewStoryRevision,
    Story,
    StoryRelation,
    StoryStateEvent,
    StoryWithRevision,
)
from src.repositories.claims import (
    ClaimExtractionPolicyRepository,
    ClaimExtractionRunRepository,
    ClaimRepository,
)
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    RelevancePolicyVersionRepository,
)
from src.repositories.stories import StoryRepository

STORY_REPO = StoryRepository()

_RELEVANCE_POLICY_REPO = RelevancePolicyVersionRepository()
_DECISION_REPO = EditionRelevanceDecisionRepository()
_EXTRACTION_POLICY_REPO = ClaimExtractionPolicyRepository()
_RUN_REPO = ClaimExtractionRunRepository()
_CLAIM_REPO = ClaimRepository()

_T0 = dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.timezone.utc)
_T1 = dt.datetime(2026, 8, 22, 12, 30, tzinfo=dt.timezone.utc)

_COUNTER = {"n": 0}


def _next_n() -> int:
    _COUNTER["n"] += 1
    return _COUNTER["n"]


async def _make_claim(conn: psycopg.AsyncConnection, edition_id: int, source_item_revision_id: int):
    """Spec §15 chain: relevance decision -> succeeded run -> one claim."""
    n = _next_n()
    relevance_policy = await _RELEVANCE_POLICY_REPO.insert(
        conn,
        edition_id=edition_id,
        version=n,
        config_hash=f"relevance-cfg-{n}",
        prompt_version="relevance-prompt-1",
    )
    decision = await _DECISION_REPO.insert_root(
        conn,
        source_item_revision_id=source_item_revision_id,
        edition_id=edition_id,
        relevance_policy_id=relevance_policy.id,
        status="relevant",
        confidence=None,
        reason="test setup",
    )
    extraction_policy = await _EXTRACTION_POLICY_REPO.insert(
        conn,
        edition_id=edition_id,
        version=n,
        config_hash=f"extraction-cfg-{n}",
        prompt_version="extraction-prompt-1",
    )
    run, _created = await _RUN_REPO.get_or_create_run(
        conn,
        source_item_revision_id=source_item_revision_id,
        edition_id=edition_id,
        extraction_policy_id=extraction_policy.id,
        relevance_decision_id=decision.id,
    )
    assert await _RUN_REPO.mark_succeeded(conn, run.id, completed_at=_T0)
    assertion = f"Утверждение номер {n}: вода пришла на улицу Приморскую."
    claims = await _CLAIM_REPO.insert_claims(
        conn,
        run=run,
        claims=[
            NewClaim(
                assertion_text=assertion,
                normalized_assertion=assertion.lower(),
            )
        ],
    )
    return claims[0]


def _new_revision(semantic_text: str, *, state: str = "developing") -> NewStoryRevision:
    return NewStoryRevision(
        current_state=state,
        semantic_text=semantic_text,
        content_hash=hashlib.sha256(semantic_text.encode("utf-8")).hexdigest(),
        created_at=_T1,
    )


async def _create_story_with_claim(
    conn: psycopg.AsyncConnection, edition_id: int, source_item_revision_id: int
) -> SimpleNamespace:
    n = _next_n()
    claim = await _make_claim(conn, edition_id, source_item_revision_id)
    result = await STORY_REPO.create_story_with_revision(
        conn,
        edition_id=edition_id,
        claim_id=claim.id,
        revision=_new_revision(f"Разлив топлива у причала, сообщение {n}."),
    )
    return SimpleNamespace(story_id=result.story_id, revision=result.revision, claim=claim)


class TestStoryRepositoryCreation:
    async def test_story_repository_create_returns_active_story_with_first_revision_and_claim(
        self, conn, edition, revision_factory
    ):
        source_revision = await revision_factory()
        claim = await _make_claim(conn, edition.id, source_revision.id)

        result = await STORY_REPO.create_story_with_revision(
            conn,
            edition_id=edition.id,
            claim_id=claim.id,
            revision=_new_revision(
                "На АЗС возле почты сливают топливо в бочки.", state="developing"
            ),
        )

        assert isinstance(result, StoryWithRevision)
        assert result.story_id > 0
        assert result.revision.story_id == result.story_id
        assert result.revision.revision_no == 1
        assert result.revision.semantic_text == "На АЗС возле почты сливают топливо в бочки."
        assert result.revision.current_state == "developing"

        story = await STORY_REPO.get(conn, result.story_id)
        assert story is not None
        assert story.edition_id == edition.id
        assert story.lifecycle_state == "active"
        assert story.current_revision_id == result.revision.id

        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s", (claim.id,)
        )
        membership = await cursor.fetchone()
        assert membership is not None
        assert membership[0] == result.story_id

    async def test_story_repository_get_and_current_revision_helpers(
        self, conn, edition, revision_factory
    ):
        assert await STORY_REPO.get(conn, 10**9) is None
        assert await STORY_REPO.current_revision_id(conn, 10**9) is None

        source_revision = await revision_factory()
        created = await _create_story_with_claim(conn, edition.id, source_revision.id)

        story = await STORY_REPO.get(conn, created.story_id)
        assert isinstance(story, Story)
        assert story.id == created.story_id
        assert story.edition_id == edition.id
        assert isinstance(story.created_at, dt.datetime)
        assert await STORY_REPO.current_revision_id(conn, created.story_id) == created.revision.id


class TestStoryRepositoryCurrentRevisionInvariant:
    async def test_story_repository_cross_story_current_revision_rejected_immediately(
        self, conn, edition, revision_factory
    ):
        first_source = await revision_factory()
        second_source = await revision_factory()
        first = await _create_story_with_claim(conn, edition.id, first_source.id)
        second = await _create_story_with_claim(conn, edition.id, second_source.id)

        # Composite FK stories(id, current_revision_id) ->
        # story_revisions(story_id, id) is INITIALLY IMMEDIATE: pointing one
        # story at another story's revision fails at statement time.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(
                "UPDATE stories SET current_revision_id = %s WHERE id = %s",
                (first.revision.id, second.story_id),
            )

    async def test_story_repository_deferred_constraint_satisfied_within_transaction(
        self, conn, edition, revision_factory
    ):
        source_revision = await revision_factory()

        # The shell -> first-revision -> pointer ordering must satisfy the
        # composite FK even when checks are deferred to commit time.
        async with conn.transaction():
            await conn.execute("SET CONSTRAINTS ALL DEFERRED")
            created = await _create_story_with_claim(conn, edition.id, source_revision.id)

        story = await STORY_REPO.get(conn, created.story_id)
        assert story is not None
        assert story.lifecycle_state == "active"
        assert story.current_revision_id == created.revision.id

    async def test_story_repository_deferred_violation_surfaces_at_commit(
        self, conn, edition, revision_factory
    ):
        first_source = await revision_factory()
        second_source = await revision_factory()
        first = await _create_story_with_claim(conn, edition.id, first_source.id)
        second = await _create_story_with_claim(conn, edition.id, second_source.id)

        # Deferral moves the check from statement time to commit time; the
        # cross-story pointer is still rejected by the database.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            async with conn.transaction():
                await conn.execute("SET CONSTRAINTS ALL DEFERRED")
                await conn.execute(
                    "UPDATE stories SET current_revision_id = %s WHERE id = %s",
                    (first.revision.id, second.story_id),
                )


class TestStoryRepositoryRevisionsAndLifecycle:
    async def test_story_repository_unchanged_semantics_creates_no_revision(
        self, conn, edition, revision_factory
    ):
        source_revision = await revision_factory()
        created = await _create_story_with_claim(conn, edition.id, source_revision.id)

        unchanged = await STORY_REPO.create_revision_if_semantic_change(
            conn, story_id=created.story_id, semantic_changed=False, revision=None
        )
        assert unchanged is None
        # A change flag without an explicit payload is a service-contract bug;
        # the repository refuses to invent a revision.
        missing_payload = await STORY_REPO.create_revision_if_semantic_change(
            conn, story_id=created.story_id, semantic_changed=True, revision=None
        )
        assert missing_payload is None

        cursor = await conn.execute(
            "SELECT count(*) FROM story_revisions WHERE story_id = %s",
            (created.story_id,),
        )
        assert (await cursor.fetchone())[0] == 1
        assert await STORY_REPO.current_revision_id(conn, created.story_id) == created.revision.id

    async def test_story_repository_semantic_change_creates_new_revision_and_state_event(
        self, conn, edition, revision_factory
    ):
        source_revision = await revision_factory()
        created = await _create_story_with_claim(conn, edition.id, source_revision.id)

        resolved_event = await STORY_REPO.set_state(
            conn,
            story_id=created.story_id,
            state="resolved",
            reason="Работы завершены",
        )
        reopened_event = await STORY_REPO.set_state(
            conn,
            story_id=created.story_id,
            state="reopened",
            evidence={"claim_ids": [created.claim.id]},
        )
        assert isinstance(resolved_event, StoryStateEvent)
        assert resolved_event.type == "resolved"
        assert resolved_event.reason == "Работы завершены"
        assert isinstance(reopened_event, StoryStateEvent)

        changed_revision = await STORY_REPO.create_revision_if_semantic_change(
            conn,
            story_id=created.story_id,
            semantic_changed=True,
            revision=_new_revision(
                "Разлив расширяется: топливо добралось до пляжа.",
                state="developing",
            ),
        )

        assert changed_revision is not None
        assert changed_revision.revision_no == 2
        assert changed_revision.reason is None
        assert await STORY_REPO.current_revision_id(conn, created.story_id) == changed_revision.id

        story = await STORY_REPO.get(conn, created.story_id)
        assert story is not None
        assert story.lifecycle_state == "reopened"  # reopened stays eligible

        cursor = await conn.execute(
            "SELECT type FROM story_state_events WHERE story_id = %s ORDER BY id",
            (created.story_id,),
        )
        event_types = [row[0] for row in await cursor.fetchall()]
        assert event_types == ["resolved", "reopened"]

    async def test_story_repository_attach_claim_is_idempotent(
        self, conn, edition, revision_factory
    ):
        first_source = await revision_factory()
        second_source = await revision_factory()
        created = await _create_story_with_claim(conn, edition.id, first_source.id)

        # Replay of the identical attachment changes nothing.
        await STORY_REPO.attach_claim(
            conn,
            story_id=created.story_id,
            claim_id=created.claim.id,
            attached_at=_T0,
        )
        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s",
            (created.claim.id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == created.story_id

        # A different claim joins the same story.
        other_claim = await _make_claim(conn, edition.id, second_source.id)
        await STORY_REPO.attach_claim(
            conn, story_id=created.story_id, claim_id=other_claim.id, attached_at=_T1
        )
        cursor = await conn.execute(
            "SELECT count(*) FROM story_claims WHERE story_id = %s",
            (created.story_id,),
        )
        assert (await cursor.fetchone())[0] == 2

        # Spec §19 exclusivity: a claim already owned by one story is never
        # stolen by another; ON CONFLICT DO NOTHING keeps the original row.
        third_source = await revision_factory()
        other_story = await _create_story_with_claim(conn, edition.id, third_source.id)
        await STORY_REPO.attach_claim(
            conn,
            story_id=other_story.story_id,
            claim_id=created.claim.id,
            attached_at=_T1,
        )
        cursor = await conn.execute(
            "SELECT story_id FROM story_claims WHERE claim_id = %s",
            (created.claim.id,),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == created.story_id


class TestStoryDomainModels:
    async def test_story_relation_from_row_maps_columns(self):
        relation = StoryRelation.from_row((7, 3, 4, "RELATED_TO", _T0))
        assert relation.id == 7
        assert relation.from_story_id == 3
        assert relation.to_story_id == 4
        assert relation.relation_type == "RELATED_TO"
        assert relation.created_at == _T0
