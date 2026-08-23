"""Bounded Vision analysis for media-dependent relevance (Plan 3 Task 3).

Covers the pure scheduling policy (``should_run_vision``), the vision policy
get-or-create service, immutable run/observation persistence, the
``VisionService`` fail-open completion boundary (``finish_vision_processing``
handoffs), the ``analyze_vision`` task retry gate, relevance-completion
wiring that defers vision atomically with the decision, and bounded backfill.

Fail-open invariants exercised throughout: ``off`` never resolves an item,
vision failure never fabricates a negative verdict, and a text-supported
relevant decision still reaches claims (``ready_for_claims=True``) even when
Vision is unavailable.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import psycopg
import pytest

from src.ai_providers import AIProvider, ProviderCascadeError
from src.domain.claims import EditionRelevanceDecision, VisionAnalysisRun
from src.processing.relevance import (
    RelevancePolicyService,
    RelevanceService,
    TransientProcessingError,
)
from src.processing.vision import (
    DEFAULT_VISION_MODE,
    VISION_PROMPT_VERSION,
    AssetDescriptor,
    MetadataVisionProvider,
    VisionObservationDraft,
    VisionOutcome,
    VisionPolicyService,
    VisionProviderUnavailable,
    VisionService,
    should_run_vision,
    vision_config_hash,
)
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    RelevancePolicyVersionRepository,
    VisionAnalysisRunRepository,
    VisionPolicyRepository,
)

# ---------------------------------------------------------------------------
# Pure policy function (brief Step 1 cases)
# ---------------------------------------------------------------------------


def _decision(status: str, *, decision_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(id=decision_id, status=status, source_item_revision_id=11, edition_id=1)


class TestShouldRunVisionPolicy:
    STRONG_TEXT = "Житель рассказал подробно: воду отключили вечером и вернули к полуночи"

    def test_relevance_only_strong_text_does_not_spend_vision(self):
        assert not should_run_vision(
            _decision("relevant"),
            SimpleNamespace(text_content=self.STRONG_TEXT),
            [{"kind": "photo"}],
            mode="relevance_only",
        )

    def test_relevance_only_needs_media_spends_vision(self):
        assert should_run_vision(
            _decision("needs_media"),
            SimpleNamespace(text_content=None),
            [{"kind": "photo"}],
            mode="relevance_only",
        )

    def test_off_mode_never_runs(self):
        for status in ("needs_media", "relevant", "uncertain"):
            assert not should_run_vision(
                _decision(status),
                SimpleNamespace(text_content=None),
                [{"kind": "photo"}],
                mode="off",
            )

    def test_full_relevant_with_media_waits_for_enrichment(self):
        assert should_run_vision(
            _decision("relevant"),
            SimpleNamespace(text_content="Текст и фото"),
            [{"kind": "photo"}],
            mode="full",
        )

    def test_full_relevant_without_media_skips(self):
        assert not should_run_vision(
            _decision("relevant"), SimpleNamespace(text_content="Только текст"), [], mode="full"
        )

    def test_full_irrelevant_never_spends(self):
        assert not should_run_vision(
            _decision("irrelevant"),
            SimpleNamespace(text_content=None),
            [{"kind": "photo"}],
            mode="full",
        )

    def test_media_gate_without_assets_keeps_item_unresolved(self):
        assert not should_run_vision(
            _decision("needs_media"), SimpleNamespace(text_content=None), [], mode="relevance_only"
        )


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class ScriptedVisionProvider:
    """Fake VisionProvider: scripted per-asset outcomes, calls recorded."""

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[AssetDescriptor] = []

    async def analyze(self, asset: AssetDescriptor, *, context: dict) -> VisionOutcome:
        self.calls.append(asset)
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, VisionOutcome)
        return outcome


class AIStubVerdict(AIProvider):
    """Always returns one fixed JSON verdict payload."""

    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload)
        self.calls: list[SimpleNamespace] = []

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
        self.calls.append(SimpleNamespace(messages=messages, model=model))
        return self._payload


def _text_observation(asset_id: int | None = None) -> VisionOutcome:
    # asset_id defaults to None: vision_observations.source_asset_id is
    # FK-guarded, so scripted drafts must not invent asset rows.
    return VisionOutcome(
        status="succeeded",
        observations=(
            VisionObservationDraft(
                source_asset_id=asset_id,
                kind="visible_text",
                text="Аварийная бригада устраняет порыв на Мазина",
                metadata={"basis": "pixel_data"},
            ),
        ),
    )


NO_PIXELS = VisionOutcome(status="unavailable", observations=(), error_kind="no_pixel_data")

_POLICY_COUNTER = {"n": 0}


async def _insert_parent(
    conn: psycopg.AsyncConnection,
    edition_id: int,
    revision_id: int,
    *,
    status: str = "needs_media",
) -> EditionRelevanceDecision:
    """One fresh relevance policy + immutable ROOT decision per call."""
    _POLICY_COUNTER["n"] += 1
    n = _POLICY_COUNTER["n"]
    policy = await RelevancePolicyVersionRepository().insert(
        conn,
        edition_id=edition_id,
        version=n,
        config_hash=f"cfg-{n}",
        prompt_version="pv1",
    )
    return await EditionRelevanceDecisionRepository().insert_root(
        conn,
        source_item_revision_id=revision_id,
        edition_id=edition_id,
        relevance_policy_id=policy.id,
        status=status,
        confidence=None if status == "needs_media" else 0.9,
        reason="seeded parent",
    )


async def _vision_policy(
    conn: psycopg.AsyncConnection, edition_id: int, *, mode: str = "relevance_only"
) -> object:
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


def _asset_descriptor(asset_id: int | None = None, **overrides) -> AssetDescriptor:
    values = {
        "asset_id": asset_id,
        "kind": "photo",
        "mime_type": "image/jpeg",
        "external_url": "https://example.test/photo.jpg",
        "width": 1080,
        "height": 810,
        "duration": None,
    }
    values.update(overrides)
    return AssetDescriptor(**values)


def _vision_service(uow, provider, relevance_service=None, **overrides) -> VisionService:
    values = {"uow": uow, "provider": provider, "relevance_service": relevance_service}
    values.update(overrides)
    return VisionService(**values)


def _relevance_service(uow, provider: AIProvider) -> RelevanceService:
    return RelevanceService(uow=uow, provider=provider, model="test-model", provider_name="fake")


async def _run_count(conn: psycopg.AsyncConnection) -> int:
    cursor = await conn.execute("SELECT COUNT(*) FROM vision_analysis_runs")
    row = await cursor.fetchone()
    return int(row[0])


async def _latest_run_row(conn: psycopg.AsyncConnection) -> tuple:
    """(id, status, error_kind, metadata) of the newest vision run."""
    cursor = await conn.execute(
        """
        SELECT id, status, error_kind, metadata FROM vision_analysis_runs
        ORDER BY id DESC LIMIT 1
        """
    )
    row = await cursor.fetchone()
    assert row is not None, "no vision run persisted"
    return (int(row[0]), row[1], row[2], row[3])


# ---------------------------------------------------------------------------
# Policy service and repositories
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestVisionPolicyServiceEnsureCurrent:
    async def test_default_mode_creates_and_reuses_identity(self, conn, edition):
        service = VisionPolicyService()
        first = await service.ensure_current(conn, edition_id=edition.id)
        again = await service.ensure_current(conn, edition_id=edition.id)

        assert first.mode == DEFAULT_VISION_MODE == "relevance_only"
        assert first.prompt_version == VISION_PROMPT_VERSION
        assert first.config_hash == vision_config_hash(mode=DEFAULT_VISION_MODE)
        assert again.id == first.id
        policies = await VisionPolicyRepository().list_for_edition(conn, edition.id)
        assert [p.version for p in policies] == [1]

    async def test_mode_change_creates_next_version(self, conn, edition):
        service = VisionPolicyService()
        v1 = await service.ensure_current(conn, edition_id=edition.id, mode="relevance_only")
        v2 = await service.ensure_current(conn, edition_id=edition.id, mode="full")

        assert (v1.version, v2.version) == (1, 2)
        assert v2.config_hash != v1.config_hash

    async def test_invalid_mode_rejected_before_sql(self, conn, edition):
        service = VisionPolicyService()

        with pytest.raises(ValueError, match="mode"):
            await service.ensure_current(conn, edition_id=edition.id, mode="sometimes")


@pytest.mark.postgres
class TestVisionRunPersistence:
    async def test_insert_running_and_complete_with_observations(self, conn, edition, revision):
        vision_policy = await _vision_policy(conn, edition.id)
        decision = await _insert_parent(conn, edition.id, revision.id)
        repo = VisionAnalysisRunRepository()
        run = await repo.insert(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_decision_id=decision.id,
            policy_id=vision_policy.id,
            metadata={"mode": "relevance_only"},
        )

        assert isinstance(run, VisionAnalysisRun)
        assert run.status == "running"
        assert run.completed_at is None

        completed = await repo.complete(
            conn,
            run,
            observations=(
                VisionObservationDraft(
                    source_asset_id=None, kind="scene", text="улица", metadata={}
                ),
            ),
        )

        assert completed.status == "succeeded"
        assert completed.error_kind is None
        assert completed.completed_at is not None
        assert completed.metadata["observation_count"] == 1
        observations = await repo.list_observations(conn, run.id)
        assert len(observations) == 1
        assert observations[0].kind == "scene"
        assert observations[0].text == "улица"
        assert observations[0].source_item_revision_id == revision.id

    async def test_complete_unavailable_records_error_kind(self, conn, edition, revision):
        vision_policy = await _vision_policy(conn, edition.id, mode="full")
        decision = await _insert_parent(conn, edition.id, revision.id, status="relevant")
        repo = VisionAnalysisRunRepository()
        run = await repo.insert(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_decision_id=decision.id,
            policy_id=vision_policy.id,
        )
        completed = await repo.complete(
            conn, run, observations=(), error="no_pixel_data", additional_metadata={"partial": []}
        )

        assert completed.status == "unavailable"
        assert completed.error_kind == "no_pixel_data"
        assert completed.metadata["partial"] == []

    async def test_latest_for_decision_policy_orders_desc(self, conn, edition, revision):
        vision_policy = await _vision_policy(conn, edition.id)
        decision = await _insert_parent(conn, edition.id, revision.id)
        repo = VisionAnalysisRunRepository()
        first = await repo.insert(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_decision_id=decision.id,
            policy_id=vision_policy.id,
        )
        second = await repo.insert(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_decision_id=decision.id,
            policy_id=vision_policy.id,
        )

        latest = await repo.latest_for_decision_policy(
            conn, relevance_decision_id=decision.id, policy_id=vision_policy.id
        )

        assert latest.id == second.id != first.id

    async def test_gap_query_finds_decisions_missing_run_for_policy(
        self, conn, edition, revision_factory
    ):
        vision_policy = await _vision_policy(conn, edition.id)
        other_vision_policy = await _vision_policy(conn, edition.id)
        rev_a = await revision_factory(with_photo=True)
        rev_b = await revision_factory(with_photo=True)
        needs_a = await _insert_parent(conn, edition.id, rev_a.id)
        relevant_b = await _insert_parent(conn, edition.id, rev_b.id, status="relevant")
        repo = VisionAnalysisRunRepository()
        await repo.insert(
            conn,
            source_item_revision_id=rev_b.id,
            edition_id=edition.id,
            relevance_decision_id=relevant_b.id,
            policy_id=other_vision_policy.id,
        )

        gaps = await repo.list_decisions_missing_run(
            conn,
            edition_id=edition.id,
            policy_id=vision_policy.id,
            statuses=["needs_media", "relevant"],
        )

        assert sorted(d.id for d in gaps) == sorted([needs_a.id, relevant_b.id])

        cursor_id = min(needs_a.id, relevant_b.id)
        after_first = await repo.list_decisions_missing_run(
            conn,
            edition_id=edition.id,
            policy_id=vision_policy.id,
            statuses=["needs_media", "relevant"],
            after_decision_id=cursor_id,
        )
        assert all(d.id > cursor_id for d in after_first)


# ---------------------------------------------------------------------------
# VisionService behaviour
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestVisionServiceNeedsMediaChildDecision:
    async def test_observations_produce_immutable_child_and_ready_handoff(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(text_content="Что случилось у АКЗ?", with_photo=True)
        parent = await _insert_parent(conn, edition.id, revision.id)
        vision_policy = await _vision_policy(conn, edition.id)

        provider = ScriptedVisionProvider(_text_observation())
        relevance_stub = AIStubVerdict(
            {"status": "relevant", "confidence": 0.88, "reason": "фото подтверждает аварию"}
        )
        service = _vision_service(uow, provider, _relevance_service(uow, relevance_stub))

        handoff = await service.run(revision.id, parent.id, vision_policy.id)

        assert handoff.relevance_decision_id != parent.id
        assert handoff.vision_run_id is not None
        assert handoff.ready_for_claims is True

        child_repo = EditionRelevanceDecisionRepository()
        child = await child_repo.get(conn, handoff.relevance_decision_id)
        assert child.status == "relevant"
        assert child.parent_decision_id == parent.id
        untouched = await child_repo.get(conn, parent.id)
        assert untouched.id == parent.id
        assert untouched.status == "needs_media"

        observations_cursor = await conn.execute(
            "SELECT kind, text FROM vision_observations WHERE vision_run_id = %s",
            (handoff.vision_run_id,),
        )
        rows = await observations_cursor.fetchall()
        assert rows and rows[0][0] == "visible_text"
        # The follow-up verdict consumed the source text plus observations.
        user_payload = relevance_stub.calls[0].messages[-1]["content"]
        assert "Что случилось у АКЗ?" in user_payload
        assert "порыв" in user_payload

    async def test_child_verdict_uncertain_keeps_claims_closed(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(text_content="Смотрите фото", with_photo=True)
        parent = await _insert_parent(conn, edition.id, revision.id)
        vision_policy = await _vision_policy(conn, edition.id)

        provider = ScriptedVisionProvider(_text_observation())
        relevance_stub = AIStubVerdict(
            {"status": "uncertain", "confidence": None, "reason": "неясно"}
        )
        service = _vision_service(uow, provider, _relevance_service(uow, relevance_stub))

        handoff = await service.run(revision.id, parent.id, vision_policy.id)

        child = await EditionRelevanceDecisionRepository().get(conn, handoff.relevance_decision_id)
        assert child.status == "uncertain"
        assert child.parent_decision_id == parent.id
        assert handoff.ready_for_claims is False

    async def test_no_observations_leaves_needs_media_without_child(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(text_content="Смотрите фото", with_photo=True)
        parent = await _insert_parent(conn, edition.id, revision.id)
        vision_policy = await _vision_policy(conn, edition.id)

        provider = ScriptedVisionProvider(VisionOutcome(status="succeeded", observations=()))
        service = _vision_service(uow, provider, None)

        handoff = await service.run(revision.id, parent.id, vision_policy.id)

        assert handoff.relevance_decision_id == parent.id
        assert handoff.vision_run_id is None
        assert handoff.ready_for_claims is False
        children = await conn.execute(
            "SELECT COUNT(*) FROM edition_relevance_decisions WHERE parent_decision_id = %s",
            (parent.id,),
        )
        assert int((await children.fetchone())[0]) == 0


@pytest.mark.postgres
class TestVisionServiceLimitsAndFailure:
    async def test_asset_limit_stops_with_explicit_partial_outcome(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(text_content="Смотрите фото")
        asset_ids = []
        for n in range(6):
            cursor = await conn.execute(
                """
                INSERT INTO source_assets (
                    source_item_revision_id, kind, mime_type, external_url, metadata
                )
                VALUES (%s, 'photo', 'image/jpeg', %s, '{}'::jsonb)
                RETURNING id
                """,
                (revision.id, f"https://example.test/{n}.jpg"),
            )
            asset_ids.append(int((await cursor.fetchone())[0]))
        parent = await _insert_parent(conn, edition.id, revision.id)
        vision_policy = await _vision_policy(conn, edition.id)

        provider = ScriptedVisionProvider(_text_observation())
        relevance_stub = AIStubVerdict(
            {"status": "relevant", "confidence": 0.9, "reason": "подтверждено"}
        )
        service = _vision_service(
            uow,
            provider,
            _relevance_service(uow, relevance_stub),
            max_assets_per_run=2,
        )

        handoff = await service.run(revision.id, parent.id, vision_policy.id)

        assert len(provider.calls) == 2
        cursor = await conn.execute(
            "SELECT status, metadata FROM vision_analysis_runs WHERE id = %s",
            (handoff.vision_run_id,),
        )
        status, metadata = await cursor.fetchone()
        assert status == "succeeded"
        partial = metadata["partial"]
        assert partial["reason"] == "asset_limit_exceeded"
        assert set(partial["asset_ids"]) == set(asset_ids[2:])

    async def test_size_descriptor_limit_marks_partial_and_unavailable(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(with_photo=True)
        await conn.execute(
            """
            UPDATE source_assets SET width = 20000, height = 10000
            WHERE source_item_revision_id = %s
            """,
            (revision.id,),
        )
        parent = await _insert_parent(conn, edition.id, revision.id)
        vision_policy = await _vision_policy(conn, edition.id)
        provider = ScriptedVisionProvider(_text_observation())
        service = _vision_service(uow, provider, None, max_size_pixels=8000)

        handoff = await service.run(revision.id, parent.id, vision_policy.id)

        assert provider.calls == []
        run_id, status, error_kind, metadata = await _latest_run_row(conn)
        assert status == "unavailable"
        assert error_kind == "no_eligible_assets"
        assert metadata["partial"]["reason"] == "size_descriptor_exceeded"
        assert handoff.ready_for_claims is False

    async def test_provider_exception_maps_to_transient_marker(self, uow):
        provider = ScriptedVisionProvider(RuntimeError("upstream connect timed out"))
        service = _vision_service(uow, provider, None)

        with pytest.raises(VisionProviderUnavailable):
            await service._analyze_assets([_asset_descriptor()])

    async def test_final_failure_leaves_needs_media_unresolved_without_child(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(text_content="Смотрите фото", with_photo=True)
        parent = await _insert_parent(conn, edition.id, revision.id)
        vision_policy = await _vision_policy(conn, edition.id)
        service = _vision_service(uow, ScriptedVisionProvider(), None)

        handoff = await service.finalize_provider_failure(revision.id, parent.id, vision_policy.id)

        assert handoff.relevance_decision_id == parent.id
        assert handoff.ready_for_claims is False
        _, status, error_kind, _ = await _latest_run_row(conn)
        assert status == "unavailable"
        assert error_kind == "provider_unavailable"
        children = await conn.execute(
            "SELECT COUNT(*) FROM edition_relevance_decisions WHERE parent_decision_id = %s",
            (parent.id,),
        )
        assert int((await children.fetchone())[0]) == 0
        still_parent = await EditionRelevanceDecisionRepository().get(conn, parent.id)
        assert still_parent.status == "needs_media"

    async def test_full_mode_timeout_still_opens_claims_for_text_supported_relevant(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(text_content="Вода появилась на Мазина", with_photo=True)
        parent = await _insert_parent(conn, edition.id, revision.id, status="relevant")
        vision_policy = await _vision_policy(conn, edition.id, mode="full")
        service = _vision_service(uow, ScriptedVisionProvider(), None)

        handoff = await service.finalize_provider_failure(revision.id, parent.id, vision_policy.id)

        assert handoff.ready_for_claims is True
        assert handoff.relevance_decision_id == parent.id

    async def test_no_pixel_data_outcome_completes_unavailable(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(text_content="Смотрите фото", with_photo=True)
        parent = await _insert_parent(conn, edition.id, revision.id)
        vision_policy = await _vision_policy(conn, edition.id)
        provider = ScriptedVisionProvider(NO_PIXELS)
        service = _vision_service(uow, provider, None)

        handoff = await service.run(revision.id, parent.id, vision_policy.id)

        _, status, error_kind, _ = await _latest_run_row(conn)
        assert status == "unavailable"
        assert error_kind == "no_pixel_data"
        assert handoff.ready_for_claims is False

    async def test_succeeded_run_guard_prevents_duplicate_work(
        self, uow, conn, edition, revision_factory
    ):
        revision = await revision_factory(text_content="Смотрите фото", with_photo=True)
        parent = await _insert_parent(conn, edition.id, revision.id)
        vision_policy = await _vision_policy(conn, edition.id)
        provider = ScriptedVisionProvider(_text_observation())
        relevance_stub = AIStubVerdict(
            {"status": "relevant", "confidence": 0.9, "reason": "подтверждено"}
        )
        service = _vision_service(uow, provider, _relevance_service(uow, relevance_stub))

        first = await service.run(revision.id, parent.id, vision_policy.id)
        provider_calls_after_first = len(provider.calls)
        replay = await service.run(revision.id, parent.id, vision_policy.id)

        assert replay.relevance_decision_id == first.relevance_decision_id
        assert replay.vision_run_id == first.vision_run_id
        assert len(provider.calls) == provider_calls_after_first
        assert await _run_count(conn) == 1


# ---------------------------------------------------------------------------
# analyze_vision task and backfill
# ---------------------------------------------------------------------------


class StubJobContext:
    def __init__(self, attempts: int) -> None:
        self.job = SimpleNamespace(attempts=attempts)


@pytest.mark.postgres
class TestAnalyzeVisionTask:
    async def test_task_attributes_transient_only_three_executions(self, jobs_import_env):
        import src.jobs.processing as jobs_processing

        strategy = jobs_processing.VISION_RETRY_STRATEGY
        assert strategy.retry_exceptions == (TransientProcessingError,)
        assert strategy.max_attempts == 3
        task = jobs_processing.analyze_vision
        assert task.queue == "processing"
        assert task.pass_context is True
        assert task.name == "analyze_vision"

    async def test_happy_path_creates_child_and_run(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app, monkeypatch
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content="Смотрите фото", with_photo=True)
        parent = await _insert_parent(conn, edition.id, revision.id)
        vision_policy = await _vision_policy(conn, edition.id)

        provider = ScriptedVisionProvider(_text_observation())
        relevance_stub = AIStubVerdict(
            {"status": "relevant", "confidence": 0.9, "reason": "подтверждено"}
        )
        monkeypatch.setattr(
            jobs_processing,
            "build_vision_service",
            lambda: _vision_service(uow, provider, _relevance_service(uow, relevance_stub)),
        )

        handoff = await jobs_processing.analyze_vision(
            StubJobContext(attempts=0), revision.id, parent.id, vision_policy.id
        )

        assert handoff.ready_for_claims is True
        assert await _run_count(conn) == 1

    async def test_transient_retries_then_finalizes_unavailable(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app, monkeypatch
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content="Вода на Мазина", with_photo=True)
        parent = await _insert_parent(conn, edition.id, revision.id, status="relevant")
        vision_policy = await _vision_policy(conn, edition.id)

        outage = ProviderCascadeError(
            "All AI provider slots failed: fake (TimeoutError)",
            failure_kinds=("timeout",),
            failure_labels=("fake",),
        )
        provider = ScriptedVisionProvider(outage, outage, outage)
        service = _vision_service(uow, provider, None)
        monkeypatch.setattr(jobs_processing, "build_vision_service", lambda: service)

        with pytest.raises(TransientProcessingError):
            await jobs_processing.analyze_vision(
                StubJobContext(attempts=0), revision.id, parent.id, vision_policy.id
            )
        with pytest.raises(TransientProcessingError):
            await jobs_processing.analyze_vision(
                StubJobContext(attempts=1), revision.id, parent.id, vision_policy.id
            )
        result = await jobs_processing.analyze_vision(
            StubJobContext(attempts=2), revision.id, parent.id, vision_policy.id
        )

        assert result.ready_for_claims is True
        assert result.relevance_decision_id == parent.id
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM vision_analysis_runs WHERE status = 'unavailable'"
        )
        assert int((await cursor.fetchone())[0]) >= 1


@pytest.mark.postgres
class TestBackfillVision:
    async def test_queues_missing_needs_media_only(
        self, pool, uow, conn, edition, revision_factory, production_jobs_app
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        vision_policy = await _vision_policy(conn, edition.id)
        with_photo = await revision_factory(text_content=None, with_photo=True)
        text_only = await revision_factory(text_content="Обычный текст без медиа")
        await _insert_parent(conn, edition.id, with_photo.id)
        await _insert_parent(conn, edition.id, text_only.id)

        queued = await jobs_processing.backfill_vision(edition.id, vision_policy.id)

        assert queued == 1
        async with pool.connection() as observer:
            cursor = await observer.execute(
                """
                SELECT args->>'policy_id'
                FROM procrastinate.procrastinate_jobs
                WHERE task_name = 'analyze_vision'
                """
            )
            rows = await cursor.fetchall()
        assert {int(row[0]) for row in rows} == {vision_policy.id}

    async def test_full_mode_backfills_relevant_with_media_too(
        self, pool, uow, conn, edition, revision_factory, production_jobs_app
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        full_policy = await _vision_policy(conn, edition.id, mode="full")
        relevance_policy = await RelevancePolicyVersionRepository().insert(
            conn,
            edition_id=edition.id,
            version=98,
            config_hash="cfg-full-bf",
            prompt_version="pv1",
        )
        relevant_photo = await revision_factory(text_content="Текст и фото", with_photo=True)
        relevant_plain = await revision_factory(text_content="Текст без медиа")
        decisions = EditionRelevanceDecisionRepository()
        for rev in (relevant_photo, relevant_plain):
            await decisions.insert_root(
                conn,
                source_item_revision_id=rev.id,
                edition_id=edition.id,
                relevance_policy_id=relevance_policy.id,
                status="relevant",
                confidence=0.9,
                reason="seeded",
            )

        queued = await jobs_processing.backfill_vision(edition.id, full_policy.id)

        assert queued == 1

    async def test_foreign_edition_policy_rejected(
        self, pool, uow, conn, edition, second_edition, production_jobs_app
    ):
        import src.jobs.processing as jobs_processing
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        foreign = await _vision_policy(conn, second_edition.id)

        with pytest.raises(ValueError):
            await jobs_processing.backfill_vision(edition.id, foreign.id)


# ---------------------------------------------------------------------------
# Relevance-completion wiring: atomic vision defer
# ---------------------------------------------------------------------------


def _wired_relevance_service(uow, payload: dict, vision_mode: str | None) -> RelevanceService:
    return RelevanceService(
        uow=uow,
        provider=AIStubVerdict(payload),
        model="test-model",
        provider_name="fake",
        vision_mode=vision_mode,
        vision_policy_service=VisionPolicyService(),
    )


@pytest.mark.postgres
class TestRelevanceCompletionWiring:
    async def _ensure_relevance_policy(self, conn, edition):
        return await RelevancePolicyService().ensure_current(
            conn, edition_id=edition.id, config_hash="cfg-a", prompt_version="pv1"
        )

    async def _job_rows(self, pool) -> list[tuple]:
        async with pool.connection() as observer:
            cursor = await observer.execute(
                """
                SELECT task_name, args->>'source_item_revision_id',
                       args->>'relevance_decision_id', args->>'policy_id'
                FROM procrastinate.procrastinate_jobs
                ORDER BY id
                """
            )
            return await cursor.fetchall()

    async def test_needs_media_defers_vision_atomically_in_relevance_only(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content=None, with_photo=True)
        relevance_policy = await self._ensure_relevance_policy(conn, edition)
        service = _wired_relevance_service(
            uow,
            {"status": "needs_media", "confidence": None, "reason": "решающее фото"},
            DEFAULT_VISION_MODE,
        )

        decision = await service.evaluate(revision.id, edition.id, relevance_policy.id)

        assert decision.status == "needs_media"
        vision_policies = await VisionPolicyRepository().list_for_edition(conn, edition.id)
        assert len(vision_policies) == 1
        rows = await self._job_rows(pool)
        vision_rows = [row for row in rows if row[0] == "analyze_vision"]
        assert len(vision_rows) == 1
        assert int(vision_rows[0][1]) == revision.id
        assert int(vision_rows[0][2]) == decision.id
        assert int(vision_rows[0][3]) == vision_policies[0].id

    async def test_relevant_strong_text_defers_nothing_in_relevance_only(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content="Житель сообщил: воду включили вечером")
        relevance_policy = await self._ensure_relevance_policy(conn, edition)
        service = _wired_relevance_service(
            uow,
            {"status": "relevant", "confidence": 0.9, "reason": "локальная новость"},
            "relevance_only",
        )

        decision = await service.evaluate(revision.id, edition.id, relevance_policy.id)

        assert decision.status == "relevant"
        assert await VisionPolicyRepository().list_for_edition(conn, edition.id) == []
        assert [row for row in await self._job_rows(pool) if row[0] == "analyze_vision"] == []

    async def test_off_mode_leaves_needs_media_unscheduled_and_never_irrelevant(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content=None, with_photo=True)
        relevance_policy = await self._ensure_relevance_policy(conn, edition)
        service = _wired_relevance_service(
            uow, {"status": "needs_media", "confidence": None, "reason": "фото"}, "off"
        )

        decision = await service.evaluate(revision.id, edition.id, relevance_policy.id)

        assert decision.status == "needs_media"
        assert await VisionPolicyRepository().list_for_edition(conn, edition.id) == []
        assert [row for row in await self._job_rows(pool) if row[0] == "analyze_vision"] == []

    async def test_full_mode_relevant_photo_waits_for_vision_before_any_claim_task(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        """Claims do not exist until Task 4; today the correct assertion is that
        full-mode relevant+photo defers bounded vision enrichment and queues no
        claim-extraction work ahead of it."""
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content="Прорыв трубы у школы", with_photo=True)
        relevance_policy = await self._ensure_relevance_policy(conn, edition)
        service = _wired_relevance_service(
            uow,
            {"status": "relevant", "confidence": 0.9, "reason": "коммунальная авария"},
            "full",
        )

        decision = await service.evaluate(revision.id, edition.id, relevance_policy.id)

        assert decision.status == "relevant"
        task_names = [row[0] for row in await self._job_rows(pool)]
        assert "analyze_vision" in task_names
        assert not any("claim" in name for name in task_names)

    async def test_duplicate_execution_does_not_double_defer(
        self, uow, pool, conn, edition, revision_factory, production_jobs_app
    ):
        from src import runtime as runtime_module
        from src.bootstrap import ApplicationInfrastructure

        runtime_module.install_runtime(
            ApplicationInfrastructure(pool=pool, uow=uow, procrastinate_app=production_jobs_app)
        )
        revision = await revision_factory(text_content=None, with_photo=True)
        relevance_policy = await self._ensure_relevance_policy(conn, edition)
        existing = await EditionRelevanceDecisionRepository().insert_root(
            conn,
            source_item_revision_id=revision.id,
            edition_id=edition.id,
            relevance_policy_id=relevance_policy.id,
            status="needs_media",
            confidence=None,
            reason="earlier execution won",
        )
        service = _wired_relevance_service(
            uow, {"status": "needs_media", "confidence": None, "reason": "повтор"}, "relevance_only"
        )

        decision = await service.evaluate(revision.id, edition.id, relevance_policy.id)

        assert decision.id == existing.id
        assert [row for row in await self._job_rows(pool) if row[0] == "analyze_vision"] == []


# ---------------------------------------------------------------------------
# Default metadata provider (offline descriptor classification)
# ---------------------------------------------------------------------------


class TestMetadataVisionProvider:
    async def test_descriptors_yield_observations_without_pixels_or_faces(self):
        provider = MetadataVisionProvider()

        outcome = await provider.analyze(
            _asset_descriptor(asset_id=5), context={"edition": "Berdyansk"}
        )

        assert outcome.status == "succeeded"
        assert len(outcome.observations) >= 1
        for observation in outcome.observations:
            assert observation.source_asset_id == 5
            assert observation.metadata.get("pixel_data") is False
            lowered = (observation.text or "").lower()
            assert "face" not in lowered
            assert "лицо" not in lowered

    async def test_descriptor_without_url_reports_unverifiable_basis(self):
        provider = MetadataVisionProvider()

        outcome = await provider.analyze(
            _asset_descriptor(asset_id=6, external_url=None), context={}
        )

        assert outcome.status == "succeeded"
        assert any(obs.metadata.get("external_url") is False for obs in outcome.observations)
