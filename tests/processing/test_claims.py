"""Constraint and repository tests for claim persistence (Plan 3 Task 1).

Covers spec §15/§16 shapes: canonical claim extraction runs (one successful
run per (source_item_revision_id, edition_id, extraction_policy_id)),
processing_attempts audit history, immutable claims with the temporal model,
claim relations, and reconstructable claim state events.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import psycopg
import pytest

from src.ai_providers import AIProvider, ProviderCascadeError
from src.domain.claims import (
    Claim,
    ClaimExtractionPolicyVersion,
    ClaimExtractionRun,
    ClaimRelation,
    ClaimStateEvent,
    NewClaim,
    ProcessingAttempt,
)
from src.processing.claims import (
    CLAIM_EXTRACTION_PROMPT_VERSION,
    CONTEXT_CHAR_BUDGET,
    ClaimExtractionContextBuilder,
    ClaimExtractionPolicyService,
    ClaimExtractionService,
)
from src.processing.relevance import (
    ProviderUnavailableError,
    RelevanceService,
    TransientProcessingError,
)
from src.processing.vision import (
    VisionObservationDraft,
    VisionOutcome,
    VisionPolicyService,
    VisionService,
)
from src.repositories.claims import (
    ClaimExtractionPolicyRepository,
    ClaimExtractionRunRepository,
    ClaimRepository,
)
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    RelevancePolicyVersionRepository,
    VisionPolicyRepository,
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


_RELEVANCE_POLICY_COUNTER = {"n": 0}


async def _insert_relevant_decision(
    conn: psycopg.AsyncConnection, edition_id: int, revision_id: int
) -> int:
    """Spec §15 chain: an extraction run always follows a relevance decision."""
    _RELEVANCE_POLICY_COUNTER["n"] += 1
    n = _RELEVANCE_POLICY_COUNTER["n"]
    relevance_policy = await RelevancePolicyVersionRepository().insert(
        conn,
        edition_id=edition_id,
        version=n,
        config_hash=f"relevance-cfg-{n}",
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

    async def test_mark_failed_never_demotes_canonical_winner(self, conn, edition, revision):
        """TOCTOU guard: a concurrent loser must not demote a just-committed
        succeeded run into failed — that would orphan its claims and let
        backfill extract permanent duplicates."""
        policy = await _insert_extraction_policy(conn, edition.id)
        decision_id = await _insert_relevant_decision(conn, edition.id, revision.id)
        winner, _ = await _create_running_run(
            conn,
            edition_id=edition.id,
            revision_id=revision.id,
            policy_id=policy.id,
            relevance_decision_id=decision_id,
        )
        assert await RUN_REPO.mark_succeeded(conn, winner.id, completed_at=dt.datetime.now(dt.UTC))

        assert not await RUN_REPO.mark_failed(
            conn, winner.id, error_kind="provider_unavailable", completed_at=dt.datetime.now(dt.UTC)
        )
        assert not await RUN_REPO.mark_failed(
            conn, winner.id, error_kind=None, completed_at=dt.datetime.now(dt.UTC)
        )

        fetched = await RUN_REPO.get(conn, winner.id)
        assert fetched.status == "succeeded"
        assert fetched.error_kind is None


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


# ---------------------------------------------------------------------------
# Plan 3 Task 4: Claim extraction service, bounded context builder, handoff
# wiring, corrections, and backfill.
#
# Prompt contract under test (brief Step 1): every normalized_assertion must
# be a self-contained semantic proposition (never a bare deictic fragment such
# as «Всё ещё нет»), short complete claims like «Пожар у вокзала.» stay valid
# (no minimum-word/sentence heuristics), and independent propositions become
# separate claims — semantic, not sentence-count based.
# ---------------------------------------------------------------------------


class ScriptedClaimProvider(AIProvider):
    """Deterministic offline chat provider replaying scripted JSON payloads."""

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[SimpleNamespace] = []

    async def chat_completion(  # pylint: disable=too-many-positional-arguments
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
        thinking: bool | None = None,
        response_format: dict | None = None,
    ) -> str:
        self.calls.append(
            SimpleNamespace(
                messages=[dict(message) for message in messages],
                model=model,
                response_format=response_format,
            )
        )
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, str)
        return outcome


def _claim_entry(
    assertion: str,
    normalized: str,
    *,
    place_mentions: list[str] | None = None,
    entities: list[str] | None = None,
    **event_time: object,
) -> dict:
    entry: dict = {"assertion_text": assertion, "normalized_assertion": normalized}
    if place_mentions is not None:
        entry["place_mentions"] = place_mentions
    if entities is not None:
        entry["entities"] = entities
    if event_time:
        entry["event_time"] = event_time
    return entry


def _claims_payload(*entries: dict) -> str:
    return json.dumps({"claims": list(entries)}, ensure_ascii=False)


def _extraction_service(uow, provider, **overrides) -> ClaimExtractionService:
    values = {
        "uow": uow,
        "provider": provider,
        "model": "test-model",
        "provider_name": "fake",
    }
    values.update(overrides)
    return ClaimExtractionService(**values)


async def _insert_reply_thread(
    conn: psycopg.AsyncConnection, *, parent_text: str, reply_text: str
) -> SimpleNamespace:
    """Parent message plus a linked reply item (parent_item_id/root_item_id)."""
    cursor = await conn.execute(
        """
        INSERT INTO sources (platform, kind, external_id, url, name)
        VALUES ('telegram', 'channel', 'thread-src', 'https://t.me/example', 'Thread')
        RETURNING id
        """
    )
    source_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', 'p1', now())
        RETURNING id
        """,
        (source_id,),
    )
    parent_item_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'parent-hash', %s)
        RETURNING id
        """,
        (parent_item_id, parent_text),
    )
    parent_revision_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_items (
            source_id, kind, external_id, parent_item_id, root_item_id, first_collected_at
        )
        VALUES (%s, 'message', 'r1', %s, %s, now())
        RETURNING id
        """,
        (source_id, parent_item_id, parent_item_id),
    )
    reply_item_id = int((await cursor.fetchone())[0])
    cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (source_item_id, revision_no, content_hash, text_content)
        VALUES (%s, 1, 'reply-hash', %s)
        RETURNING id
        """,
        (reply_item_id, reply_text),
    )
    reply_revision_id = int((await cursor.fetchone())[0])
    return SimpleNamespace(
        source_id=source_id,
        parent_item_id=parent_item_id,
        parent_revision_id=parent_revision_id,
        reply_item_id=reply_item_id,
        reply_revision_id=reply_revision_id,
    )


class TestClaimExtractionPolicyEnsureCurrent:
    async def test_identity_creates_once_and_reuses(self, conn, edition):
        service = ClaimExtractionPolicyService()
        first = await service.ensure_current(conn, edition_id=edition.id)
        again = await service.ensure_current(conn, edition_id=edition.id)

        assert first.prompt_version == CLAIM_EXTRACTION_PROMPT_VERSION
        assert again.id == first.id
        policies = await POLICY_REPO.list_for_edition(conn, edition.id)
        assert [p.version for p in policies] == [1]

    async def test_new_identity_creates_next_version_latest_wins(self, conn, edition):
        service = ClaimExtractionPolicyService()
        v1 = await service.ensure_current(
            conn, edition_id=edition.id, config_hash="claims-cfg-a", prompt_version="cp1"
        )
        v2 = await service.ensure_current(
            conn, edition_id=edition.id, config_hash="claims-cfg-b", prompt_version="cp1"
        )

        assert (v1.version, v2.version) == (1, 2)

    async def test_editions_table_gains_no_pointer_column(self, conn, edition):
        """RULING: no editions column for claim policy; resolution stays
        identity-based (latest version wins per edition)."""
        service = ClaimExtractionPolicyService()
        await service.ensure_current(conn, edition_id=edition.id)

        cursor = await conn.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'editions' AND column_name LIKE '%claim%'
            """
        )
        assert await cursor.fetchall() == []


