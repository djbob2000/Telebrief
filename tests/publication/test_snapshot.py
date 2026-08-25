"""Tests for Publication snapshot, candidate constraints, and idempotency (Plan 4 Task 1)."""

import datetime as dt

import psycopg
import pytest

from src.publication.repository import (
    PublicationPolicyRepository,
    PublicationRepository,
)

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


async def _seed_policies(conn: psycopg.AsyncConnection, edition_id: int) -> tuple[int, int, int]:
    policy_repo = PublicationPolicyRepository()
    elig = await policy_repo.get_or_create_eligibility_policy(
        conn, edition_id=edition_id, config_hash="elig-hash-1", prompt_version="elig-v1"
    )
    sel = await policy_repo.get_or_create_selection_policy(
        conn, edition_id=edition_id, config_hash="sel-hash-1", prompt_version="sel-v1"
    )
    wri = await policy_repo.get_or_create_writer_policy(
        conn, edition_id=edition_id, config_hash="wri-hash-1", prompt_version="wri-v1"
    )
    return (elig.id, sel.id, wri.id)


async def _seed_story_with_revision(
    conn: psycopg.AsyncConnection, edition_id: int, created_at: dt.datetime = _NOW
) -> tuple[int, int]:
    from tests.publication.conftest import seed_claim_for_story

    cur = await conn.execute(
        "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
        (edition_id, created_at),
    )
    story_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        """
        INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
        VALUES (%s, 1, 'open', 'Бердянск новость', 'hash-1', %s)
        RETURNING id
        """,
        (story_id, created_at),
    )
    rev_id = (await cur.fetchone())[0]
    await conn.execute(
        "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
    )
    await seed_claim_for_story(conn, edition_id, story_id, created_at)
    return story_id, rev_id


@pytest.mark.postgres
class TestPublicationSnapshotConstraints:
    """Tests DB constraints and idempotency for publication runs and candidates."""

    async def test_scheduled_request_key_idempotency(self, conn: psycopg.AsyncConnection, edition):
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)

        req_key = "scheduled:berdyansk:article:2026-08-22T20:00:00+03:00"

        run1 = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key=req_key,
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        assert run1.id is not None
        assert run1.status == "created"

        # Calling again with identical request_key resolves existing run without creating duplicate
        run2 = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key=req_key,
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        assert run2.id == run1.id

    async def test_candidate_duplicate_story_in_same_run_is_rejected(
        self, conn: psycopg.AsyncConnection, edition
    ):
        repo = PublicationRepository()
        policy_ids = await _seed_policies(conn, edition.id)
        run = await repo.get_or_create_run(
            conn,
            edition_id=edition.id,
            publication_type="article",
            request_key="test-key-candidates",
            snapshot_at=_NOW,
            policy_ids=policy_ids,
        )
        story_id, rev_id = await _seed_story_with_revision(conn, edition.id)

        cand1 = await repo.insert_candidate(
            conn,
            run.id,
            story_id=story_id,
            story_revision_id=rev_id,
            deterministic_rank=1,
        )
        assert cand1.id is not None

        # Inserting duplicate story for same run violates uniqueness
        with pytest.raises(psycopg.errors.UniqueViolation):
            await repo.insert_candidate(
                conn,
                run.id,
                story_id=story_id,
                story_revision_id=rev_id,
                deterministic_rank=2,
            )

    async def test_unverified_and_single_source_stories_are_eligible_candidates(
        self, conn: psycopg.AsyncConnection, edition
    ):
        from src.publication.repository import PublicationRepository

        repo = PublicationRepository()
        story_id, rev_id = await _seed_story_with_revision(conn, edition.id)

        # Neither evidence clusters nor verification assessments exist for this story
        eligible = await repo.eligible_story_revisions(
            conn,
            edition_id=edition.id,
            snapshot_at=_NOW,
        )

        assert len(eligible) == 1
        assert eligible[0]["story_id"] == story_id
        assert eligible[0]["story_revision_id"] == rev_id
