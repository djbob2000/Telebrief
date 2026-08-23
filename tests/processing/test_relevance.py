"""Constraint and repository tests for relevance policy persistence (Plan 3 Task 1).

Covers spec §12-14 shapes: versioned relevance policies bound to an edition,
immutable edition relevance decisions with root/child structure, the
editions.current_relevance_policy_id pointer with its edition-consistent
composite FK, and the minimal vision policy table.
"""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from src.domain.claims import EditionRelevanceDecision, RelevancePolicyVersion, VisionPolicyVersion
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    RelevancePolicyVersionRepository,
    VisionPolicyRepository,
)


async def _insert_policy(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    version: int = 1,
) -> RelevancePolicyVersion:
    return await RelevancePolicyVersionRepository().insert(
        conn,
        edition_id=edition_id,
        version=version,
        config_hash="cfg-abc",
        prompt_version="relevance-prompt-1",
    )


class TestRelevancePolicyVersions:
    async def test_insert_and_get_roundtrip(self, conn, edition):
        repo = RelevancePolicyVersionRepository()

        policy = await _insert_policy(conn, edition.id)

        assert isinstance(policy, RelevancePolicyVersion)
        assert policy.id > 0
        assert policy.edition_id == edition.id
        assert policy.version == 1
        assert policy.config_hash == "cfg-abc"
        assert policy.prompt_version == "relevance-prompt-1"
        assert isinstance(policy.created_at, dt.datetime)

        fetched = await repo.get(conn, policy.id)
        assert fetched == policy

    async def test_version_unique_per_edition(self, conn, edition):
        await _insert_policy(conn, edition.id, version=1)

        with pytest.raises(psycopg.errors.UniqueViolation):
            await _insert_policy(conn, edition.id, version=1)

    async def test_same_version_across_editions_allowed(self, conn, edition, second_edition):
        first = await _insert_policy(conn, edition.id, version=1)
        second = await _insert_policy(conn, second_edition.id, version=1)

        assert first.version == second.version == 1
        assert first.edition_id != second.edition_id

    async def test_list_for_edition_orders_by_version(self, conn, edition, second_edition):
        repo = RelevancePolicyVersionRepository()
        await _insert_policy(conn, edition.id, version=2)
        await _insert_policy(conn, second_edition.id, version=1)
        await _insert_policy(conn, edition.id, version=1)

        listed = await repo.list_for_edition(conn, edition.id)

        assert [policy.version for policy in listed] == [1, 2]
        assert all(policy.edition_id == edition.id for policy in listed)


class TestEditionsCurrentRelevancePointer:
    async def test_set_and_get_current(self, conn, edition):
        policy_repo = RelevancePolicyVersionRepository()
        policy = await _insert_policy(conn, edition.id)

        assert await policy_repo.get_current(conn, edition.id) is None

        await policy_repo.set_current(conn, edition_id=edition.id, policy_id=policy.id)

        assert await policy_repo.get_current(conn, edition.id) == policy

    async def test_clear_current(self, conn, edition):
        policy_repo = RelevancePolicyVersionRepository()
        policy = await _insert_policy(conn, edition.id)
        await policy_repo.set_current(conn, edition_id=edition.id, policy_id=policy.id)

        await policy_repo.clear_current(conn, edition_id=edition.id)

        assert await policy_repo.get_current(conn, edition.id) is None

    async def test_mismatched_edition_policy_pair_rejected(self, conn, edition, second_edition):
        """Composite FK (current_relevance_policy_id, id) must reject a pointer
        whose policy belongs to a different edition."""
        policy_repo = RelevancePolicyVersionRepository()
        foreign_policy = await _insert_policy(conn, second_edition.id)

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await policy_repo.set_current(conn, edition_id=edition.id, policy_id=foreign_policy.id)

        assert await policy_repo.get_current(conn, edition.id) is None