@pytest.mark.postgres
class TestClaimExtractionContextBuilder:
    async def test_standalone_revision_has_no_conversation_context(self, uow, revision):
        builder = ClaimExtractionContextBuilder(uow)

        context = await builder.build(revision.id)

        assert context.revision_id == revision.id
        assert context.assertion_text == "На АКЗ возле почты вода уже появилась"
        assert context.parent_text is None
        assert context.root_text is None

    async def test_reply_gets_bounded_parent_and_root_context(self, uow, conn):
        thread = await _insert_reply_thread(
            conn,
            parent_text="На АКЗ воду уже дали?",
            reply_text="Всё ещё нет.",
        )
        builder = ClaimExtractionContextBuilder(uow)

        context = await builder.build(thread.reply_revision_id)

        assert context.assertion_text == "Всё ещё нет."
        assert context.parent_text == "На АКЗ воду уже дали?"
        assert context.root_text == "На АКЗ воду уже дали?"

    async def test_long_context_is_truncated_to_budget(self, uow, conn):
        thread = await _insert_reply_thread(
            conn,
            parent_text="х" * 10_000,
            reply_text="Ответ.",
        )
        builder = ClaimExtractionContextBuilder(uow)

        context = await builder.build(thread.reply_revision_id)

        combined = (context.parent_text or "") + (context.root_text or "")
        assert len(combined) <= CONTEXT_CHAR_BUDGET

    async def test_missing_revision_raises(self, uow):
        builder = ClaimExtractionContextBuilder(uow)

        with pytest.raises(ValueError):
            await builder.build(revision_id=999_999)


AKZ_WATER_TEXT = "На АКЗ возле почты вода уже появилась"


_POLICY_COUNTER = {"n": 0}


async def _insert_vision_policy(
    conn: psycopg.AsyncConnection, edition_id: int, *, mode: str = "relevance_only"
):
    """One fresh vision policy per call (unique version per edition)."""
    _POLICY_COUNTER["n"] += 1
    n = _POLICY_COUNTER["n"]
    return await VisionPolicyRepository().insert(
        conn,
        edition_id=edition_id,
        version=n,
        mode=mode,
        config_hash=f"vcfg-{n}",
        prompt_version="vp1",
    )


