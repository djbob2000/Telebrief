"""Constraint and repository tests for relevance policy persistence (Plan 3 Task 1)
plus AI relevance processing behaviour with conservative fail-open semantics
(Plan 3 Task 2).

Task 1 sections cover spec §12-14 shapes: versioned relevance policies bound
to an edition, immutable edition relevance decisions with root/child
structure, the editions.current_relevance_policy_id pointer with its
edition-consistent composite FK, and the minimal vision policy table.

Task 2 sections cover RelevancePolicyService.ensure_current, the AI-backed
RelevanceService (strict-JSON verdict, no keyword pre-gate, immutable
persistence, duplicate-execution idempotence), the bounded
TransientProcessingError-only retry gate, and exact-policy backfill.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import psycopg
import pytest

from src.ai_providers import AIProvider, ProviderCascadeError
from src.domain.claims import EditionRelevanceDecision, RelevancePolicyVersion, VisionPolicyVersion
from src.processing.relevance import (
    ProviderUnavailableError,
    RelevancePolicyService,
    RelevanceResult,
    RelevanceService,
    TransientProcessingError,
)
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

    async def test_vision_run_decision_edition_mismatch_rejected(
        self, conn, edition, second_edition, revision
    ):
        """Raw-SQL guard: a vision run cannot link a relevance decision that
        belongs to a different edition (composite decision FK)."""
        vision_policy = await VisionPolicyRepository().insert(
            conn,
            edition_id=edition.id,
            version=1,
            mode="relevance_only",
            config_hash="vision-cfg",
            prompt_version="vision-prompt-1",
        )
        foreign_relevance_policy = await RelevancePolicyVersionRepository().insert(
            conn,
            edition_id=second_edition.id,
            version=1,
            config_hash="cfg-abc",
            prompt_version="relevance-prompt-1",
        )
        foreign_decision = await EditionRelevanceDecisionRepository().insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=second_edition.id,
            relevance_policy_id=foreign_relevance_policy.id,
            status="needs_media",
            confidence=None,
            reason="decision owned by another edition",
        )

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(
                """
                INSERT INTO vision_analysis_runs (
                    source_item_revision_id, edition_id, relevance_decision_id,
                    policy_id, status
                )
                VALUES (%s, %s, %s, %s, 'running')
                """,
                # Run belongs to edition A but the decision belongs to
                # edition B: the pair (relevance_decision_id, edition_id)
                # does not exist.
                (revision.id, edition.id, foreign_decision.id, vision_policy.id),
            )


# ---------------------------------------------------------------------------
# Plan 3 Task 2: AI relevance processing (conservative fail-open semantics).
# ---------------------------------------------------------------------------


class ScriptedAIProvider(AIProvider):
    """Deterministic offline provider: replays scripted outcomes in order.

    The final outcome repeats once the script is exhausted so a single
    success/failure can serve arbitrarily many calls. Every call is recorded
    for prompt-shape assertions.
    """

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
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                response_format=response_format,
            )
        )
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, str)
        return outcome


def _verdict(
    *, status: str, confidence: float | None = 0.9, reason: str = "clear local signal"
) -> str:
    return json.dumps({"status": status, "confidence": confidence, "reason": reason})


def _service(uow, provider: AIProvider) -> RelevanceService:
    return RelevanceService(
        uow=uow,
        provider=provider,
        model="test-model",
        provider_name="fake",
    )


async def _decision_count(uow) -> int:
    async with uow.pool.connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM edition_relevance_decisions")
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


class TestRelevanceResultParsing:
    """Strict JSON verdict validation; garbage degrades to uncertain."""

    def test_valid_payload_roundtrips(self):
        result = RelevanceResult.from_dict(
            {"status": "relevant", "confidence": 0.87, "reason": "resident report"}
        )
        assert result == RelevanceResult(
            status="relevant", confidence=0.87, reason="resident report"
        )

    def test_null_confidence_allowed(self):
        result = RelevanceResult.from_dict(
            {"status": "needs_media", "confidence": None, "reason": "photo only"}
        )
        assert result.confidence is None

    def test_unknown_status_becomes_uncertain(self):
        result = RelevanceResult.from_dict({"status": "maybe", "reason": "?"})
        assert result.status == "uncertain"
        assert result.reason == "invalid_ai_response"

    def test_non_numeric_confidence_becomes_uncertain(self):
        result = RelevanceResult.from_dict(
            {"status": "relevant", "confidence": "high", "reason": "x"}
        )
        assert result.status == "uncertain"
        assert result.reason == "invalid_ai_response"

    def test_non_dict_payload_becomes_uncertain(self):
        assert RelevanceResult.from_dict(None).status == "uncertain"
        assert RelevanceResult.from_dict(["relevant"]).status == "uncertain"


@pytest.mark.postgres
class TestEnsureCurrentPolicyResolution:
    async def test_returns_existing_policy_matching_identity(self, conn, edition):
        service = _policy_service()
        first = await service.ensure_current(
            conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
        )
        again = await service.ensure_current(
            conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
        )

        assert again.id == first.id
        policies = await RelevancePolicyVersionRepository().list_for_edition(conn, edition.id)
        assert len(policies) == 1
        current = await RelevancePolicyVersionRepository().get_current(conn, edition.id)
        assert current == first

    async def test_new_identity_creates_version_max_plus_one_and_moves_pointer(self, conn, edition):
        service = _policy_service()
        v1 = await service.ensure_current(
            conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
        )
        v2 = await service.ensure_current(
            conn, edition_id=edition.id, config_hash="cfg-b", prompt_version="pv1"
        )

        assert (v1.version, v2.version) == (1, 2)
        current = await RelevancePolicyVersionRepository().get_current(conn, edition.id)
        assert current == v2

    async def test_identity_match_wins_even_when_newer_policy_is_current(self, conn, edition):
        """ensure_current resolves by identity, not by pointer position."""
        repo = RelevancePolicyVersionRepository()
        service = _policy_service()
        await service.ensure_current(
            conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
        )
        newest = await service.ensure_current(
            conn, edition_id=edition.id, config_hash="cfg-b", prompt_version="pv2"
        )
        assert await repo.get_current(conn, edition.id) == newest

        resolved = await service.ensure_current(
            conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
        )

        assert resolved.version == 1
        assert await repo.get_current(conn, edition.id) == resolved


def _policy_service() -> RelevancePolicyService:
    return RelevancePolicyService()


RESIDENT_REPORT = (
    "Житель Бердянска: вчера вечером восстановили воду на улице Мазина, "
    "давление стабильное, бригада работала до полуночи"
)

UNRELATED_EXTERNAL_STORY = (
    "Токийская биржа закрылась ростом индекса Nikkei на 1,8 процента "
    "на фоне решений центрального банка Японии"
)


@pytest.mark.postgres
class TestRelevanceServiceEvaluate:
    async def test_resident_report_is_relevant(self, uow, edition, revision_factory):
        revision = await revision_factory(text_content=RESIDENT_REPORT)
        provider = ScriptedAIProvider(_verdict(status="relevant", confidence=0.92))
        policy_service = _policy_service()

        async with uow.pool.connection() as conn:
            policy = await policy_service.ensure_current(
                conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
            )

        service = _service(uow, provider)
        decision = await service.evaluate(revision.id, edition.id, policy.id)

        assert isinstance(decision, EditionRelevanceDecision)
        assert decision.status == "relevant"
        assert decision.confidence == pytest.approx(0.92)
        assert decision.parent_decision_id is None
        assert decision.relevance_policy_id == policy.id
        assert decision.provider == "fake"
        assert decision.model == "test-model"

        # Exactly one AI consultation with strict-JSON response format.
        assert len(provider.calls) == 1
        call = provider.calls[0]
        assert call.response_format == {"type": "json_object"}
        system_prompt = call.messages[0]["content"]
        user_payload = call.messages[-1]["content"]
        assert call.messages[0]["role"] == "system"
        assert "Berdyansk" in system_prompt
        assert "context, not proof" in system_prompt
        assert "single resident observation" in system_prompt
        assert "Absence of corroboration is not irrelevance" in system_prompt
        # The AI always receives valid non-empty text — no keyword pre-gate.
        assert RESIDENT_REPORT in user_payload
        assert user_payload.strip() != ""

    async def test_unrelated_external_story_is_irrelevant(self, uow, edition, revision_factory):
        revision = await revision_factory(text_content=UNRELATED_EXTERNAL_STORY)
        provider = ScriptedAIProvider(_verdict(status="irrelevant", confidence=0.81))
        policy_service = _policy_service()

        async with uow.pool.connection() as conn:
            policy = await policy_service.ensure_current(
                conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
            )

        decision = await _service(uow, provider).evaluate(revision.id, edition.id, policy.id)

        assert decision.status == "irrelevant"
        assert decision.confidence == pytest.approx(0.81)

    async def test_media_only_item_needs_media_without_keyword_pregate(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(text_content=None, with_photo=True)
        provider = ScriptedAIProvider(
            _verdict(status="needs_media", confidence=None, reason="decisive photo unseen")
        )
        policy_service = _policy_service()

        async with uow.pool.connection() as db_conn:
            policy = await policy_service.ensure_current(
                db_conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
            )

        decision = await _service(uow, provider).evaluate(revision.id, edition.id, policy.id)

        assert decision.status == "needs_media"
        # The AI was still consulted for an empty-text item: no deterministic
        # keyword rejection happened before the policy saw non-empty text.
        assert len(provider.calls) == 1
        user_payload = provider.calls[0].messages[-1]["content"]
        assert user_payload.strip() != ""
        assert "photo" in user_payload

    async def test_provider_outage_maps_to_unavailable_and_persists_nothing(
        self, uow, edition, revision
    ):
        policy_service = _policy_service()

        async with uow.pool.connection() as conn:
            policy = await policy_service.ensure_current(
                conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
            )

        provider = ScriptedAIProvider(RuntimeError("upstream connect timed out after 30000 ms"))
        service = _service(uow, provider)

        with pytest.raises(ProviderUnavailableError):
            await service.evaluate(revision.id, edition.id, policy.id)

        assert await _decision_count(uow) == 0

    async def test_duplicate_execution_returns_existing_immutable_decision(
        self, uow, conn, edition, revision
    ):
        policy_service = _policy_service()
        decisions = EditionRelevanceDecisionRepository()

        async with conn.transaction():
            policy = await policy_service.ensure_current(
                conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
            )
            existing = await decisions.insert_root(
                conn,
                source_item_revision_id=revision.id,
                edition_id=edition.id,
                relevance_policy_id=policy.id,
                status="irrelevant",
                confidence=None,
                reason="earlier execution won",
            )

        provider = ScriptedAIProvider(_verdict(status="relevant", confidence=0.95))
        decision = await _service(uow, provider).evaluate(revision.id, edition.id, policy.id)

        assert decision.id == existing.id
        assert decision.status == "irrelevant"
        assert await _decision_count(uow) == 1


class StubJobContext:
    """Minimal stand-in for procrastinate's JobContext (only .job.attempts)."""

    def __init__(self, attempts: int) -> None:
        self.job = SimpleNamespace(attempts=attempts)


