"""Gate authority regressions for exact assignment boundaries."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config_loader import EditionScopeConfig
from src.processing.edition_scope import scope_config_hash
from src.processing.event_authority import EventAuthorityService
from src.processing.event_triage import StoryTriageService
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.stories import StoryRepository


@pytest.mark.postgres
async def test_gate_target_excludes_post_assignment_evidence(conn, edition, revision):
    now = dt.datetime.now(dt.timezone.utc)
    old_source_at = now - dt.timedelta(minutes=10)
    cutoff_at = now - dt.timedelta(minutes=5)
    target_assignment_at = now - dt.timedelta(minutes=5)
    post_cutoff_source_at = now - dt.timedelta(minutes=1)
    post_cutoff_assignment_at = now - dt.timedelta(minutes=6)
    story_id = await StoryRepository().create_story_shell(
        conn, edition_id=edition.id, knowledge_source="event_first"
    )
    cluster_repo = EventClusterRepository()

    await conn.execute(
        "UPDATE source_items SET published_at = %s WHERE id = %s",
        (old_source_at, revision.item_id),
    )
    source_cursor = await conn.execute(
        "SELECT source_id FROM source_items WHERE id = %s", (revision.item_id,)
    )
    source_id = int((await source_cursor.fetchone())[0])
    item_cursor = await conn.execute(
        """
        INSERT INTO source_items (source_id, kind, external_id, first_collected_at)
        VALUES (%s, 'message', %s, %s)
        RETURNING id
        """,
        (source_id, "authority-post-cutoff-item", post_cutoff_source_at),
    )
    post_item_id = int((await item_cursor.fetchone())[0])
    post_revision_cursor = await conn.execute(
        """
        INSERT INTO source_item_revisions (
            source_item_id, revision_no, collected_at, content_hash, text_content
        ) VALUES (%s, 1, %s, %s, 'post-cutoff revision')
        RETURNING id
        """,
        (post_item_id, post_cutoff_source_at, "authority-post-cutoff-revision"),
    )
    post_revision_id = int((await post_revision_cursor.fetchone())[0])

    assignments: list[tuple[int, str]] = []
    fragment_ids: list[int] = []
    for suffix, assigned_at, revision_id in (
        ("old", target_assignment_at, revision.id),
        ("new", post_cutoff_assignment_at, post_revision_id),
    ):
        cursor = await conn.execute(
            """
            INSERT INTO fragment_embedding_vectors (normalized_hash, embedding, model, dimensions)
            VALUES (%s, '[1, 0]'::vector, 'authority-test', 2) RETURNING id
            """,
            (f"authority-vector-{suffix}",),
        )
        vector_id = int((await cursor.fetchone())[0])
        cursor = await conn.execute(
            """
            INSERT INTO source_fragments (
                source_item_revision_id, ordinal, text_content, normalized_hash,
                fragmenter_version, is_candidate, created_at
            ) VALUES (%s, %s, %s, %s, 'v1', TRUE, %s) RETURNING id
            """,
            (
                revision_id,
                len(assignments),
                f"fragment {suffix}",
                f"authority-{suffix}",
                assigned_at,
            ),
        )
        fragment_id = int((await cursor.fetchone())[0])
        cursor = await conn.execute(
            """
            INSERT INTO source_fragment_embeddings (fragment_id, vector_id)
            VALUES (%s, %s) RETURNING id
            """,
            (fragment_id, vector_id),
        )
        embedding_id = int((await cursor.fetchone())[0])
        assignment_id = await cluster_repo.assign_fragment_to_story(
            conn,
            story_id=story_id,
            fragment_id=fragment_id,
            fragment_embedding_id=embedding_id,
            assignment_kind="new_story",
        )
        await conn.execute(
            "UPDATE story_fragments SET assigned_at = %s WHERE id = %s",
            (assigned_at, assignment_id),
        )
        assignments.append((assignment_id, suffix))
        fragment_ids.append(fragment_id)

    await cluster_repo.upsert_cluster_state(
        conn,
        story_id=story_id,
        centroid=[1.0, 0.0],
        model="authority-test",
        dimensions=2,
        fragment_count=2,
        unique_source_count=1,
        first_seen_at=post_cutoff_assignment_at,
        last_seen_at=target_assignment_at,
        latest_assignment_id=assignments[-1][0],
        analysis_dirty=True,
    )
    state = await cluster_repo.get_cluster_state(conn, story_id)
    assert state is not None

    mock_ai = AsyncMock()
    mock_ai.primary_provider_name = "test"
    mock_ai.model_name = "test"
    mock_ai.generate_text.return_value = json.dumps(
        {
            "results": [
                {
                    "story_id": story_id,
                    "scope": "LOCAL",
                    "scope_basis_fragment_ids": [fragment_ids[0]],
                    "scope_confidence": 0.99,
                    "scope_reason": "The older evidence is local.",
                    "retention": "KEEP",
                    "enrichment": "BRIEF",
                    "confidence": 0.99,
                    "reason": "Useful local report.",
                    "brief_payload": {
                        "publishability": "brief",
                        "topic": "Local report",
                        "headline": "Local report",
                        "digest_summary": "The older evidence is retained.",
                        "evidence_items": [
                            {
                                "text": "fragment old",
                                "kind": "community_report",
                                "publication_use": "PUBLISH",
                                "source_fragment_ids": [fragment_ids[0]],
                            }
                        ],
                    },
                }
            ]
        }
    )
    scope = EditionScopeConfig(
        name="Berdyansk", focus_places=("Berdyansk",), direct_impact_only=True
    )
    result = await StoryTriageService(mock_ai, cluster_repo=cluster_repo).triage_stories_batch(
        conn,
        [state],
        edition_id=edition.id,
        scope_config=scope,
        scope_hash=scope_config_hash(scope),
        assignment_id_by_story={story_id: assignments[0][0]},
        source_cutoff_at=cutoff_at,
    )

    assert result.deferred_story_ids == ()
    prompt = mock_ai.generate_text.await_args.kwargs["prompt"]
    assert "fragment old" in prompt
    assert "fragment new" not in prompt
    rows = await (
        await conn.execute(
            """
            SELECT latest_assignment_id
            FROM story_event_triage_decisions
            WHERE story_id = %s
            """,
            (story_id,),
        )
    ).fetchall()
    assert [int(row[0]) for row in rows] == [assignments[0][0]]


def _create_test_authority_service(
    triage_split_max_extra_calls_per_cycle: int = 8,
):
    runtime = SimpleNamespace(uow=MagicMock())
    runtime.uow.transaction.return_value.__aenter__.return_value = AsyncMock()

    config = SimpleNamespace(
        settings=SimpleNamespace(
            event_pipeline=SimpleNamespace(
                triage_batch_size=10,
                event_processing_stage_lease_seconds=600,
                authority_coordination_lease_seconds=10,
                triage_excerpt_chars=320,
                triage_min_ignore_confidence=0.95,
                authority_provider_timeout_seconds=540,
                triage_split_max_extra_calls_per_cycle=triage_split_max_extra_calls_per_cycle,
                triage_max_attempts_per_assignment=2,
                provider_retry_backoff_seconds=300,
                provider_retry_backoff_max_seconds=3600,
            )
        )
    )

    cluster_repo = SimpleNamespace(
        get_cluster_state=AsyncMock(),
        mark_cluster_processed_without_analysis=AsyncMock(),
    )
    authority_repo = SimpleNamespace(
        filter_unsatisfied_targets=AsyncMock(
            side_effect=lambda conn, targets, snapshot_at: list(targets)
        )
    )
    retry_repo = SimpleNamespace(
        get_for_assignments=AsyncMock(return_value={}),
        record_failure=AsyncMock(),
        clear=AsyncMock(),
    )
    claim_repo = SimpleNamespace(
        acquire_cycle=AsyncMock(return_value=SimpleNamespace(edition_id=1)),
        release_cycle=AsyncMock(),
        try_claim_stage=AsyncMock(
            side_effect=lambda conn, story_id, latest_assignment_id, stage, owner_id, ttl_seconds: (
                SimpleNamespace(
                    story_id=story_id,
                    latest_assignment_id=latest_assignment_id,
                    stage=stage,
                    claim_token="token",
                )
            )
        ),
        release_stage=AsyncMock(),
        renew_stage=AsyncMock(),
        lock_stage_claim_if_live=AsyncMock(return_value=True),
    )
    triage_service = SimpleNamespace(
        triage_stories_batch=AsyncMock(),
    )
    brief_service = SimpleNamespace(
        persist_brief=AsyncMock(),
    )

    service = EventAuthorityService(
        runtime=runtime,
        config=config,
        cluster_repo=cluster_repo,
        authority_repo=authority_repo,
        retry_repo=retry_repo,
        claim_repo=claim_repo,
        triage_service=triage_service,
        brief_service=brief_service,
    )
    return service


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_batch_partial_gate_recovers_missing_story_via_singleton():
    from src.processing.event_authority import AuthorityTarget
    from src.processing.event_triage import StoryGateBatchResult, StoryGateResult

    service = _create_test_authority_service(triage_split_max_extra_calls_per_cycle=8)

    state1 = SimpleNamespace(story_id=1, latest_assignment_id=10)
    state2 = SimpleNamespace(story_id=2, latest_assignment_id=20)
    service.cluster_repo.get_cluster_state.side_effect = lambda conn, sid: (
        state1 if sid == 1 else state2
    )

    gate1 = StoryGateResult(
        story_id=1,
        scope="LOCAL",
        scope_confidence=0.99,
        scope_reason="ok",
        retention="KEEP",
        enrichment="BRIEF",
        exclusion_reason=None,
        confidence=0.95,
        reason="ok",
        brief_payload=None,
    )
    gate2 = StoryGateResult(
        story_id=2,
        scope="LOCAL",
        scope_confidence=0.99,
        scope_reason="ok",
        retention="KEEP",
        enrichment="BRIEF",
        exclusion_reason=None,
        confidence=0.95,
        reason="ok",
        brief_payload=None,
    )

    # First call: missing sid 2
    initial_res = StoryGateBatchResult(
        results=(gate1,),
        deferred_story_ids=(2,),
        missing_story_ids=(2,),
        batch_error_kind="partial_response",
        prompt_hash="p1",
    )
    # Second call (singleton recovery): succeeds for sid 2
    recovery_res = StoryGateBatchResult(
        results=(gate2,),
        deferred_story_ids=(),
        missing_story_ids=(),
        prompt_hash="p2",
    )
    service.triage_service.triage_stories_batch.side_effect = [initial_res, recovery_res]

    targets = [
        AuthorityTarget(
            edition_id=1,
            story_id=1,
            assignment_id=10,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
        AuthorityTarget(
            edition_id=1,
            story_id=2,
            assignment_id=20,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
    ]

    with patch(
        "src.processing.event_authority.resolve_edition_scope",
        AsyncMock(return_value=("berdyansk", SimpleNamespace(focus_places=("Бердянск",)))),
    ):
        result = await service.process_batch(targets, mode="background")

    assert result.stats.triaged == 2
    assert service.triage_service.triage_stories_batch.await_count == 2
    # Verify singleton call passed story 2
    second_call_args = service.triage_service.triage_stories_batch.await_args_list[1]
    assert second_call_args.args[1] == [state2]
    assert second_call_args.kwargs["assignment_id_by_story"] == {2: 20}

    # Verify retry_repo calls: clear for both, record_failure NEVER called
    cleared_sids = [call.kwargs["story_id"] for call in service.retry_repo.clear.await_args_list]
    assert set(cleared_sids) == {1, 2}
    service.retry_repo.record_failure.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_batch_partial_gate_budget_exhausted_leaves_pending_without_durable_attempt():
    from src.processing.event_authority import AuthorityTarget
    from src.processing.event_triage import StoryGateBatchResult, StoryGateResult

    # Budget = 0 extra calls
    service = _create_test_authority_service(triage_split_max_extra_calls_per_cycle=0)

    state1 = SimpleNamespace(story_id=1, latest_assignment_id=10)
    state2 = SimpleNamespace(story_id=2, latest_assignment_id=20)
    service.cluster_repo.get_cluster_state.side_effect = lambda conn, sid: (
        state1 if sid == 1 else state2
    )

    gate1 = StoryGateResult(
        story_id=1,
        scope="LOCAL",
        scope_confidence=0.99,
        scope_reason="ok",
        retention="KEEP",
        enrichment="BRIEF",
        exclusion_reason=None,
        confidence=0.95,
        reason="ok",
        brief_payload=None,
    )

    initial_res = StoryGateBatchResult(
        results=(gate1,),
        deferred_story_ids=(2,),
        missing_story_ids=(2,),
        batch_error_kind="partial_response",
        prompt_hash="p1",
    )
    service.triage_service.triage_stories_batch.return_value = initial_res

    targets = [
        AuthorityTarget(
            edition_id=1,
            story_id=1,
            assignment_id=10,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
        AuthorityTarget(
            edition_id=1,
            story_id=2,
            assignment_id=20,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
    ]

    with patch(
        "src.processing.event_authority.resolve_edition_scope",
        AsyncMock(return_value=("berdyansk", SimpleNamespace(focus_places=("Бердянск",)))),
    ):
        result = await service.process_batch(targets, mode="background")

    assert result.stats.triaged == 1
    # No extra singleton call was made
    assert service.triage_service.triage_stories_batch.await_count == 1

    # Story 1 cleared, story 2 record_failure NOT called
    cleared_sids = [call.kwargs["story_id"] for call in service.retry_repo.clear.await_args_list]
    assert cleared_sids == [1]
    service.retry_repo.record_failure.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_batch_partial_gate_singleton_failure_records_durable_failure():
    from src.processing.event_authority import AuthorityTarget
    from src.processing.event_triage import StoryGateBatchResult, StoryGateResult

    service = _create_test_authority_service(triage_split_max_extra_calls_per_cycle=8)

    state1 = SimpleNamespace(story_id=1, latest_assignment_id=10)
    state2 = SimpleNamespace(story_id=2, latest_assignment_id=20)
    service.cluster_repo.get_cluster_state.side_effect = lambda conn, sid: (
        state1 if sid == 1 else state2
    )

    gate1 = StoryGateResult(
        story_id=1,
        scope="LOCAL",
        scope_confidence=0.99,
        scope_reason="ok",
        retention="KEEP",
        enrichment="BRIEF",
        exclusion_reason=None,
        confidence=0.95,
        reason="ok",
        brief_payload=None,
    )

    initial_res = StoryGateBatchResult(
        results=(gate1,),
        deferred_story_ids=(2,),
        missing_story_ids=(2,),
        batch_error_kind="partial_response",
        prompt_hash="p1",
    )
    failed_singleton_res = StoryGateBatchResult(
        results=(),
        deferred_story_ids=(2,),
        missing_story_ids=(),
        batch_error_kind="parse_failure",
        prompt_hash="p2",
    )
    service.triage_service.triage_stories_batch.side_effect = [initial_res, failed_singleton_res]

    targets = [
        AuthorityTarget(
            edition_id=1,
            story_id=1,
            assignment_id=10,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
        AuthorityTarget(
            edition_id=1,
            story_id=2,
            assignment_id=20,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
    ]

    with patch(
        "src.processing.event_authority.resolve_edition_scope",
        AsyncMock(return_value=("berdyansk", SimpleNamespace(focus_places=("Бердянск",)))),
    ):
        result = await service.process_batch(targets, mode="background")

    assert result.stats.triaged == 1
    assert service.triage_service.triage_stories_batch.await_count == 2

    # Story 1 cleared
    cleared_sids = [call.kwargs["story_id"] for call in service.retry_repo.clear.await_args_list]
    assert cleared_sids == [1]

    # Story 2 failed isolated recovery: durable record_failure is called
    service.retry_repo.record_failure.assert_awaited_once()
    call_kwargs = service.retry_repo.record_failure.await_args.kwargs
    assert call_kwargs["story_id"] == 2
    assert call_kwargs["latest_assignment_id"] == 20
    assert call_kwargs["stage"] == "triage"
    assert call_kwargs["error_kind"] == "parse_failure"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_heartbeat_remains_active_across_singleton_recovery():
    from src.processing.event_authority import AuthorityTarget
    from src.processing.event_triage import StoryGateBatchResult, StoryGateResult

    service = _create_test_authority_service(triage_split_max_extra_calls_per_cycle=8)
    service.config.settings.event_pipeline.event_processing_stage_lease_seconds = 3

    state1 = SimpleNamespace(story_id=1, latest_assignment_id=10)
    state2 = SimpleNamespace(story_id=2, latest_assignment_id=20)
    service.cluster_repo.get_cluster_state.side_effect = lambda conn, sid: (
        state1 if sid == 1 else state2
    )

    gate1 = StoryGateResult(
        story_id=1,
        scope="LOCAL",
        scope_confidence=0.99,
        scope_reason="ok",
        retention="KEEP",
        enrichment="BRIEF",
        exclusion_reason=None,
        confidence=0.95,
        reason="ok",
        brief_payload=None,
    )
    gate2 = StoryGateResult(
        story_id=2,
        scope="LOCAL",
        scope_confidence=0.99,
        scope_reason="ok",
        retention="KEEP",
        enrichment="BRIEF",
        exclusion_reason=None,
        confidence=0.95,
        reason="ok",
        brief_payload=None,
    )

    initial_res = StoryGateBatchResult(
        results=(gate1,),
        deferred_story_ids=(2,),
        missing_story_ids=(2,),
        batch_error_kind="partial_response",
        prompt_hash="p1",
    )
    recovery_res = StoryGateBatchResult(
        results=(gate2,),
        deferred_story_ids=(),
        missing_story_ids=(),
        prompt_hash="p2",
    )

    call_count = 0

    async def mock_triage(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return initial_res
        await asyncio.sleep(1.2)
        return recovery_res

    service.triage_service.triage_stories_batch.side_effect = mock_triage

    targets = [
        AuthorityTarget(
            edition_id=1,
            story_id=1,
            assignment_id=10,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
        AuthorityTarget(
            edition_id=1,
            story_id=2,
            assignment_id=20,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
    ]

    with patch(
        "src.processing.event_authority.resolve_edition_scope",
        AsyncMock(return_value=("berdyansk", SimpleNamespace(focus_places=("Бердянск",)))),
    ):
        result = await service.process_batch(targets, mode="background")

    assert result.stats.triaged == 2
    # Verify renew_stage was called by heartbeat during slow recovery
    assert service.claim_repo.renew_stage.await_count >= 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_batch_shared_provider_deadline_halts_recovery_when_time_exhausted(
    monkeypatch,
):
    from src.processing.event_authority import AuthorityTarget
    from src.processing.event_triage import StoryGateBatchResult, StoryGateResult

    service = _create_test_authority_service(triage_split_max_extra_calls_per_cycle=8)

    state1 = SimpleNamespace(story_id=1, latest_assignment_id=10)
    state2 = SimpleNamespace(story_id=2, latest_assignment_id=20)
    service.cluster_repo.get_cluster_state.side_effect = lambda conn, sid: (
        state1 if sid == 1 else state2
    )

    gate1 = StoryGateResult(
        story_id=1,
        scope="LOCAL",
        scope_confidence=0.99,
        scope_reason="ok",
        retention="KEEP",
        enrichment="BRIEF",
        exclusion_reason=None,
        confidence=0.95,
        reason="ok",
        brief_payload=None,
    )

    initial_res = StoryGateBatchResult(
        results=(gate1,),
        deferred_story_ids=(2,),
        missing_story_ids=(2,),
        batch_error_kind="partial_response",
        prompt_hash="p1",
    )

    loop = asyncio.get_running_loop()
    current_time = 1000.0

    def fake_time():
        return current_time

    monkeypatch.setattr(loop, "time", fake_time)

    async def mock_triage(*args, **kwargs):
        nonlocal current_time
        # Initial call consumes the entire 540s budget
        current_time += 540.0
        return initial_res

    service.triage_service.triage_stories_batch.side_effect = mock_triage

    targets = [
        AuthorityTarget(
            edition_id=1,
            story_id=1,
            assignment_id=10,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
        AuthorityTarget(
            edition_id=1,
            story_id=2,
            assignment_id=20,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
    ]

    with patch(
        "src.processing.event_authority.resolve_edition_scope",
        AsyncMock(return_value=("berdyansk", SimpleNamespace(focus_places=("Бердянск",)))),
    ):
        result = await service.process_batch(targets, mode="background")

    from unittest.mock import ANY

    # Only 1 story triaged
    assert result.stats.triaged == 1
    # Only initial batch triage was called; singleton recovery was skipped because remaining time <= 0
    assert service.triage_service.triage_stories_batch.await_count == 1
    # Story 1 cleared
    service.retry_repo.clear.assert_awaited_once_with(
        ANY, story_id=1, latest_assignment_id=10, stage="triage"
    )
    # Story 2 was NOT attempted in recovery and was NOT recorded as a durable failure
    service.retry_repo.record_failure.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_batch_singleton_isolated_timeout_consumes_durable_attempt_and_halts_further_singletons(
    monkeypatch,
):
    from src.processing.event_authority import AuthorityTarget
    from src.processing.event_triage import StoryGateBatchResult, StoryGateResult

    service = _create_test_authority_service(triage_split_max_extra_calls_per_cycle=8)

    state1 = SimpleNamespace(story_id=1, latest_assignment_id=10)
    state2 = SimpleNamespace(story_id=2, latest_assignment_id=20)
    state3 = SimpleNamespace(story_id=3, latest_assignment_id=30)
    service.cluster_repo.get_cluster_state.side_effect = lambda conn, sid: {
        1: state1,
        2: state2,
        3: state3,
    }[sid]

    gate1 = StoryGateResult(
        story_id=1,
        scope="LOCAL",
        scope_confidence=0.99,
        scope_reason="ok",
        retention="KEEP",
        enrichment="BRIEF",
        exclusion_reason=None,
        confidence=0.95,
        reason="ok",
        brief_payload=None,
    )

    initial_res = StoryGateBatchResult(
        results=(gate1,),
        deferred_story_ids=(2, 3),
        missing_story_ids=(2, 3),
        batch_error_kind="partial_response",
        prompt_hash="p1",
    )

    loop = asyncio.get_running_loop()
    current_time = 1000.0

    def fake_time():
        return current_time

    monkeypatch.setattr(loop, "time", fake_time)

    call_count = 0

    async def mock_triage(*args, **kwargs):
        nonlocal current_time, call_count
        call_count += 1
        if call_count == 1:
            # Initial call takes 530s, leaving 10s
            current_time += 530.0
            return initial_res
        elif call_count == 2:
            # Singleton for story 2: advances time by 15s (exceeds remaining 10s budget)
            current_time += 15.0
            raise asyncio.TimeoutError()
        raise AssertionError("Story 3 should not be called")

    service.triage_service.triage_stories_batch.side_effect = mock_triage

    targets = [
        AuthorityTarget(
            edition_id=1,
            story_id=1,
            assignment_id=10,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
        AuthorityTarget(
            edition_id=1,
            story_id=2,
            assignment_id=20,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
        AuthorityTarget(
            edition_id=1,
            story_id=3,
            assignment_id=30,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
    ]

    with patch(
        "src.processing.event_authority.resolve_edition_scope",
        AsyncMock(return_value=("berdyansk", SimpleNamespace(focus_places=("Бердянск",)))),
    ):
        result = await service.process_batch(targets, mode="background")

    assert result.stats.triaged == 1
    assert service.triage_service.triage_stories_batch.await_count == 2

    # Story 2 (which started and timed out) gets durable failure with error_kind="timeout"
    service.retry_repo.record_failure.assert_awaited_once()
    record_call = service.retry_repo.record_failure.await_args
    assert record_call.kwargs["story_id"] == 2
    assert record_call.kwargs["latest_assignment_id"] == 20
    assert record_call.kwargs["stage"] == "triage"
    assert record_call.kwargs["error_kind"] == "timeout"
    assert record_call.kwargs["exhausted"] is False

    # Story 3 was never started and was not recorded as failure
    assert 3 not in [
        call.kwargs["story_id"] for call in service.retry_repo.record_failure.await_args_list
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_batch_invalid_response_triggers_singleton_recovery():
    from src.processing.event_authority import AuthorityTarget
    from src.processing.event_triage import StoryGateBatchResult, StoryGateResult

    service = _create_test_authority_service(triage_split_max_extra_calls_per_cycle=8)

    state1 = SimpleNamespace(story_id=1, latest_assignment_id=10)
    state2 = SimpleNamespace(story_id=2, latest_assignment_id=20)
    service.cluster_repo.get_cluster_state.side_effect = lambda conn, sid: (
        state1 if sid == 1 else state2
    )

    gate1 = StoryGateResult(
        story_id=1,
        scope="LOCAL",
        scope_confidence=0.99,
        scope_reason="ok",
        retention="KEEP",
        enrichment="BRIEF",
        exclusion_reason=None,
        confidence=0.95,
        reason="ok",
        brief_payload=None,
    )
    gate2 = StoryGateResult(
        story_id=2,
        scope="LOCAL",
        scope_confidence=0.99,
        scope_reason="ok",
        retention="KEEP",
        enrichment="BRIEF",
        exclusion_reason=None,
        confidence=0.95,
        reason="ok",
        brief_payload=None,
    )

    # Initial call returns story 1 valid, story 2 schema-invalid (not missing, but invalid)
    initial_res = StoryGateBatchResult(
        results=(gate1,),
        deferred_story_ids=(2,),
        missing_story_ids=(),
        invalid_story_ids=(2,),
        batch_error_kind="invalid_response",
        prompt_hash="p1",
    )
    # Singleton recovery succeeds for story 2
    recovery_res = StoryGateBatchResult(
        results=(gate2,),
        deferred_story_ids=(),
        missing_story_ids=(),
        invalid_story_ids=(),
        prompt_hash="p2",
    )
    service.triage_service.triage_stories_batch.side_effect = [initial_res, recovery_res]

    targets = [
        AuthorityTarget(
            edition_id=1,
            story_id=1,
            assignment_id=10,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
        AuthorityTarget(
            edition_id=1,
            story_id=2,
            assignment_id=20,
            scope_config_hash="h1",
            triage_version="v2",
            scope_version="v1",
            source_cutoff_at=None,
        ),
    ]

    with patch(
        "src.processing.event_authority.resolve_edition_scope",
        AsyncMock(return_value=("berdyansk", SimpleNamespace(focus_places=("Бердянск",)))),
    ):
        result = await service.process_batch(targets, mode="background")

    assert result.stats.triaged == 2
    assert service.triage_service.triage_stories_batch.await_count == 2
    # Second call was for story 2
    assert service.triage_service.triage_stories_batch.await_args_list[1].args[1] == [state2]

    # Both stories cleared, 0 durable failures
    cleared = {call.kwargs["story_id"] for call in service.retry_repo.clear.await_args_list}
    assert cleared == {1, 2}
    service.retry_repo.record_failure.assert_not_awaited()