async def _seed_extraction_setup(
    conn: psycopg.AsyncConnection, edition_id: int, revision_id: int
) -> tuple[int, int]:
    """Create extraction policy + relevant decision; return (policy_id, decision_id)."""
    policy = await _insert_extraction_policy(conn, edition_id)
    decision_id = await _insert_relevant_decision(conn, edition_id, revision_id)
    return policy.id, decision_id


async def _run_rows(conn: psycopg.AsyncConnection) -> list[tuple]:
    cursor = await conn.execute(
        """
        SELECT id, status, error_kind FROM claim_extraction_runs ORDER BY id
        """
    )
    return [(int(r[0]), r[1], r[2]) for r in await cursor.fetchall()]


async def _attempt_rows(conn: psycopg.AsyncConnection) -> list[tuple]:
    cursor = await conn.execute(
        """
        SELECT stage, semantic_run_id, attempt_no, status, error_kind
        FROM processing_attempts WHERE stage = 'claim_extraction'
        ORDER BY semantic_run_id, attempt_no
        """
    )
    return [tuple(row) for row in await cursor.fetchall()]


@pytest.mark.postgres
class TestClaimExtractionService:
    async def test_akz_water_yields_one_claim_with_place_mention_metadata(
        self, uow, conn, edition, revision
    ):
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        provider = ScriptedClaimProvider(
            _claims_payload(
                _claim_entry(
                    AKZ_WATER_TEXT,
                    "По сообщению местного жителя, на территории АКЗ возле почты появилась вода.",
                    place_mentions=["АКЗ"],
                    entities=["почта"],
                    start="2026-08-22T08:00:00+00:00",
                    precision="hour",
                    confidence=0.7,
                    original_text="уже появилась",
                )
            )
        )
        service = _extraction_service(uow, provider)

        result = await service.extract(revision.id, edition.id, decision_id, policy_id)

        assert result.replayed is False
        assert result.degraded is None
        assert len(result.claims) == 1
        claim = result.claims[0]
        assert claim.source_item_revision_id == revision.id
        assert claim.edition_id == edition.id
        assert claim.normalized_assertion.startswith("По сообщению местного жителя")
        assert claim.event_time_original_text == "уже появилась"
        # T8 ruling: mentions live in claims.metadata until the dedicated table lands.
        assert claim.metadata["place_mentions"] == ["АКЗ"]
        assert claim.metadata["entities"] == ["почта"]
        assert result.run.status == "succeeded"
        attempts = await _attempt_rows(conn)
        assert attempts == [("claim_extraction", result.run.id, 1, "succeeded", None)]

    async def test_two_independent_assertions_become_two_claims(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(
            text_content=(
                "Воду на Мазина дали вечером. Школа №3 открывается 1 сентября. "
                "Ещё сообщают про ремонт на Ленина."
            )
        )
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        provider = ScriptedClaimProvider(
            _claims_payload(
                _claim_entry(
                    "Воду на Мазина дали вечером.",
                    "В Бердянске на улице Мазина вечером восстановили водоснабжение.",
                ),
                _claim_entry(
                    "Школа №3 открывается 1 сентября.",
                    "Школа №3 в Бердянске откроется 1 сентября.",
                ),
            )
        )
        service = _extraction_service(uow, provider)

        result = await service.extract(revision.id, edition.id, decision_id, policy_id)

        # Semantic propositions, not sentence count: three sentences → two claims.
        assert len(result.claims) == 2

    async def test_context_dependent_reply_keeps_provenance_and_self_contained_normalization(
        self, uow, conn, edition
    ):
        thread = await _insert_reply_thread(
            conn,
            parent_text="На АКЗ воду уже дали?",
            reply_text="Всё ещё нет.",
        )
        policy_id, decision_id = await _seed_extraction_setup(
            conn, edition.id, thread.reply_revision_id
        )
        provider = ScriptedClaimProvider(
            _claims_payload(
                _claim_entry(
                    "Всё ещё нет.",
                    "По сообщению автора комментария, водоснабжение на АКЗ на момент "
                    "комментария ещё не восстановилось.",
                    place_mentions=["АКЗ"],
                )
            )
        )
        service = _extraction_service(uow, provider)

        result = await service.extract(thread.reply_revision_id, edition.id, decision_id, policy_id)

        assert len(result.claims) == 1
        claim = result.claims[0]
        # Provenance stays THE reply revision, never the parent.
        assert claim.source_item_revision_id == thread.reply_revision_id
        normalized = claim.normalized_assertion
        # Never a bare fragment: must be a self-contained proposition.
        assert normalized.strip() != "Всё ещё нет"
        assert len(normalized) > len("Всё ещё нет.")
        assert "АКЗ" in normalized or "водоснабжение" in normalized.lower()
        # The model saw the parent/root conversation context to resolve deixis.
        user_payload = provider.calls[0].messages[-1]["content"]
        assert "На АКЗ воду уже дали?" in user_payload
        system_prompt = provider.calls[0].messages[0]["content"]
        assert "stand on its own" in system_prompt
        assert response_format_is_json(provider.calls[0])

    async def test_short_complete_claim_is_valid_without_heuristics(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(text_content="Пожар у вокзала.")
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        provider = ScriptedClaimProvider(
            _claims_payload(_claim_entry("Пожар у вокзала.", "Пожар у вокзала в Бердянске."))
        )
        service = _extraction_service(uow, provider)

        result = await service.extract(revision.id, edition.id, decision_id, policy_id)

        assert len(result.claims) == 1
        assert result.claims[0].normalized_assertion == "Пожар у вокзала в Бердянске."

    async def test_seven_sentence_post_with_two_propositions_yields_two_claims(
        self, uow, conn, edition, revision_factory
    ):
        seven_sentences = (
            "Доброе утро, Бердянск. Погода сегодня солнечная и тёплая. "
            "Кто был на рынке, поделитесь впечатлениями. "
            "Воду на Мазина дали около восьми утра. "
            "Напряжение в сети стабилизировалось после ночи. "
            "Спасибо бригадам за работу. Хорошего дня всем!"
        )
        assert len([s for s in seven_sentences.split(".") if s.strip()]) >= 7
        revision = await revision_factory(text_content=seven_sentences)
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        provider = ScriptedClaimProvider(
            _claims_payload(
                _claim_entry(
                    "Воду на Мазина дали около восьми утра.",
                    "На улице Мазина в Бердянске водоснабжение восстановили около восьми утра.",
                    original_text="около восьми утра",
                ),
                _claim_entry(
                    "Напряжение в сети стабилизировалось после ночи.",
                    "Электроснабжение в Бердянске стабилизировалось после ночных сбоев.",
                ),
            )
        )
        service = _extraction_service(uow, provider)

        result = await service.extract(revision.id, edition.id, decision_id, policy_id)

        # Exactly two factual propositions → two claims; later embedding work
        # makes two claim calls, never seven sentence-chunk calls.
        assert len(result.claims) == 2
        assert len(provider.calls) == 1


def response_format_is_json(call: SimpleNamespace) -> bool:
    return call.response_format == {"type": "json_object"}


@pytest.mark.postgres
class TestClaimExtractionPromptContract:
    async def test_system_prompt_demands_self_contained_semantic_claims(
        self, uow, conn, edition, revision
    ):
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        provider = ScriptedClaimProvider(_claims_payload())
        service = _extraction_service(uow, provider)

        await service.extract(revision.id, edition.id, decision_id, policy_id)

        system_prompt = provider.calls[0].messages[0]["content"]
        assert "stand on its own" in system_prompt
        assert "Всё ещё нет" in system_prompt  # named counter-example forbidden alone
        assert "Пожар у вокзала" in system_prompt  # short complete claims are valid
        assert "semantic" in system_prompt.lower()
        assert "place_mentions" in system_prompt
        assert provider.calls[0].response_format == {"type": "json_object"}


@pytest.mark.postgres
class TestClaimExtractionIdempotenceAndRetries:
    async def test_duplicate_execution_returns_existing_canonical_run(
        self, uow, conn, edition, revision
    ):
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        provider = ScriptedClaimProvider(
            _claims_payload(_claim_entry(AKZ_WATER_TEXT, "Нормализованный текст про АКЗ."))
        )
        service = _extraction_service(uow, provider)

        first = await service.extract(revision.id, edition.id, decision_id, policy_id)
        second = await service.extract(revision.id, edition.id, decision_id, policy_id)

        assert second.replayed is True
        assert second.run.id == first.run.id
        assert [c.id for c in second.claims] == [c.id for c in first.claims]
        assert len(provider.calls) == 1  # no second provider spend
        rows = await _run_rows(conn)
        assert [(r[1]) for r in rows].count("succeeded") == 1

    async def test_preexisting_success_replays_without_provider_call(
        self, uow, conn, edition, revision
    ):
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        run, created = await RUN_REPO.get_or_create_run(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            extraction_policy_id=policy_id,
            relevance_decision_id=decision_id,
        )
        assert created
        await RUN_REPO.mark_succeeded(conn, run.id, completed_at=dt.datetime.now(dt.UTC))
        existing = await CLAIM_REPO.insert_claims(
            conn,
            run=run,
            claims=[
                NewClaim(
                    assertion_text="Ранее.", normalized_assertion="Ранее извлечённое утверждение."
                )
            ],
        )
        provider = ScriptedClaimProvider(_claims_payload())
        service = _extraction_service(uow, provider)

        result = await service.extract(revision.id, edition.id, decision_id, policy_id)

        assert result.replayed is True
        assert [c.id for c in result.claims] == [c.id for c in existing]
        assert provider.calls == []

    async def test_provider_outage_fails_attempt_but_keeps_semantic_run_for_retry(
        self, uow, conn, edition, revision
    ):
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        outage = ProviderCascadeError(
            "All AI provider slots failed: fake (TimeoutError)",
            failure_kinds=("timeout",),
            failure_labels=("fake",),
        )
        good = ScriptedClaimProvider(
            outage,
            _claims_payload(
                _claim_entry(AKZ_WATER_TEXT, "Повторная попытка извлекла утверждение.")
            ),
        )
        service = _extraction_service(uow, good)

        with pytest.raises(ProviderUnavailableError):
            await service.extract(revision.id, edition.id, decision_id, policy_id)

        runs = await _run_rows(conn)
        assert runs[0][1] == "running"  # semantic run stays open for the retry
        attempts = await _attempt_rows(conn)
        assert attempts[0][3] == "unavailable"
        assert attempts[0][4] == "provider_unavailable"

        result = await service.extract(revision.id, edition.id, decision_id, policy_id)

        # Same semantic run across technical retries; attempts are audit history.
        assert result.run.id == runs[0][0]
        assert result.run.status == "succeeded"
        attempts = await _attempt_rows(conn)
        assert [a[2] for a in attempts] == [1, 2]
        assert {a[0] for a in attempts} == {"claim_extraction"}
        assert all(a[1] == result.run.id for a in attempts)

    async def test_structural_garbage_fails_run_and_degrades_operationally(
        self, uow, conn, edition, revision
    ):
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        provider = ScriptedClaimProvider(json.dumps({"claims": "not-a-list"}))
        service = _extraction_service(uow, provider)

        result = await service.extract(revision.id, edition.id, decision_id, policy_id)

        # Deterministic parse failure never blocks the pipeline: item stays
        # relevant-but-claimless and backfill may re-run it later.
        assert result.degraded == "invalid_ai_response"
        assert result.claims == ()
        runs = await _run_rows(conn)
        assert runs[0][1] == "failed"
        assert runs[0][2] == "invalid_ai_response"

    async def test_fail_open_finalization_converges_when_canonical_already_won(
        self, uow, conn, edition, revision
    ):
        """A concurrent winner must never be demoted by the fail-open path:
        finalizing provider unavailability over an already-succeeded run
        converges on its claims without crashing or changing state."""
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        run, created = await RUN_REPO.get_or_create_run(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            extraction_policy_id=policy_id,
            relevance_decision_id=decision_id,
        )
        assert created
        assert await RUN_REPO.mark_succeeded(conn, run.id, completed_at=dt.datetime.now(dt.UTC))
        winner_claims = await CLAIM_REPO.insert_claims(
            conn,
            run=run,
            claims=[
                NewClaim(
                    assertion_text="Победа.",
                    normalized_assertion="Победитель уже записал утверждение.",
                )
            ],
        )
        provider = ScriptedClaimProvider()
        service = _extraction_service(uow, provider)

        result = await service.finalize_provider_failure(
            revision.id, edition.id, decision_id, policy_id
        )

        # Graceful lost-slot handling: no crash, no degraded verdict, and the
        # canonical winner's artifacts are returned untouched.
        assert result.degraded is None
        assert result.replayed is True
        assert [c.id for c in result.claims] == [c.id for c in winner_claims]
        assert result.run.status == "succeeded"
        fetched = await RUN_REPO.get(conn, run.id)
        assert fetched.status == "succeeded"
        assert fetched.error_kind is None


@pytest.mark.postgres
class TestRecordCorrection:
    async def test_correction_creates_new_claim_and_relation(self, uow, conn, edition, revision):
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        provider = ScriptedClaimProvider(
            _claims_payload(
                _claim_entry("Старое утверждение.", "Старое нормализованное утверждение.")
            )
        )
        service = _extraction_service(uow, provider)
        extracted = await service.extract(revision.id, edition.id, decision_id, policy_id)
        old_claim = extracted.claims[0]

        corrected_assertion = "Уточнено: воду дали не на всей улице Мазина."
        new_claim = await service.record_correction(
            old_claim.id, corrected_assertion, relation="CORRECTS"
        )

        assert old_claim.assertion_text != new_claim.assertion_text
        assert new_claim.assertion_text == corrected_assertion
        assert new_claim.normalized_assertion == corrected_assertion
        # Provenance chain preserved through the original run.
        assert new_claim.claim_extraction_run_id == old_claim.claim_extraction_run_id
        assert new_claim.source_item_revision_id == old_claim.source_item_revision_id
        relations = await conn.execute(
            "SELECT from_claim_id, to_claim_id, relation_type FROM claim_relations"
        )
        row = await relations.fetchone()
        assert row == (new_claim.id, old_claim.id, "CORRECTS")
        # The old claim is immutable and untouched.
        fetched = await CLAIM_REPO.get_many(conn, [old_claim.id])
        assert fetched[0].assertion_text == old_claim.assertion_text

    async def test_invalid_relation_type_rejected(self, uow, conn, edition, revision):
        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        provider = ScriptedClaimProvider(
            _claims_payload(_claim_entry(AKZ_WATER_TEXT, "Утверждение про АКЗ и воду."))
        )
        service = _extraction_service(uow, provider)
        extracted = await service.extract(revision.id, edition.id, decision_id, policy_id)

        with pytest.raises(ValueError):
            await service.record_correction(
                extracted.claims[0].id, "Исправление.", relation="MENTIONS"
            )


class StubJobContext:
    """Minimal stand-in for procrastinate's JobContext (only .job.attempts)."""

    def __init__(self, attempts: int) -> None:
        self.job = SimpleNamespace(attempts=attempts)


@pytest.mark.postgres
class TestExtractClaimsTaskGate:
    async def test_task_attributes_transient_only_three_executions(self, jobs_import_env):
        import src.jobs.processing as jobs_processing

        strategy = jobs_processing.CLAIM_RETRY_STRATEGY
        assert strategy.retry_exceptions == (TransientProcessingError,)
        assert strategy.max_attempts == 3
        task = jobs_processing.extract_claims
        assert task.queue == "processing"
        assert task.pass_context is True
        assert task.name == "extract_claims"

    async def test_final_provider_unavailable_marks_run_failed_and_succeeds_operationally(
        self, uow, conn, edition, revision, jobs_import_env, monkeypatch
    ):
        import src.jobs.processing as jobs_processing

        policy_id, decision_id = await _seed_extraction_setup(conn, edition.id, revision.id)
        outage = ProviderCascadeError(
            "All AI provider slots failed: fake (ServerError)",
            failure_kinds=("server",),
            failure_labels=("fake",),
        )
        provider = ScriptedClaimProvider(outage, outage, outage)
        service = _extraction_service(uow, provider)
        monkeypatch.setattr(
            jobs_processing, "build_claim_extraction_service", lambda *args, **kwargs: service
        )

        task = jobs_processing.extract_claims
        with pytest.raises(TransientProcessingError):
            await task(
                StubJobContext(attempts=0),
                source_item_revision_id=revision.id,
                edition_id=edition.id,
                relevance_decision_id=decision_id,
                policy_id=policy_id,
            )
        with pytest.raises(TransientProcessingError):
            await task(
                StubJobContext(attempts=1),
                source_item_revision_id=revision.id,
                edition_id=edition.id,
                relevance_decision_id=decision_id,
                policy_id=policy_id,
            )

        result = await task(
            StubJobContext(attempts=2),
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_decision_id=decision_id,
            policy_id=policy_id,
        )

        # Fail-open: final exhaustion marks the run failed but the task itself
        # succeeds operationally (item stays relevant-but-claimless).
        assert result.degraded == "provider_unavailable"
        assert result.claims == ()
        runs = await _run_rows(conn)
        assert runs[0][1] == "failed"
        assert runs[0][2] == "provider_unavailable"
        attempts = await _attempt_rows(conn)
        assert attempts[-1][3] == "unavailable"
        assert attempts[-1][4] == "provider_unavailable"


# ---------------------------------------------------------------------------
# Handoff wiring: relevance completion / vision completion defer extract_claims
# ---------------------------------------------------------------------------


def _wired_relevance_service(
    uow, payload: dict, vision_mode: str | None, *, claims_enabled: bool = False
) -> RelevanceService:
    return RelevanceService(
        uow=uow,
        provider=ScriptedClaimProvider(json.dumps(payload)),
        model="test-model",
        provider_name="fake",
        vision_mode=vision_mode,
        vision_policy_service=VisionPolicyService(),
        claims_enabled=claims_enabled,
        claim_policy_service=ClaimExtractionPolicyService(),
    )


async def _job_rows(pool, task_name: str) -> list[tuple]:
    async with pool.connection() as observer:
        cursor = await observer.execute(
            f"""
            SELECT args->>'source_item_revision_id', args->>'relevance_decision_id',
                   args->>'policy_id', args->>'vision_run_id'
            FROM procrastinate.procrastinate_jobs
            WHERE task_name = '{task_name}'
            ORDER BY id
            """
        )
        return await cursor.fetchall()


@pytest.mark.postgres
class TestRelevanceToClaimsWiring:
    async def test_relevant_decision_defers_extract_claims_with_exact_policy(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content=AKZ_WATER_TEXT)
        relevance_policy = await RelevancePolicyVersionRepository().insert(
            conn,
            edition_id=edition.id,
            version=1,
            config_hash="cfg-a",
            prompt_version="pv1",
        )
        service = _wired_relevance_service(
            uow,
            {"status": "relevant", "confidence": 0.9, "reason": "локальная новость"},
            "relevance_only",
            claims_enabled=True,
        )

        decision = await service.evaluate(revision.id, edition.id, relevance_policy.id)

        claim_policies = await conn.execute(
            "SELECT id, config_hash, prompt_version FROM claim_extraction_policy_versions"
        )
        policy_row = await claim_policies.fetchone()
        assert policy_row is not None
        rows = await _job_rows(pool, "extract_claims")
        assert len(rows) == 1
        revision_arg, decision_arg, policy_arg, vision_arg = rows[0]
        assert int(revision_arg) == revision.id
        assert int(decision_arg) == decision.id
        assert int(policy_arg) == int(policy_row[0])
        assert vision_arg is None

    async def test_needs_media_defers_no_claims_until_vision_child_relevant(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content=None, with_photo=True)
        relevance_policy = await RelevancePolicyVersionRepository().insert(
            conn,
            edition_id=edition.id,
            version=1,
            config_hash="cfg-nm",
            prompt_version="pv1",
        )
        service = _wired_relevance_service(
            uow,
            {"status": "needs_media", "confidence": None, "reason": "фото"},
            "relevance_only",
            claims_enabled=True,
        )

        await service.evaluate(revision.id, edition.id, relevance_policy.id)

        assert await _job_rows(pool, "extract_claims") == []

    async def test_full_mode_relevant_media_waits_for_vision_before_claims(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content="Прорыв трубы у школы", with_photo=True)
        relevance_policy = await RelevancePolicyVersionRepository().insert(
            conn,
            edition_id=edition.id,
            version=1,
            config_hash="cfg-full",
            prompt_version="pv1",
        )
        service = _wired_relevance_service(
            uow,
            {"status": "relevant", "confidence": 0.9, "reason": "авария"},
            "full",
            claims_enabled=True,
        )

        decision = await service.evaluate(revision.id, edition.id, relevance_policy.id)

        assert decision.status == "relevant"
        assert await _job_rows(pool, "extract_claims") == []  # vision first
        assert await _job_rows(pool, "analyze_vision")

    async def test_irrelevant_defers_nothing(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(
            text_content="Далёкая зарубежная новость без локального угла"
        )
        relevance_policy = await RelevancePolicyVersionRepository().insert(
            conn,
            edition_id=edition.id,
            version=1,
            config_hash="cfg-irr",
            prompt_version="pv1",
        )
        service = _wired_relevance_service(
            uow,
            {"status": "irrelevant", "confidence": 0.8, "reason": "не локально"},
            "relevance_only",
            claims_enabled=True,
        )

        await service.evaluate(revision.id, edition.id, relevance_policy.id)

        assert await _job_rows(pool, "extract_claims") == []


@pytest.mark.postgres
class TestVisionCompletionClaimsHandoff:
    def _wired_vision_service(self, uow, provider, relevance_service) -> VisionService:
        return VisionService(
            uow=uow,
            provider=provider,
            relevance_service=relevance_service,
            claims_enabled=True,
            claim_policy_service=ClaimExtractionPolicyService(),
        )

    async def test_ready_handoff_defers_extract_claims_with_vision_run_id(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content="Что случилось у АКЗ?", with_photo=True)
        vision_policy = await _insert_vision_policy(conn, edition.id)
        relevance_policy = await RelevancePolicyVersionRepository().insert(
            conn,
            edition_id=edition.id,
            version=90,
            config_hash="cfg-vh",
            prompt_version="pv1",
        )
        parent = await EditionRelevanceDecisionRepository().insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=relevance_policy.id,
            status="needs_media",
            confidence=None,
            reason="фото",
        )

        vision_provider = ScriptedVisionAdapter()
        relevance_service = RelevanceService(
            uow=uow,
            provider=AIStubVerdict(
                {"status": "relevant", "confidence": 0.9, "reason": "подтверждено фото"}
            ),
            model="test-model",
            provider_name="fake",
        )
        service = self._wired_vision_service(uow, vision_provider, relevance_service)

        handoff = await service.run(revision.id, parent.id, vision_policy.id)

        assert handoff.ready_for_claims is True
        rows = await _job_rows(pool, "extract_claims")
        assert len(rows) == 1
        revision_arg, decision_arg, _policy_arg, vision_run_arg = rows[0]
        assert int(revision_arg) == revision.id
        assert int(decision_arg) == handoff.relevance_decision_id  # the child
        assert vision_run_arg is not None
        assert int(vision_run_arg) == handoff.vision_run_id

    async def test_not_ready_handoff_defers_nothing(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content="Смотрите фото", with_photo=True)
        vision_policy = await _insert_vision_policy(conn, edition.id)
        relevance_policy = await RelevancePolicyVersionRepository().insert(
            conn,
            edition_id=edition.id,
            version=91,
            config_hash="cfg-vh2",
            prompt_version="pv1",
        )
        parent = await EditionRelevanceDecisionRepository().insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=relevance_policy.id,
            status="needs_media",
            confidence=None,
            reason="фото",
        )

        vision_provider = ScriptedVisionAdapter()
        relevance_service = RelevanceService(
            uow=uow,
            provider=AIStubVerdict({"status": "uncertain", "confidence": None, "reason": "?"}),
            model="test-model",
            provider_name="fake",
        )
        service = self._wired_vision_service(uow, vision_provider, relevance_service)

        handoff = await service.run(revision.id, parent.id, vision_policy.id)

        assert handoff.ready_for_claims is False
        assert await _job_rows(pool, "extract_claims") == []


class ScriptedVisionAdapter:
    """Minimal VisionProvider returning one visible-text observation."""

    async def analyze(self, asset, *, context):
        return VisionOutcome(
            status="succeeded",
            observations=(
                VisionObservationDraft(
                    source_asset_id=None,
                    kind="visible_text",
                    text="Аварийная бригада на месте",
                    metadata={"basis": "pixel_data"},
                ),
            ),
        )


class AIStubVerdict(AIProvider):
    """Always returns one fixed JSON verdict payload."""

    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload)

    async def chat_completion(  # pylint: disable=too-many-positional-arguments
        self,
        messages,
        model,
        temperature=None,
        max_tokens=4096,
        reasoning_effort=None,
        thinking=None,
        response_format=None,
    ) -> str:
        return self._payload