@pytest.mark.postgres
class TestEvaluateRelevanceTaskRetryGate:
    async def test_retry_strategy_is_transient_only_with_three_total_executions(
        self, jobs_import_env
    ):
        import src.jobs.processing as jobs_processing

        strategy = jobs_processing.RELEVANCE_RETRY_STRATEGY
        assert strategy.retry_exceptions == (TransientProcessingError,)
        # Plan 2 retry math: max_attempts counts TOTAL executions, so 3 means
        # the initial attempt plus exactly two bounded retries.
        assert strategy.max_attempts == 3

        task = jobs_processing.evaluate_relevance
        assert task.queue == "processing"
        assert task.pass_context is True
        assert task.name == "evaluate_relevance"

    async def test_timeout_retries_twice_then_persists_uncertain_and_succeeds(
        self, uow, edition, revision, jobs_import_env, monkeypatch
    ):
        import src.jobs.processing as jobs_processing

        policy_service = _policy_service()

        async with uow.pool.connection() as conn:
            policy = await policy_service.ensure_current(
                conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
            )

        outage = ProviderCascadeError(
            "All AI provider slots failed: fake (TimeoutError)",
            failure_kinds=("timeout",),
            failure_labels=("fake",),
        )
        provider = ScriptedAIProvider(outage, outage, outage)
        service = _service(uow, provider)
        monkeypatch.setattr(jobs_processing, "build_relevance_service", lambda: service)

        task = jobs_processing.evaluate_relevance

        with pytest.raises(TransientProcessingError):
            await task(StubJobContext(attempts=0), revision.id, edition.id, policy.id)
        assert await _decision_count(uow) == 0

        with pytest.raises(TransientProcessingError):
            await task(StubJobContext(attempts=1), revision.id, edition.id, policy.id)
        assert await _decision_count(uow) == 0

        result = await task(StubJobContext(attempts=2), revision.id, edition.id, policy.id)

        # Final failed attempt persists uncertain and succeeds operationally.
        assert result is not None
        assert result.status == "uncertain"
        assert result.confidence is None
        assert result.reason == "provider_unavailable"
        assert await _decision_count(uow) == 1

    async def test_retried_job_keeps_originally_queued_policy_id(
        self, uow, edition, revision, jobs_import_env, monkeypatch
    ):
        import src.jobs.processing as jobs_processing

        policy_service = _policy_service()

        async with uow.pool.connection() as conn:
            original_policy = await policy_service.ensure_current(
                conn, edition_id=edition.id, config_hash="cfg-v1", prompt_version="pv1"
            )

        outage = ProviderCascadeError(
            "All AI provider slots failed: fake (ServerError)",
            failure_kinds=("server",),
            failure_labels=("fake",),
        )
        provider = ScriptedAIProvider(outage, _verdict(status="relevant", confidence=0.9))
        service = _service(uow, provider)
        monkeypatch.setattr(jobs_processing, "build_relevance_service", lambda: service)

        task = jobs_processing.evaluate_relevance
        with pytest.raises(TransientProcessingError):
            await task(StubJobContext(attempts=0), revision.id, edition.id, original_policy.id)

        # A newer policy becomes current before the retry fires.
        async with uow.pool.connection() as conn:
            newer_policy = await policy_service.ensure_current(
                conn, edition_id=edition.id, config_hash="cfg-v2", prompt_version="pv2"
            )
        assert newer_policy.id != original_policy.id

        decision = await task(
            StubJobContext(attempts=2), revision.id, edition.id, original_policy.id
        )

        assert decision is not None
        assert decision.relevance_policy_id == original_policy.id


