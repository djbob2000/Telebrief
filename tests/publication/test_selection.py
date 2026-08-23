"""Tests for AI editorial selection over frozen candidates (Plan 4 Task 3)."""

import datetime as dt

import psycopg
import pytest

from src.db.uow import DatabaseUnitOfWork
from src.publication.models import PublicationCandidate, PublicationRun
from src.publication.selection import (
    EditorialSelectionService,
    SelectionProposal,
)
from src.publication.snapshot import PublicationSnapshotService

_NOW = dt.datetime(2026, 8, 22, 20, 0, tzinfo=dt.timezone.utc)


class MockSelectionModel:
    def __init__(self, proposals: list[SelectionProposal]) -> None:
        self.proposals = proposals
        self.calls: list[list[PublicationCandidate]] = []

    async def select_stories(
        self,
        *,
        run: PublicationRun,
        candidates: list[PublicationCandidate],
    ) -> list[SelectionProposal]:
        self.calls.append(candidates)
        return self.proposals


@pytest.mark.postgres
class TestEditorialSelection:
    """Tests editorial selection behavior and invariant enforcement."""

    async def test_single_source_story_can_be_included(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        # Seed story
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Оперативное сообщение о перекрытии улицы', 'h-1', %s) RETURNING id
            """,
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-sel-single-source",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 1

        # Model proposes INCLUDE with unverified_operational intent
        model = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=story_id,
                    story_revision_id=rev_id,
                    decision="INCLUDE",
                    presentation_intent="unverified_operational",
                    confidence=0.85,
                    reason="Полезная оперативная информация для жителей",
                    rank=1,
                )
            ]
        )
        sel_service = EditorialSelectionService(uow=uow, model=model)
        inputs = await sel_service.select(run.id)

        assert len(inputs) == 1
        assert inputs[0].story_id == story_id
        assert inputs[0].presentation_intent == "unverified_operational"
        assert inputs[0].rank == 1

    async def test_selector_rejects_stories_not_in_candidate_set(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        # Seed valid story 1
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        s1_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) VALUES (%s, 1, 'open', 'Story 1', 'h1', %s) RETURNING id",
            (s1_id, _NOW),
        )
        s1_rev = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (s1_rev, s1_id)
        )

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-sel-reject-unknown",
        )
        await snap_service.seal_candidates(run.id)

        # Model proposes a fake/hallucinated story ID 99999
        model = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=99999,
                    story_revision_id=88888,
                    decision="INCLUDE",
                    presentation_intent="lead",
                ),
                SelectionProposal(
                    story_id=s1_id,
                    story_revision_id=s1_rev,
                    decision="INCLUDE",
                    presentation_intent="lead",
                ),
            ]
        )
        sel_service = EditorialSelectionService(uow=uow, model=model)
        inputs = await sel_service.select(run.id)

        # Only the valid candidate story was included; hallucinated was rejected
        assert len(inputs) == 1
        assert inputs[0].story_id == s1_id

    async def test_omitted_story_can_be_included_in_later_run(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        # Seed story
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) VALUES (%s, 1, 'open', 'Story A', 'ha', %s) RETURNING id",
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )

        # Run 1: OMIT story
        run1 = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-sel-run-1",
        )
        await snap_service.seal_candidates(run1.id)
        model1 = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=story_id, story_revision_id=rev_id, decision="OMIT", reason="Не влезло"
                )
            ]
        )
        inputs1 = await EditorialSelectionService(uow=uow, model=model1).select(run1.id)
        assert len(inputs1) == 0

        # Run 2 at later time: INCLUDE story
        later = _NOW + dt.timedelta(hours=2)
        run2 = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=later,
            request_key="test-sel-run-2",
        )
        await snap_service.seal_candidates(run2.id)
        model2 = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=story_id,
                    story_revision_id=rev_id,
                    decision="INCLUDE",
                    presentation_intent="normal",
                )
            ]
        )
        inputs2 = await EditorialSelectionService(uow=uow, model=model2).select(run2.id)
        assert len(inputs2) == 1
        assert inputs2[0].story_id == story_id
