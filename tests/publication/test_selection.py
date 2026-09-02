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
from tests.publication.conftest import seed_claim_for_story

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
        from tests.publication.conftest import seed_claim_for_story

        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

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
        from tests.publication.conftest import seed_claim_for_story

        await seed_claim_for_story(conn, edition.id, s1_id, _NOW)

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

    async def test_article_subjective_omit_is_coverage_overridden(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) "
            "VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions "
            "(story_id, revision_no, current_state, semantic_text, content_hash, created_at) "
            "VALUES (%s, 1, 'open', 'Небольшая, но полезная городская история', 'article-omit', %s) "
            "RETURNING id",
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s",
            (rev_id, story_id),
        )
        from tests.publication.conftest import seed_claim_for_story

        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-article-coverage-override",
        )
        await snap_service.seal_candidates(run.id)

        model = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=story_id,
                    story_revision_id=rev_id,
                    decision="OMIT",
                    reason="Too minor for the article",
                )
            ]
        )
        inputs = await EditorialSelectionService(uow=uow, model=model).select(
            run.id, defer_generation=False
        )

        assert [inp.story_id for inp in inputs] == [story_id]
        async with uow.transaction() as tx:
            row = await (
                await tx.execute(
                    "SELECT decision, metadata FROM publication_selection_decisions "
                    "WHERE publication_run_id = %s AND candidate_id IN "
                    "(SELECT id FROM publication_candidates WHERE publication_run_id = %s AND story_id = %s)",
                    (run.id, run.id, story_id),
                )
            ).fetchone()
        assert row[0] == "INCLUDE"
        assert row[1]["coverage_override"] is True
        assert row[1]["model_decision"] == "OMIT"

    async def test_article_hard_exclusion_overridden_to_brief_with_disagreement_metadata(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Test 4A: For article, selector OMIT with hard exclusion is overridden to INCLUDE + brief."""
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) "
            "VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions "
            "(story_id, revision_no, current_state, semantic_text, content_hash, created_at) "
            "VALUES (%s, 1, 'open', 'Чисто коммерческое объявление', 'article-hard-omit', %s) "
            "RETURNING id",
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s",
            (rev_id, story_id),
        )
        from tests.publication.conftest import seed_claim_for_story

        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-article-hard-exclusion",
        )
        await snap_service.seal_candidates(run.id)

        model = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=story_id,
                    story_revision_id=rev_id,
                    decision="OMIT",
                    reason="Commercial classified only",
                    exclusion_reason="commercial_classified",
                )
            ]
        )
        inputs = await EditorialSelectionService(uow=uow, model=model).select(
            run.id, defer_generation=False
        )

        assert len(inputs) == 1
        assert inputs[0].story_id == story_id
        async with uow.transaction() as tx:
            row = await (
                await tx.execute(
                    "SELECT decision, presentation_intent, metadata FROM publication_selection_decisions "
                    "WHERE publication_run_id = %s AND candidate_id IN "
                    "(SELECT id FROM publication_candidates WHERE publication_run_id = %s AND story_id = %s)",
                    (run.id, run.id, story_id),
                )
            ).fetchone()
        assert row[0] == "INCLUDE"
        assert row[1] == "brief"
        assert row[2]["coverage_override"] is True
        assert row[2]["disagreement_with_gate"] == "selector_hard_exclusion_override"
        assert row[2]["exclusion_reason"] == "commercial_classified"

    async def test_digest_hard_exclusion_overridden_to_normal_with_disagreement_metadata(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Test 4B: For DIGEST_PUBLICATION_TYPES, selector OMIT is overridden to INCLUDE + normal."""
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) "
            "VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions "
            "(story_id, revision_no, current_state, semantic_text, content_hash, created_at) "
            "VALUES (%s, 1, 'open', 'Слухи и частное объявление', 'digest-hard-omit', %s) "
            "RETURNING id",
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s",
            (rev_id, story_id),
        )
        from tests.publication.conftest import seed_claim_for_story

        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        for digest_type in ("digest_grouped", "digest", "digest_channel"):
            run = await snap_service.create_run(
                edition_id=edition.id,
                publication_type=digest_type,
                snapshot_at=_NOW,
                request_key=f"test-digest-hard-exclusion-{digest_type}",
            )
            await snap_service.seal_candidates(run.id)

            model = MockSelectionModel(
                [
                    SelectionProposal(
                        story_id=story_id,
                        story_revision_id=rev_id,
                        decision="OMIT",
                        reason="Private classified",
                        exclusion_reason="private_classified",
                    )
                ]
            )
            inputs = await EditorialSelectionService(uow=uow, model=model).select(
                run.id, defer_generation=False
            )

            assert len(inputs) == 1
            assert inputs[0].story_id == story_id
            async with uow.transaction() as tx:
                row = await (
                    await tx.execute(
                        "SELECT decision, presentation_intent, metadata FROM publication_selection_decisions "
                        "WHERE publication_run_id = %s AND candidate_id IN "
                        "(SELECT id FROM publication_candidates WHERE publication_run_id = %s AND story_id = %s)",
                        (run.id, run.id, story_id),
                    )
                ).fetchone()
            assert row[0] == "INCLUDE"
            assert row[1] == "normal"
            assert row[2]["coverage_override"] is True
            assert row[2]["disagreement_with_gate"] == "selector_hard_exclusion_override"

    async def test_overlay_zero_omission_for_unproposed_candidates(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Test 4C: Candidates completely unmentioned by selector are preserved (selected_input_count == candidate_count)."""
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) "
            "VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions "
            "(story_id, revision_no, current_state, semantic_text, content_hash, created_at) "
            "VALUES (%s, 1, 'open', 'Сюжет не предложенный моделью', 'unproposed-story', %s) "
            "RETURNING id",
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s",
            (rev_id, story_id),
        )
        from tests.publication.conftest import seed_claim_for_story

        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-unproposed-candidate-run",
        )
        cands = await snap_service.seal_candidates(run.id)
        assert len(cands) == 1

        # Model returns empty proposals
        model = MockSelectionModel([])
        inputs = await EditorialSelectionService(uow=uow, model=model).select(
            run.id, defer_generation=False
        )

        assert len(inputs) == len(cands)
        assert inputs[0].story_id == story_id
        async with uow.transaction() as tx:
            row = await (
                await tx.execute(
                    "SELECT decision, presentation_intent, metadata FROM publication_selection_decisions "
                    "WHERE publication_run_id = %s AND candidate_id IN "
                    "(SELECT id FROM publication_candidates WHERE publication_run_id = %s AND story_id = %s)",
                    (run.id, run.id, story_id),
                )
            ).fetchone()
        assert row[0] == "INCLUDE"
        assert row[1] == "brief"
        assert row[2]["coverage_override"] is True
        assert row[2]["disagreement_with_gate"] == "selector_unproposed_candidate"

    async def test_ai_selector_strict_json_and_validation(self):
        import json
        from unittest.mock import AsyncMock

        from src.ai_providers import AIProvider
        from src.publication.selection_ai import (
            AIPublicationSelectionModel,
            InvalidSelectionResponse,
        )

        mock_provider = AsyncMock(spec=AIProvider)
        selector = AIPublicationSelectionModel(provider=mock_provider)

        cand1 = PublicationCandidate(
            id=1,
            publication_run_id=1,
            story_id=10,
            story_revision_id=100,
            deterministic_rank=1,
            snapshot_features={},
            created_at=_NOW,
        )
        cand2 = PublicationCandidate(
            id=2,
            publication_run_id=1,
            story_id=20,
            story_revision_id=200,
            deterministic_rank=2,
            snapshot_features={},
            created_at=_NOW,
        )
        run = PublicationRun(
            id=1,
            edition_id=1,
            publication_type="digest_grouped",
            request_key="k",
            snapshot_at=_NOW,
            eligibility_policy_id=1,
            selection_policy_id=1,
            writer_policy_id=1,
            status="candidates_sealed",
            error_kind=None,
            metadata={},
            created_at=_NOW,
            completed_at=None,
        )

        # 1. Valid response with markdown wrapping
        valid_payload = {
            "proposals": [
                {
                    "story_id": 10,
                    "story_revision_id": 100,
                    "decision": "INCLUDE",
                    "presentation_intent": "lead",
                    "rank": 1,
                    "confidence": 0.95,
                    "reason": "Top news",
                },
                {
                    "story_id": 20,
                    "story_revision_id": 200,
                    "decision": "OMIT",
                    "presentation_intent": None,
                    "rank": None,
                    "confidence": 0.8,
                    "reason": "Not enough space",
                },
            ]
        }
        mock_provider.chat_completion.return_value = f"```json\n{json.dumps(valid_payload)}\n```"
        proposals = await selector.select_stories(run=run, candidates=[cand1, cand2])
        assert len(proposals) == 2
        assert proposals[0].story_id == 10
        assert proposals[0].decision == "INCLUDE"
        assert proposals[1].story_id == 20
        assert proposals[1].decision == "OMIT"

        # 2. Missing candidate 20
        invalid_missing = {
            "proposals": [
                {
                    "story_id": 10,
                    "story_revision_id": 100,
                    "decision": "INCLUDE",
                    "presentation_intent": "lead",
                }
            ]
        }
        mock_provider.chat_completion.return_value = json.dumps(invalid_missing)
        with pytest.raises(InvalidSelectionResponse, match="omitted decisions"):
            await selector.select_stories(run=run, candidates=[cand1, cand2])

        # 3. Unknown candidate
        invalid_unknown = {
            "proposals": [
                {"story_id": 10, "story_revision_id": 100, "decision": "INCLUDE"},
                {"story_id": 999, "story_revision_id": 888, "decision": "INCLUDE"},
            ]
        }
        mock_provider.chat_completion.return_value = json.dumps(invalid_unknown)
        with pytest.raises(InvalidSelectionResponse, match="unknown candidate"):
            await selector.select_stories(run=run, candidates=[cand1, cand2])

        # 4. Invalid decision enum
        invalid_enum = {
            "proposals": [
                {"story_id": 10, "story_revision_id": 100, "decision": "MAYBE"},
                {"story_id": 20, "story_revision_id": 200, "decision": "OMIT"},
            ]
        }
        mock_provider.chat_completion.return_value = json.dumps(invalid_enum)
        with pytest.raises(InvalidSelectionResponse, match="invalid decision"):
            await selector.select_stories(run=run, candidates=[cand1, cand2])

    async def test_fail_open_selection_model_falls_back_to_heuristic(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        from unittest.mock import AsyncMock

        from src.ai_providers import AIProvider
        from src.publication.selection_ai import (
            AIPublicationSelectionModel,
            FailOpenSelectionModel,
        )

        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        # Seed 2 stories
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        s1 = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) VALUES (%s, 1, 'open', 'Story 1', 'h1', %s) RETURNING id",
            (s1, _NOW),
        )
        r1 = (await cur.fetchone())[0]
        await conn.execute("UPDATE stories SET current_revision_id = %s WHERE id = %s", (r1, s1))

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        s2 = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) VALUES (%s, 1, 'open', 'Story 2', 'h2', %s) RETURNING id",
            (s2, _NOW),
        )
        r2 = (await cur.fetchone())[0]
        await conn.execute("UPDATE stories SET current_revision_id = %s WHERE id = %s", (r2, s2))

        from tests.publication.conftest import seed_claim_for_story

        await seed_claim_for_story(conn, edition.id, s1, _NOW)
        await seed_claim_for_story(conn, edition.id, s2, _NOW)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            request_key="test-fail-open-fallback",
        )
        await snap_service.seal_candidates(run.id)

        # Mock primary AI provider that crashes / fails
        mock_provider = AsyncMock(spec=AIProvider)
        mock_provider.chat_completion.side_effect = RuntimeError("OpenAI API unreachable 503")

        failing_ai_model = AIPublicationSelectionModel(provider=mock_provider)
        fail_open_model = FailOpenSelectionModel(primary=failing_ai_model)

        service = EditorialSelectionService(uow=uow, model=fail_open_model)
        inputs = await service.select(run.id)

        # Both stories must be included by fail-open fallback
        assert len(inputs) == 2
        included_story_ids = {inp.story_id for inp in inputs}
        assert included_story_ids == {s1, s2}

    async def test_orphan_story_without_claims_is_never_candidate_or_input(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """A story without attached claims must never become a candidate or selected input."""
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        # Seed story without claims
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        orphan_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Осиротевший сюжет', 'h-orphan', %s) RETURNING id
            """,
            (orphan_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, orphan_id)
        )

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-orphan-exclusion",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 0

        # Even if artificially proposed by selector, select must skip stories with 0 claims
        model = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=orphan_id,
                    story_revision_id=rev_id,
                    decision="INCLUDE",
                    presentation_intent="lead",
                )
            ]
        )
        sel_service = EditorialSelectionService(uow=uow, model=model)
        inputs = await sel_service.select(run.id)
        assert len(inputs) == 0

    async def test_valid_all_omit_does_not_suppress_single_source_low_risk_story(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """Valid all-OMIT response from primary AI must trigger fail-open fallback for eligible candidate."""
        import json
        from unittest.mock import AsyncMock

        from src.ai_providers import AIProvider
        from src.publication.selection_ai import (
            AIPublicationSelectionModel,
            FailOpenSelectionModel,
        )

        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        # 1. Seed single-source low-risk community story (e.g. Facebook comment)
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'На АКЗ возле почты появилась вода', 'h-water', %s) RETURNING id
            """,
            (story_id, _NOW),
        )
        story_rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (story_rev_id, story_id)
        )

        from tests.publication.conftest import seed_claim_for_story

        await seed_claim_for_story(conn, edition.id, story_id, _NOW, platform="facebook")

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="article",
            snapshot_at=_NOW,
            request_key="test-valid-all-omit-fallback",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 1
        assert candidates[0].story_id == story_id

        # 2. AI model returns completely valid JSON proposing OMIT because of single-source / lack of confirmation
        valid_omit_payload = {
            "proposals": [
                {
                    "story_id": story_id,
                    "story_revision_id": story_rev_id,
                    "decision": "OMIT",
                    "presentation_intent": None,
                    "rank": None,
                    "confidence": 0.9,
                    "reason": "not sufficiently confirmed / single community source",
                }
            ]
        }
        mock_provider = AsyncMock(spec=AIProvider)
        mock_provider.chat_completion.return_value = json.dumps(valid_omit_payload)

        # 3. Use production FailOpenSelectionModel with this AI provider
        ai_model = AIPublicationSelectionModel(provider=mock_provider)
        fail_open_selector = FailOpenSelectionModel(primary=ai_model)

        service = EditorialSelectionService(uow=uow, model=fail_open_selector)
        inputs = await service.select(run.id)

        # 4. Publication-first invariant: fail-open fallback must include the eligible candidate
        assert len(inputs) == 1
        assert inputs[0].story_id == story_id
        assert inputs[0].presentation_intent == "lead"

    async def test_digest_with_omit_overrides_to_include_with_metadata(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """For digest types, AI proposing OMIT is overridden to effective INCLUDE with metadata."""
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at)
            VALUES (%s, 1, 'open', 'Сюжет про транспорт', 'h-trans', %s) RETURNING id
            """,
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )
        from tests.publication.conftest import seed_claim_for_story

        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            request_key="test-digest-omit-override",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 1

        model = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=story_id,
                    story_revision_id=rev_id,
                    decision="OMIT",
                    presentation_intent=None,
                    confidence=0.8,
                    reason="Low priority in AI view",
                    rank=None,
                )
            ]
        )
        sel_service = EditorialSelectionService(uow=uow, model=model)
        inputs = await sel_service.select(run.id, defer_generation=False)

        assert len(inputs) == 1
        assert inputs[0].story_id == story_id
        assert inputs[0].rank == 1

        # Verify decision metadata stored in DB
        cur = await conn.execute(
            "SELECT decision, metadata FROM publication_selection_decisions WHERE publication_run_id = %s",
            (run.id,),
        )
        row = await cur.fetchone()
        assert row[0] == "INCLUDE"
        assert row[1].get("coverage_override") is True
        assert row[1].get("model_decision") == "OMIT"

    async def test_article_omit_is_overridden_to_brief_preserving_denominator(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """For article types, OMIT decision is overridden to brief preserving sealed candidate denominator."""
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)
        from tests.publication.conftest import seed_claim_for_story

        # Seed 2 stories
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story1_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) "
            "VALUES (%s, 1, 'open', 'Сюжет 1', 'h-1', %s) RETURNING id",
            (story1_id, _NOW),
        )
        rev1_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev1_id, story1_id)
        )
        await seed_claim_for_story(conn, edition.id, story1_id, _NOW)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story2_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) "
            "VALUES (%s, 1, 'open', 'Сюжет 2', 'h-2', %s) RETURNING id",
            (story2_id, _NOW),
        )
        rev2_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev2_id, story2_id)
        )
        await seed_claim_for_story(conn, edition.id, story2_id, _NOW)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="daily_article",
            snapshot_at=_NOW,
            request_key="test-article-partial-omit",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 2

        model = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=story1_id,
                    story_revision_id=rev1_id,
                    decision="INCLUDE",
                    presentation_intent="lead",
                    confidence=0.95,
                    rank=1,
                ),
                SelectionProposal(
                    story_id=story2_id,
                    story_revision_id=rev2_id,
                    decision="OMIT",
                    presentation_intent=None,
                    confidence=0.8,
                    reason="Commercial advertisement",
                    exclusion_reason="commercial_classified",
                ),
            ]
        )
        sel_service = EditorialSelectionService(uow=uow, model=model)
        inputs = await sel_service.select(run.id, defer_generation=False)

        assert len(inputs) == 2
        assert inputs[0].story_id == story1_id
        assert inputs[0].presentation_intent == "lead"
        assert inputs[1].story_id == story2_id
        assert inputs[1].presentation_intent == "brief"

    async def test_selection_ranks_order_inputs_correctly(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        """AI rank determines the final sequential order of frozen publication inputs."""
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)
        from tests.publication.conftest import seed_claim_for_story

        # Seed 2 stories
        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story1_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) "
            "VALUES (%s, 1, 'open', 'Сюжет 1', 'h-1', %s) RETURNING id",
            (story1_id, _NOW),
        )
        rev1_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev1_id, story1_id)
        )
        await seed_claim_for_story(conn, edition.id, story1_id, _NOW)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story2_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) "
            "VALUES (%s, 1, 'open', 'Сюжет 2', 'h-2', %s) RETURNING id",
            (story2_id, _NOW),
        )
        rev2_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev2_id, story2_id)
        )
        await seed_claim_for_story(conn, edition.id, story2_id, _NOW)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            request_key="test-ranking-order",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 2

        # AI assigns reversed ranks: story2 is rank 1, story1 is rank 2
        model = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=story1_id,
                    story_revision_id=rev1_id,
                    decision="INCLUDE",
                    presentation_intent="normal",
                    rank=2,
                ),
                SelectionProposal(
                    story_id=story2_id,
                    story_revision_id=rev2_id,
                    decision="INCLUDE",
                    presentation_intent="lead",
                    rank=1,
                ),
            ]
        )
        sel_service = EditorialSelectionService(uow=uow, model=model)
        inputs = await sel_service.select(run.id, defer_generation=False)

        assert len(inputs) == 2
        assert inputs[0].story_id == story2_id
        assert inputs[0].rank == 1
        assert inputs[1].story_id == story1_id
        assert inputs[1].rank == 2