# ---------------------------------------------------------------------------
# Bounded backfill of relevant decisions missing claim extraction
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestBackfillClaims:
    async def test_backfill_queues_only_relevant_decisions_missing_success(
        self, pool, uow, conn, edition, revision_factory, production_jobs_app
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        policy = await _insert_extraction_policy(conn, edition.id)
        other_policy = await _insert_extraction_policy(conn, edition.id, version=2)
        rev_gap = await revision_factory(text_content="Разрыв: воды нет")
        rev_done = await revision_factory(text_content="Уже извлечено")
        rev_failed = await revision_factory(text_content="Провалившийся прогон")
        rev_other = await revision_factory(text_content="Другая политика")
        decision_gap = await _insert_relevant_decision(conn, edition.id, rev_gap.id)
        decision_done = await _insert_relevant_decision(conn, edition.id, rev_done.id)
        decision_failed = await _insert_relevant_decision(conn, edition.id, rev_failed.id)
        decision_other = await _insert_relevant_decision(conn, edition.id, rev_other.id)

        async def _open_run(
            revision_id: int, decision_id: int, policy_id: int
        ) -> ClaimExtractionRun:
            run, _ = await RUN_REPO.get_or_create_run(
                conn,
                source_item_revision_id=revision_id,
                edition_id=edition.id,
                extraction_policy_id=policy_id,
                relevance_decision_id=decision_id,
            )
            return run

        done_run = await _open_run(rev_done.id, decision_done, policy.id)
        await RUN_REPO.mark_succeeded(conn, done_run.id, completed_at=dt.datetime.now(dt.UTC))
        failed_run = await _open_run(rev_failed.id, decision_failed, policy.id)
        await RUN_REPO.mark_failed(
            conn,
            failed_run.id,
            error_kind="provider_unavailable",
            completed_at=dt.datetime.now(dt.UTC),
        )
        other_run = await _open_run(rev_other.id, decision_other, other_policy.id)
        await RUN_REPO.mark_succeeded(conn, other_run.id, completed_at=dt.datetime.now(dt.UTC))

        queued = await jobs_processing.backfill_claims(edition.id, policy.id)

        # Succeeded run (same policy) satisfies the debt; a FAILED run does
        # not (fail-open items must be retried by backfill); another policy's
        # success does not cover THIS policy's debt either.
        assert queued == 3
        rows = await _job_rows(pool, "extract_claims")
        assert {(int(r[0]), int(r[2])) for r in rows} == {
            (rev_gap.id, policy.id),
            (rev_failed.id, policy.id),
            (rev_other.id, policy.id),
        }
        assert all(int(r[1]) in (decision_gap, decision_failed, decision_other) for r in rows)

    async def test_backfill_cursor_is_bounded(
        self, pool, uow, conn, edition, revision_factory, production_jobs_app
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        policy = await _insert_extraction_policy(conn, edition.id)
        revisions = [await revision_factory(text_content=f"item {n}") for n in range(3)]
        decisions = [await _insert_relevant_decision(conn, edition.id, rev.id) for rev in revisions]

        first = await jobs_processing.backfill_claims(edition.id, policy.id)
        again = await jobs_processing.backfill_claims(edition.id, policy.id)

        assert first == 3
        # Safe to re-run: duplicate jobs converge on the canonical success.
        assert again == 3

        cursor_after_first = min(decisions)
        tail = await jobs_processing.backfill_claims(
            edition.id, policy.id, after_decision_id=cursor_after_first + 10**9
        )
        assert tail == 0

    async def test_backfill_full_mode_skips_media_candidates_pending_vision(
        self, pool, uow, conn, edition, revision_factory, production_jobs_app
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        policy = await _insert_extraction_policy(conn, edition.id)
        with_photo = await revision_factory(text_content="Текст и фото", with_photo=True)
        plain = await revision_factory(text_content="Только текст")
        await _insert_relevant_decision(conn, edition.id, with_photo.id)
        await _insert_relevant_decision(conn, edition.id, plain.id)

        queued = await jobs_processing.backfill_claims(edition.id, policy.id, vision_mode="full")

        # Full mode routes media items through vision enrichment first.
        assert queued == 1
        rows = await _job_rows(pool, "extract_claims")
        assert {int(r[0]) for r in rows} == {plain.id}

    async def test_backfill_rejects_foreign_edition_policy(
        self, pool, uow, conn, edition, second_edition, production_jobs_app
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        foreign = await _insert_extraction_policy(conn, second_edition.id)

        with pytest.raises(ValueError):
            await jobs_processing.backfill_claims(edition.id, foreign.id)
