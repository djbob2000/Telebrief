"""Constraint and repository tests for claim persistence (Plan 3 Task 1).

Covers spec §15/§16 shapes: canonical claim extraction runs (one successful
run per (source_item_revision_id, edition_id, extraction_policy_id)),
processing_attempts audit history, immutable claims with the temporal model,
claim relations, and reconstructable claim state events.
"""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from src.domain.claims import (
    Claim,
    ClaimExtractionPolicyVersion,
    ClaimExtractionRun,
    ClaimRelation,
    ClaimStateEvent,
    NewClaim,
    ProcessingAttempt,
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

RUN_REPO = ClaimExtractionRunRepository()
CLAIM_REPO = ClaimRepository()
POLICY_REPO = ClaimExtractionPolicyRepository()
DECISION_REPO = EditionRelevanceDecisionRepository()


async def _insert_extraction_policy(
    conn: psycopg.AsyncConnection, edition_id: int, version: int = 1
) -> ClaimExtractionPolicyVersion:
    return await POLICY_REPO.insert(
        conn,
        edition_id=edition_id,
        version=version,
        config_hash="claims-cfg",
        prompt_version="claims-prompt-1",
    )


async def _insert_relevant_decision(
    conn: psycopg.AsyncConnection, edition_id: int, revision_id: int
) -> int:
    """Spec §15 chain: an extraction run always follows a relevance decision."""
    relevance_policy = await RelevancePolicyVersionRepository().insert(
        conn,
        edition_id=edition_id,
        version=1,
        config_hash="relevance-cfg",
        prompt_version="relevance-prompt-1",
    )
    decision = await DECISION_REPO.insert_root(
        conn,
        source_item_revision_id=revision_id,
        edition_id=edition_id,
        relevance_policy_id=relevance_policy.id,
        status="relevant",
        confidence=None,
        reason="test setup",
    )
    return decision.id


async def _create_running_run(
    conn: psycopg.AsyncConnection,
    *,
    edition_id: int,
    revision_id: int,
    policy_id: int,
    relevance_decision_id: int,
) -> tuple[ClaimExtractionRun, bool]:
    return await RUN_REPO.get_or_create_run(
        conn,
        source_item_revision_id=revision_id,
        edition_id=edition_id,
        extraction_policy_id=policy_id,
        relevance_decision_id=relevance_decision_id,
    )


class TestClaimExtractionPolicies:
    async def test_insert_and_get_roundtrip(self, conn, edition):
        policy = await _insert_extraction_policy(conn, edition.id)

        assert isinstance(policy, ClaimExtractionPolicyVersion)
        assert policy.id > 0
        assert policy.edition_id == edition.id
        assert policy.version == 1
        assert policy.config_hash == "claims-cfg"
        assert policy.prompt_version == "claims-prompt-1"

    async def test_version_unique_per_edition(self, conn, edition):
        await _insert_extraction_policy(conn, edition.id, version=1)

        with pytest.raises(psycopg.errors.UniqueViolation):
            await _insert_extraction_policy(conn, edition.id, version=1)


class TestCanonicalExtractionRuns:
    async def test_get_or_create_creates_running_run(self, conn, edition, revision):
        policy = await _insert_extraction_policy(conn, edition.id)
        decision_id = await _insert_relevant_decision(conn, edition.id, revision.id)

        run, created = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )

        assert created is True
        assert isinstance(run, ClaimExtractionRun)
        assert run.id > 0
        assert run.source_item_revision_id == revision.id
        assert run.edition_id == edition.id
        assert run.extraction_policy_id == policy.id
        assert run.relevance_decision_id == decision_id
        assert run.status == "running"
        assert run.completed_at is None

    async def test_get_or_create_returns_existing_canonical_success(self, conn, edition, revision):
        """At-least-once task execution observes the canonical succeeded run
        instead of creating a duplicate."""
        policy = await _insert_extraction_policy(conn, edition.id)
        decision_id = await _insert_relevant_decision(conn, edition.id, revision.id)
        run, _ = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )
        assert await RUN_REPO.mark_succeeded(conn, run.id, completed_at=dt.datetime.now(dt.UTC))

        again, created = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )

        assert created is False
        assert again.id == run.id
        assert again.status == "succeeded"

    async def test_duplicate_successful_run_rejected_by_partial_index(
        self, conn, edition, revision
    ):
        """The partial unique index is the authoritative canonical constraint:
        a second succeeded row for the same key cannot exist even via raw SQL."""
        policy = await _insert_extraction_policy(conn, edition.id)
        decision_id = await _insert_relevant_decision(conn, edition.id, revision.id)
        # Two concurrent executions both open a running run (nothing is
        # canonical yet); only one of them may ever win the success slot.
        first, _ = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )
        second, _ = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )
        assert first.id != second.id
        await RUN_REPO.mark_succeeded(conn, first.id, completed_at=dt.datetime.now(dt.UTC))

        with pytest.raises(psycopg.errors.UniqueViolation):
            await conn.execute(
                """
                UPDATE claim_extraction_runs
                SET status = 'succeeded', completed_at = now()
                WHERE id = %s
                """,
                (second.id,),
            )

    async def test_failed_run_allowed_then_new_run_succeeds(self, conn, edition, revision):
        """A failed run is not canonical: retries create a fresh run which may
        succeed."""
        policy = await _insert_extraction_policy(conn, edition.id)
        decision_id = await _insert_relevant_decision(conn, edition.id, revision.id)
        failed, _ = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )
        await RUN_REPO.mark_failed(
            conn,
            failed.id,
            completed_at=dt.datetime.now(dt.UTC),
            error_kind="provider_timeout",
        )

        retry, created = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )

        assert created is True
        assert retry.id != failed.id
        assert await RUN_REPO.mark_succeeded(conn, retry.id, completed_at=dt.datetime.now(dt.UTC))

    async def test_new_policy_version_allows_new_success(self, conn, edition, revision):
        """Reinterpretation requires a new policy version; the canonical key
        includes the policy id."""
        policy_v1 = await _insert_extraction_policy(conn, edition.id, version=1)
        decision_id = await _insert_relevant_decision(conn, edition.id, revision.id)
        run_v1, _ = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy_v1.id,
            relevance_decision_id=decision_id,
        )
        await RUN_REPO.mark_succeeded(conn, run_v1.id, completed_at=dt.datetime.now(dt.UTC))

        policy_v2 = await _insert_extraction_policy(conn, edition.id, version=2)
        run_v2, created = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy_v2.id,
            relevance_decision_id=decision_id,
        )

        assert created is True
        assert await RUN_REPO.mark_succeeded(conn, run_v2.id, completed_at=dt.datetime.now(dt.UTC))

    async def test_extraction_policy_edition_mismatch_rejected(
        self, conn, edition, second_edition, revision
    ):
        policy = await _insert_extraction_policy(conn, second_edition.id)
        foreign_decision_id = await _insert_relevant_decision(conn, second_edition.id, revision.id)

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(
                """
                INSERT INTO claim_extraction_runs (
                    source_item_revision_id, edition_id, extraction_policy_id,
                    relevance_decision_id, status
                )
                VALUES (%s, %s, %s, %s, 'running')
                """,
                # Same policy id, but a different edition: the composite pair
                # (extraction_policy_id, edition_id) does not exist.
                (revision.id, edition.id, policy.id, foreign_decision_id),
            )

    async def test_mark_succeeded_returns_false_when_canonical_slot_taken(
        self, conn, edition, revision
    ):
        policy = await _insert_extraction_policy(conn, edition.id)
        decision_id = await _insert_relevant_decision(conn, edition.id, revision.id)
        first, _ = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )
        second, _ = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )
        assert await RUN_REPO.mark_succeeded(conn, first.id, completed_at=dt.datetime.now(dt.UTC))

        assert not await RUN_REPO.mark_succeeded(
            conn, second.id, completed_at=dt.datetime.now(dt.UTC)
        )

        still_running = await RUN_REPO.get(conn, second.id)
        assert still_running.status == "running"