class TestSelectionAIParserContracts:
    """Unit tests for AIPublicationSelectionModel JSON parser contracts."""

    def test_selector_parses_valid_commercial_classified_omit(self):
        from src.publication.selection_ai import AIPublicationSelectionModel

        model = AIPublicationSelectionModel(provider=None, config=None)
        candidates = [
            PublicationCandidate(
                id=1,
                publication_run_id=1,
                story_id=41,
                story_revision_id=87,
                deterministic_rank=1,
                snapshot_features={},
                created_at=_NOW,
            ),
            PublicationCandidate(
                id=2,
                publication_run_id=1,
                story_id=42,
                story_revision_id=88,
                deterministic_rank=2,
                snapshot_features={},
                created_at=_NOW,
            ),
        ]
        raw = """
        {
          "proposals": [
            {
              "story_id": 41,
              "story_revision_id": 87,
              "decision": "INCLUDE",
              "exclusion_reason": null,
              "presentation_intent": "lead",
              "rank": 1,
              "reason": "Major event"
            },
            {
              "story_id": 42,
              "story_revision_id": 88,
              "decision": "OMIT",
              "exclusion_reason": "commercial_classified",
              "reason": "Commercial ad"
            }
          ]
        }
        """
        proposals = model._parse_and_validate(raw, candidates)
        assert len(proposals) == 2
        assert proposals[0].decision == "INCLUDE"
        assert proposals[0].exclusion_reason is None
        assert proposals[1].decision == "OMIT"
        assert proposals[1].exclusion_reason == "commercial_classified"

    def test_selector_rejects_include_with_exclusion_reason(self):
        from src.publication.selection_ai import (
            AIPublicationSelectionModel,
            InvalidSelectionResponse,
        )

        model = AIPublicationSelectionModel(provider=None, config=None)
        candidates = [
            PublicationCandidate(
                id=1,
                publication_run_id=1,
                story_id=41,
                story_revision_id=87,
                deterministic_rank=1,
                snapshot_features={},
                created_at=_NOW,
            )
        ]
        raw = """
        {
          "proposals": [
            {
              "story_id": 41,
              "story_revision_id": 87,
              "decision": "INCLUDE",
              "exclusion_reason": "commercial_classified",
              "rank": 1
            }
          ]
        }
        """
        with pytest.raises(InvalidSelectionResponse, match="exclusion_reason must be null"):
            model._parse_and_validate(raw, candidates)

    def test_selector_rejects_unknown_exclusion_reason(self):
        from src.publication.selection_ai import (
            AIPublicationSelectionModel,
            InvalidSelectionResponse,
        )

        model = AIPublicationSelectionModel(provider=None, config=None)
        candidates = [
            PublicationCandidate(
                id=1,
                publication_run_id=1,
                story_id=41,
                story_revision_id=87,
                deterministic_rank=1,
                snapshot_features={},
                created_at=_NOW,
            )
        ]
        raw = """
        {
          "proposals": [
            {
              "story_id": 41,
              "story_revision_id": 87,
              "decision": "OMIT",
              "exclusion_reason": "random_noise_reason"
            }
          ]
        }
        """
        with pytest.raises(InvalidSelectionResponse, match="invalid exclusion_reason"):
            model._parse_and_validate(raw, candidates)