class TestEditionRelevanceDecisions:
    async def test_insert_root_and_get_roundtrip(self, conn, edition, revision):
        policy = await _insert_policy(conn, edition.id)
        repo = EditionRelevanceDecisionRepository()

        decision = await repo.insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=policy.id,
            status="relevant",
            confidence=0.9,
            reason="local resident report about water supply",
            provider="openai",
            model="gpt-5-nano",
        )

        assert isinstance(decision, EditionRelevanceDecision)
        assert decision.id > 0
        assert decision.source_item_revision_id == revision.id
        assert decision.edition_id == edition.id
        assert decision.relevance_policy_id == policy.id
        assert decision.status == "relevant"
        assert decision.confidence == pytest.approx(0.9)
        assert decision.parent_decision_id is None
        assert decision.created_at is not None

        fetched = await repo.get(conn, decision.id)
        assert fetched == decision

    async def test_policy_edition_mismatch_rejected(self, conn, edition, second_edition, revision):
        """Decisions must reference a policy of the SAME edition via the
        composite FK (relevance_policy_id, edition_id)."""
        policy = await _insert_policy(conn, second_edition.id)
        repo = EditionRelevanceDecisionRepository()

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await repo.insert_root(
                conn,
                source_item_revision_id=revision.id,
                edition_id=edition.id,
                relevance_policy_id=policy.id,
                status="relevant",
                confidence=None,
                reason="mismatched policy",
            )

    async def test_root_decision_uniqueness_enforced(self, conn, edition, revision):
        policy = await _insert_policy(conn, edition.id)
        repo = EditionRelevanceDecisionRepository()
        await repo.insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=policy.id,
            status="uncertain",
            confidence=None,
            reason="first root",
        )

        with pytest.raises(psycopg.errors.UniqueViolation):
            await repo.insert_root(
                conn,
                source_item_revision_id=revision.id,
                edition_id=edition.id,
                relevance_policy_id=policy.id,
                status="relevant",
                confidence=None,
                reason="duplicate root",
            )

    async def test_child_decision_allowed_pointing_at_needs_media_parent(
        self, conn, edition, revision
    ):
        """A post-vision decision is a new immutable child of the prior
        needs_media root; the root uniqueness does not apply to children."""
        policy = await _insert_policy(conn, edition.id)
        repo = EditionRelevanceDecisionRepository()
        parent = await repo.insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=policy.id,
            status="needs_media",
            confidence=None,
            reason="photo without usable text",
        )

        child = await repo.insert_child(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=policy.id,
            status="relevant",
            confidence=0.8,
            reason="vision observations confirm local event",
            parent_decision_id=parent.id,
        )

        assert child.parent_decision_id == parent.id
        assert child.status == "relevant"

    async def test_invalid_status_rejected(self, conn, edition, revision):
        policy = await _insert_policy(conn, edition.id)
        repo = EditionRelevanceDecisionRepository()

        with pytest.raises(psycopg.errors.CheckViolation):
            await repo.insert_root(
                conn,
                source_item_revision_id=revision.id,
                edition_id=edition.id,
                relevance_policy_id=policy.id,
                status="maybe",
                confidence=None,
                reason="bad status",
            )

    async def test_latest_for_revision_edition(self, conn, edition, revision):
        policy = await _insert_policy(conn, edition.id)
        repo = EditionRelevanceDecisionRepository()
        root = await repo.insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=policy.id,
            status="needs_media",
            confidence=None,
            reason="root",
        )

        latest = await repo.latest_for_revision_edition(
            conn, source_item_revision_id=revision.id, edition_id=edition.id
        )
        assert latest == root

        child = await repo.insert_child(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=policy.id,
            status="irrelevant",
            confidence=None,
            reason="child",
            parent_decision_id=root.id,
        )

        latest = await repo.latest_for_revision_edition(
            conn, source_item_revision_id=revision.id, edition_id=edition.id
        )
        assert latest == child

    async def test_latest_returns_none_without_decisions(self, conn, edition, revision):
        repo = EditionRelevanceDecisionRepository()

        latest = await repo.latest_for_revision_edition(
            conn, source_item_revision_id=revision.id, edition_id=edition.id
        )

        assert latest is None


class TestVisionPolicies:
    async def test_insert_and_get_roundtrip(self, conn, edition):
        repo = VisionPolicyRepository()

        policy = await repo.insert(
            conn,
            edition_id=edition.id,
            version=1,
            mode="relevance_only",
            config_hash="vision-cfg",
            prompt_version="vision-prompt-1",
        )

        assert isinstance(policy, VisionPolicyVersion)
        assert policy.id > 0
        assert policy.edition_id == edition.id
        assert policy.mode == "relevance_only"
        assert policy.config_hash == "vision-cfg"
        assert policy.prompt_version == "vision-prompt-1"

        assert await repo.get(conn, policy.id) == policy

    async def test_invalid_mode_rejected(self, conn, edition):
        repo = VisionPolicyRepository()

        with pytest.raises(psycopg.errors.CheckViolation):
            await repo.insert(
                conn,
                edition_id=edition.id,
                version=1,
                mode="sometimes",
                config_hash="vision-cfg",
                prompt_version="vision-prompt-1",
            )

    async def test_vision_run_policy_edition_mismatch_rejected(
        self, conn, edition, second_edition, revision
    ):
        """Raw-SQL guard: vision_analysis_runs references its policy through
        the composite FK (policy_id, edition_id) even though the service lands
        in Task 3."""
        policy = await VisionPolicyRepository().insert(
            conn,
            edition_id=second_edition.id,
            version=1,
            mode="relevance_only",
            config_hash="vision-cfg",
            prompt_version="vision-prompt-1",
        )

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(
                """
                INSERT INTO vision_analysis_runs (
                    source_item_revision_id, edition_id, relevance_decision_id,
                    policy_id, status
                )
                VALUES (%s, %s, NULL, %s, 'running')
                """,
                # Same policy id, but a different edition: the composite pair
                # (policy_id, edition_id) does not exist.
                (revision.id, edition.id, policy.id),
            )