class TestProcessingAttempts:
    async def _running_run(self, conn, edition, revision) -> ClaimExtractionRun:
        policy = await _insert_extraction_policy(conn, edition.id)
        decision_id = await _insert_relevant_decision(conn, edition.id, revision.id)
        run, _ = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )
        return run

    async def test_start_attempt_increments_attempt_no(self, conn, edition, revision):
        run = await self._running_run(conn, edition, revision)

        first = await RUN_REPO.start_attempt(
            conn,
            stage="claim_extraction",
            semantic_run_id=run.id,
            provider="openai",
            model="gpt-5-nano",
        )
        second = await RUN_REPO.start_attempt(
            conn,
            stage="claim_extraction",
            semantic_run_id=run.id,
            provider="anthropic",
            model="claude-haiku",
        )

        assert isinstance(first, ProcessingAttempt)
        assert first.attempt_no == 1
        assert second.attempt_no == 2
        assert second.semantic_run_id == run.id
        assert second.status == "running"

    async def test_attempt_key_unique(self, conn, edition, revision):
        run = await self._running_run(conn, edition, revision)
        await RUN_REPO.start_attempt(conn, stage="claim_extraction", semantic_run_id=run.id)

        with pytest.raises(psycopg.errors.UniqueViolation):
            await conn.execute(
                """
                INSERT INTO processing_attempts (stage, semantic_run_id, attempt_no, status)
                VALUES ('claim_extraction', %s, 1, 'running')
                """,
                (run.id,),
            )

    async def test_finish_attempt_records_outcome(self, conn, edition, revision):
        run = await self._running_run(conn, edition, revision)
        attempt = await RUN_REPO.start_attempt(
            conn, stage="claim_extraction", semantic_run_id=run.id, provider="openai"
        )

        completed_at = dt.datetime.now(dt.UTC)
        await RUN_REPO.finish_attempt(
            conn,
            attempt,
            status="failed",
            completed_at=completed_at,
            error_kind="provider_timeout",
        )

        cursor = await conn.execute(
            """
            SELECT status, completed_at, error_kind
            FROM processing_attempts
            WHERE stage = %s AND semantic_run_id = %s AND attempt_no = %s
            """,
            (attempt.stage, attempt.semantic_run_id, attempt.attempt_no),
        )
        status, stored_completed_at, error_kind = await cursor.fetchone()
        assert status == "failed"
        assert stored_completed_at == completed_at
        assert error_kind == "provider_timeout"