@pytest.mark.postgres
class TestBackfillRelevanceGapFilling:
    async def _seed_gap_fixture(self, conn, edition, revision_factory, policy_service):
        revisions = [await revision_factory(text_content=f"item {n}") for n in range(3)]
        for revision in revisions:
            await conn.execute(
                "INSERT INTO source_editions (source_id, edition_id) VALUES (%s, %s)",
                (revision.source_id, edition.id),
            )
        policy_a = await policy_service.ensure_current(
            conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
        )
        policy_b = await policy_service.ensure_current(
            conn, edition_id=edition.id, config_hash="cfg-b", prompt_version="pv1"
        )
        decisions = EditionRelevanceDecisionRepository()
        await decisions.insert_root(
            conn,
            source_item_revision_id=revisions[0].id,
            edition_id=edition.id,
            relevance_policy_id=policy_a.id,
            status="relevant",
            confidence=0.7,
            reason="already decided under policy A",
        )
        return revisions, policy_a, policy_b

    async def test_repo_gap_query_targets_exact_policy(self, conn, edition, revision_factory):
        policy_service = _policy_service()
        revisions, policy_a, policy_b = await self._seed_gap_fixture(
            conn, edition, revision_factory, policy_service
        )
        decisions = EditionRelevanceDecisionRepository()

        gaps_for_a = await decisions.list_revision_ids_missing_root(
            conn, edition_id=edition.id, relevance_policy_id=policy_a.id
        )
        gaps_for_b = await decisions.list_revision_ids_missing_root(
            conn, edition_id=edition.id, relevance_policy_id=policy_b.id
        )

        assert gaps_for_a == [revisions[1].id, revisions[2].id]
        assert gaps_for_b == [revision.id for revision in revisions]

    async def test_backfill_queues_only_missing_revisions_for_exact_policy(
        self,
        pool,
        uow,
        conn,
        edition,
        revision_factory,
        production_jobs_app,
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )

        policy_service = _policy_service()
        revisions, policy_a, policy_b = await self._seed_gap_fixture(
            conn, edition, revision_factory, policy_service
        )

        queued_b = await jobs_processing.backfill_relevance(edition.id, policy_b.id)
        queued_a = await jobs_processing.backfill_relevance(edition.id, policy_a.id)
        # Bounded cursor: everything after the second revision only.
        queued_cursor = await jobs_processing.backfill_relevance(
            edition.id, policy_a.id, after_revision_id=revisions[1].id
        )

        assert queued_b == 3
        assert queued_a == 2
        assert queued_cursor == 1

        async with pool.connection() as observer:
            cursor = await observer.execute(
                """
                SELECT args->>'source_item_revision_id', args->>'policy_id'
                FROM procrastinate.procrastinate_jobs
                WHERE task_name = 'evaluate_relevance'
                ORDER BY id
                """
            )
            rows = await cursor.fetchall()
        assert len(rows) == 6
        assert {(int(row[0]), int(row[1])) for row in rows} == {
            (revision.id, policy_b.id) for revision in revisions
        } | {(revisions[1].id, policy_a.id), (revisions[2].id, policy_a.id)}

    async def test_backfill_rejects_foreign_edition_policy(
        self, pool, uow, conn, edition, second_edition, production_jobs_app
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )

        policy = await RelevancePolicyVersionRepository().insert(
            conn,
            edition_id=second_edition.id,
            version=1,
            config_hash="cfg-x",
            prompt_version="pv1",
        )

        with pytest.raises(ValueError):
            await jobs_processing.backfill_relevance(edition.id, policy.id)