@pytest.mark.postgres
class TestSelectionPublishabilityAndFailOpen:
    """Integration tests for narrow publishability exclusion and fail-open normalization."""

    async def test_digest_commercial_classified_omit_overridden_to_normal(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) "
            "VALUES (%s, 1, 'open', 'Вода 3 рубля', 'h-ad', %s) RETURNING id",
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )
        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            request_key="test-comm-omit",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 1

        model = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=story_id,
                    story_revision_id=rev_id,
                    decision="OMIT",
                    exclusion_reason="commercial_classified",
                    reason="Pure commercial classified ad",
                )
            ]
        )
        sel_service = EditorialSelectionService(uow=uow, model=model)
        inputs = await sel_service.select(run.id, defer_generation=False)
        assert len(inputs) == 1
        assert inputs[0].story_id == story_id
        assert inputs[0].presentation_intent == "normal"

    async def test_digest_subjective_omit_is_overridden_to_include(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) "
            "VALUES (%s, 1, 'open', 'Свет на АКЗ', 'h-civic', %s) RETURNING id",
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )
        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            request_key="test-subj-override",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 1

        model = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=story_id,
                    story_revision_id=rev_id,
                    decision="OMIT",
                    exclusion_reason=None,
                    reason="Low priority brief",
                )
            ]
        )
        sel_service = EditorialSelectionService(uow=uow, model=model)
        inputs = await sel_service.select(run.id, defer_generation=False)
        assert len(inputs) == 1
        assert inputs[0].story_id == story_id

    async def test_article_commercial_omit_is_overridden_to_brief(
        self, conn: psycopg.AsyncConnection, pool, edition
    ):
        uow = DatabaseUnitOfWork(pool)
        snap_service = PublicationSnapshotService(uow=uow)

        cur = await conn.execute(
            "INSERT INTO stories (edition_id, lifecycle_state, created_at) VALUES (%s, 'active', %s) RETURNING id",
            (edition.id, _NOW),
        )
        story_id = (await cur.fetchone())[0]
        cur = await conn.execute(
            "INSERT INTO story_revisions (story_id, revision_no, current_state, semantic_text, content_hash, created_at) "
            "VALUES (%s, 1, 'open', 'Новость для статьи', 'h-art', %s) RETURNING id",
            (story_id, _NOW),
        )
        rev_id = (await cur.fetchone())[0]
        await conn.execute(
            "UPDATE stories SET current_revision_id = %s WHERE id = %s", (rev_id, story_id)
        )
        await seed_claim_for_story(conn, edition.id, story_id, _NOW)

        run = await snap_service.create_run(
            edition_id=edition.id,
            publication_type="daily_article",
            snapshot_at=_NOW,
            request_key="test-art-omit",
        )
        candidates = await snap_service.seal_candidates(run.id)
        assert len(candidates) == 1

        model = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=story_id,
                    story_revision_id=rev_id,
                    decision="OMIT",
                    reason="Commercial classified payload",
                    exclusion_reason="commercial_classified",
                )
            ]
        )
        sel_service = EditorialSelectionService(uow=uow, model=model)
        inputs = await sel_service.select(run.id, defer_generation=False)
        assert len(inputs) == 1
        assert inputs[0].story_id == story_id
        assert inputs[0].presentation_intent == "brief"

    async def test_fail_open_digest_all_commercial_omits_are_valid(self):
        from src.publication.selection_ai import FailOpenSelectionModel

        candidates = [
            PublicationCandidate(
                id=1,
                publication_run_id=1,
                story_id=41,
                story_revision_id=87,
                deterministic_rank=1,
                snapshot_features={},
                created_at=_NOW,
            )
        ]
        run = PublicationRun(
            id=1,
            edition_id=1,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            eligibility_policy_id=1,
            selection_policy_id=1,
            writer_policy_id=1,
            status="candidates_sealed",
            error_kind=None,
            metadata={},
            request_key="test-k",
            created_at=_NOW,
        )
        primary = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=41,
                    story_revision_id=87,
                    decision="OMIT",
                    exclusion_reason="commercial_classified",
                    reason="Commercial offer",
                )
            ]
        )
        fallback = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=41,
                    story_revision_id=87,
                    decision="INCLUDE",
                    reason="Fallback included",
                )
            ]
        )
        model = FailOpenSelectionModel(primary=primary, fallback=fallback)
        proposals = await model.select_stories(run=run, candidates=candidates)

        assert len(proposals) == 1
        assert proposals[0].decision == "OMIT"
        assert proposals[0].exclusion_reason == "commercial_classified"

    async def test_fail_open_digest_zero_includes_without_hard_reasons_uses_heuristic(self):
        from src.publication.selection_ai import FailOpenSelectionModel

        candidates = [
            PublicationCandidate(
                id=1,
                publication_run_id=1,
                story_id=41,
                story_revision_id=87,
                deterministic_rank=1,
                snapshot_features={},
                created_at=_NOW,
            )
        ]
        run = PublicationRun(
            id=1,
            edition_id=1,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            eligibility_policy_id=1,
            selection_policy_id=1,
            writer_policy_id=1,
            status="candidates_sealed",
            error_kind=None,
            metadata={},
            request_key="test-k2",
            created_at=_NOW,
        )
        # Primary returns subjective OMIT without hard exclusion reason
        primary = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=41,
                    story_revision_id=87,
                    decision="OMIT",
                    exclusion_reason=None,
                    reason="Subjectively omitted",
                )
            ]
        )
        fallback = MockSelectionModel(
            [
                SelectionProposal(
                    story_id=41,
                    story_revision_id=87,
                    decision="INCLUDE",
                    reason="Fallback heuristic included",
                )
            ]
        )
        model = FailOpenSelectionModel(primary=primary, fallback=fallback)
        proposals = await model.select_stories(run=run, candidates=candidates)

        assert len(proposals) == 1
        assert proposals[0].decision == "INCLUDE"
        assert proposals[0].reason == "Fallback heuristic included"

    async def test_selection_prompt_includes_scope_contract(self):
        from src.publication.selection_ai import AIPublicationSelectionModel

        class CapturingProvider:
            def __init__(self):
                self.messages = None

            async def chat_completion(self, messages, model, **kwargs):
                self.messages = messages
                return '{"proposals": [{"story_id": 1, "story_revision_id": 1, "decision": "INCLUDE", "presentation_intent": "normal", "confidence": 0.9, "reason": "Local news"}]}'

        provider = CapturingProvider()
        contract_text = "GEOGRAPHIC SCOPE CONTRACT for Berdyansk Edition"
        ai_model = AIPublicationSelectionModel(
            provider=provider,
            model_name="dummy",
            scope_contract=contract_text,
        )

        run = PublicationRun(
            id=1,
            edition_id=1,
            publication_type="digest_grouped",
            snapshot_at=_NOW,
            eligibility_policy_id=1,
            selection_policy_id=1,
            writer_policy_id=1,
            status="candidates_sealed",
            error_kind=None,
            metadata={},
            request_key="test-k-scope",
            created_at=_NOW,
        )
        cand = PublicationCandidate(
            id=1,
            publication_run_id=1,
            story_id=1,
            story_revision_id=1,
            deterministic_rank=1,
            snapshot_features={},
            created_at=_NOW,
        )

        await ai_model.select_stories(run=run, candidates=[cand])
        assert provider.messages is not None
        user_msg = next(m["content"] for m in provider.messages if m["role"] == "user")
        assert "GEOGRAPHIC SCOPE CONTRACT for Berdyansk Edition" in user_msg