class TestClaims:
    async def _succeeded_run_with_claims(self, conn, edition, revision) -> list[Claim]:
        policy = await _insert_extraction_policy(conn, edition.id)
        decision_id = await _insert_relevant_decision(conn, edition.id, revision.id)
        run, _ = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )
        await RUN_REPO.mark_succeeded(conn, run.id, completed_at=dt.datetime.now(dt.UTC))
        return await CLAIM_REPO.insert_claims(
            conn,
            run=run,
            claims=[
                NewClaim(
                    assertion_text="На АКЗ возле почты вода уже появилась",
                    normalized_assertion=(
                        "По сообщению местного жителя, водоснабжение на АКЗ восстановилось."
                    ),
                    event_time_start=dt.datetime(2026, 8, 22, 8, 0, tzinfo=dt.UTC),
                    event_time_end=dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.UTC),
                    event_time_precision="hour",
                    event_time_confidence=0.7,
                    event_time_original_text="с восьми утра",
                ),
                NewClaim(
                    assertion_text="Школа №3 открывается 1 сентября",
                    normalized_assertion="Школа №3 в Бердянске откроется 1 сентября.",
                ),
            ],
        )

    async def test_insert_claims_batch_with_provenance(self, conn, edition, revision):
        claims = await self._succeeded_run_with_claims(conn, edition, revision)

        assert len(claims) == 2
        first, second = claims
        assert isinstance(first, Claim)
        assert first.id > 0
        assert first.claim_extraction_run_id > 0
        assert first.source_item_revision_id == revision.id
        assert first.edition_id == edition.id
        assert first.event_time_start is not None
        assert first.event_time_original_text == "с восьми утра"
        assert second.event_time_start is None
        assert second.event_time_precision is None

    async def test_get_many_roundtrip(self, conn, edition, revision):
        inserted = await self._succeeded_run_with_claims(conn, edition, revision)

        fetched = await CLAIM_REPO.get_many(conn, [claim.id for claim in inserted])

        assert fetched == inserted

    async def test_attach_relation_roundtrip_and_check(self, conn, edition, revision):
        inserted = await self._succeeded_run_with_claims(conn, edition, revision)
        original, correction = inserted[0], inserted[1]

        relation = await CLAIM_REPO.attach_relation(
            conn,
            from_claim_id=correction.id,
            to_claim_id=original.id,
            relation_type="CORRECTS",
        )

        assert isinstance(relation, ClaimRelation)
        assert relation.from_claim_id == correction.id
        assert relation.to_claim_id == original.id
        assert relation.relation_type == "CORRECTS"

        with pytest.raises(psycopg.errors.CheckViolation):
            await CLAIM_REPO.attach_relation(
                conn,
                from_claim_id=correction.id,
                to_claim_id=original.id,
                relation_type="MENTIONS",
            )

    async def test_insert_state_event_roundtrip(self, conn, edition, revision):
        inserted = await self._succeeded_run_with_claims(conn, edition, revision)
        claim = inserted[0]
        observed_at = dt.datetime.now(dt.UTC)

        event = await CLAIM_REPO.insert_state_event(
            conn,
            claim_id=claim.id,
            type="superseded",
            observed_at=observed_at,
            reason="newer claim CORRECTS this one",
            evidence={"relation_id": 1},
        )

        assert isinstance(event, ClaimStateEvent)
        assert event.claim_id == claim.id
        assert event.type == "superseded"
        assert event.observed_at == observed_at
        assert event.evidence == {"relation_id": 1}
