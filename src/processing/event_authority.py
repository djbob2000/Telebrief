"""Bounded, assignment-scoped Gate authority processing.

This service deliberately stops at Gate/scope and the minimal exact-assignment
brief. Rich Analysis is optional enrichment and is dispatched by a separate
job after this service returns.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from src.ai_providers import create_provider
from src.config_loader import load_config
from src.domain.event_authority import AuthorityTarget
from src.domain.event_clusters import StoryClusterState
from src.processing.edition_scope import resolve_edition_scope
from src.processing.event_brief import EventBriefService
from src.processing.event_triage import StoryGateBatchResult, StoryTriageService
from src.processing.retry_policy import decide_retry
from src.repositories.event_authority import EventAuthorityRepository
from src.repositories.event_clusters import EventClusterRepository
from src.repositories.event_processing_claims import (
    EventProcessingClaimRepository,
    EventProcessingStageClaim,
)
from src.repositories.event_retries import EventProcessingRetryRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedAuthorityTarget:
    target: AuthorityTarget
    state: StoryClusterState
    claim: EventProcessingStageClaim


@dataclass
class AuthorityBatchStats:
    requested: int = 0
    already_satisfied: int = 0
    claimed: int = 0
    busy: int = 0
    triaged: int = 0
    retry_wait: int = 0
    terminal: int = 0
    provider_failures: int = 0


@dataclass(frozen=True)
class AuthorityBatchResult:
    stats: AuthorityBatchStats
    enrichment_targets: tuple[AuthorityTarget, ...] = ()


class EventAuthorityService:
    """Process at most one Gate batch without holding edition coordination."""

    def __init__(
        self,
        runtime: Any,
        config: Any,
        *,
        cluster_repo: EventClusterRepository,
        authority_repo: EventAuthorityRepository,
        retry_repo: EventProcessingRetryRepository,
        claim_repo: EventProcessingClaimRepository,
        triage_service: StoryTriageService,
        brief_service: EventBriefService,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self.cluster_repo = cluster_repo
        self.authority_repo = authority_repo
        self.retry_repo = retry_repo
        self.claim_repo = claim_repo
        self.triage_service = triage_service
        self.brief_service = brief_service

    @classmethod
    def from_runtime(cls, runtime: Any, config: Any | None = None) -> "EventAuthorityService":
        config = config or getattr(runtime, "config", None) or load_config()
        cfg = config.settings.event_pipeline
        provider = getattr(runtime, "provider_cascade", None) or create_provider(
            config.settings.ai_provider,
            logger,
            openai_api_key=config.openai_api_key,
            anthropic_api_key=config.anthropic_api_key,
            google_api_key=config.gemini_api_key,
            openrouter_api_key=config.openrouter_api_key,
            openrouter_model=config.openrouter_model,
            openrouter_model_2=getattr(config, "openrouter_model_2", ""),
            openrouter_models=getattr(config, "openrouter_models", None),
        )
        cluster_repo = EventClusterRepository()
        return cls(
            runtime,
            config,
            cluster_repo=cluster_repo,
            authority_repo=EventAuthorityRepository(),
            retry_repo=EventProcessingRetryRepository(),
            claim_repo=EventProcessingClaimRepository(),
            triage_service=StoryTriageService(
                provider,
                cluster_repo=cluster_repo,
                model=config.settings.ai_model,
                uow=runtime.uow,
                max_output_tokens=cfg.triage_max_output_tokens,
                reasoning_effort=cfg.triage_reasoning_effort,
            ),
            brief_service=EventBriefService(cluster_repo=cluster_repo),
        )

    async def process_batch(
        self,
        targets: Sequence[AuthorityTarget],
        *,
        mode: Literal["background", "publication"],
    ) -> AuthorityBatchResult:
        publication_mode = mode == "publication"
        cfg = self.config.settings.event_pipeline
        batch = list(targets[: cfg.triage_batch_size])
        stats = AuthorityBatchStats(requested=len(batch))
        if not batch:
            return AuthorityBatchResult(stats)
        editions = {target.edition_id for target in batch}
        if len(editions) != 1:
            raise ValueError("one authority batch must contain exactly one edition")
        edition_id = next(iter(editions))
        now = dt.datetime.now(dt.timezone.utc)
        owner_id = f"authority-{os.getpid()}-{uuid.uuid4().hex}"

        async with self.runtime.uow.transaction() as conn:
            retry_states = await self.retry_repo.get_for_assignments(
                conn,
                [(target.story_id, target.assignment_id) for target in batch],
                stage="triage",
            )
        processable: list[AuthorityTarget] = []
        for target in batch:
            retry = retry_states.get((target.story_id, target.assignment_id))
            if retry is not None and retry.exhausted_at is not None:
                stats.terminal += 1
            elif (
                retry is not None and retry.next_retry_at is not None and retry.next_retry_at > now
            ):
                stats.retry_wait += 1
            else:
                processable.append(target)
        if not processable:
            return AuthorityBatchResult(stats)

        claimed: list[ClaimedAuthorityTarget] = []
        async with self.runtime.uow.transaction() as conn:
            cycle = await self.claim_repo.acquire_cycle(
                conn,
                edition_id=edition_id,
                owner_id=owner_id,
                ttl_seconds=cfg.authority_coordination_lease_seconds,
            )
            if cycle is None:
                stats.busy += len(processable)
                return AuthorityBatchResult(stats)
            try:
                for target in processable:
                    state = await self.cluster_repo.get_cluster_state(conn, target.story_id)
                    if state is None:
                        continue
                    claim = await self.claim_repo.try_claim_stage(
                        conn,
                        story_id=target.story_id,
                        latest_assignment_id=target.assignment_id,
                        stage="triage",
                        owner_id=owner_id,
                        ttl_seconds=cfg.event_processing_stage_lease_seconds,
                    )
                    if claim is None:
                        stats.busy += 1
                        continue
                    claimed.append(ClaimedAuthorityTarget(target, state, claim))
                    stats.claimed += 1
            finally:
                await self.claim_repo.release_cycle(conn, cycle)

        if not claimed:
            return AuthorityBatchResult(stats)

        try:
            async with self.runtime.uow.transaction() as conn:
                snapshot_at = next(
                    (target.snapshot_at for target in batch if target.snapshot_at is not None),
                    dt.datetime.now(dt.timezone.utc),
                )
                unsatisfied = await self.authority_repo.filter_unsatisfied_targets(
                    conn,
                    targets=[item.target for item in claimed],
                    snapshot_at=snapshot_at,
                )
            unsatisfied_keys = {(target.story_id, target.assignment_id) for target in unsatisfied}
            already_satisfied = [
                item
                for item in claimed
                if (item.target.story_id, item.target.assignment_id) not in unsatisfied_keys
            ]
            stats.already_satisfied += len(already_satisfied)
            if already_satisfied:
                async with self.runtime.uow.transaction() as conn:
                    for item in already_satisfied:
                        await self.claim_repo.release_stage(conn, item.claim)
            claimed = [item for item in claimed if item not in already_satisfied]
            if not claimed:
                return AuthorityBatchResult(stats)

            async with self.runtime.uow.transaction() as conn:
                _slug, scope_config = await resolve_edition_scope(conn, self.config, edition_id)
            assignment_map = {item.target.story_id: item.target.assignment_id for item in claimed}
            claim_map = {
                (item.target.story_id, item.target.assignment_id): item.claim for item in claimed
            }

            async def decision_fence(write_conn: Any, story_id: int, assignment_id: int) -> bool:
                claim = claim_map.get((story_id, assignment_id))
                return claim is not None and await self.claim_repo.lock_stage_claim_if_live(
                    write_conn, claim
                )

            async def before_decision_persist(
                write_conn: Any, result: Any, assignment_id: int
            ) -> None:
                if result.retention == "KEEP":
                    await self.brief_service.persist_brief(
                        write_conn,
                        story_id=result.story_id,
                        assignment_id=assignment_id,
                        payload=result.brief_payload,
                        exact_assignment=True,
                        merge_existing_analysis=not publication_mode,
                    )

            heartbeat_stop = asyncio.Event()

            async def renew_claims() -> None:
                interval = max(1.0, cfg.event_processing_stage_lease_seconds / 3)
                while True:
                    try:
                        await asyncio.wait_for(heartbeat_stop.wait(), timeout=interval)
                        return
                    except asyncio.TimeoutError:
                        async with self.runtime.uow.transaction() as heartbeat_conn:
                            for item in claimed:
                                await self.claim_repo.renew_stage(
                                    heartbeat_conn,
                                    item.claim,
                                    ttl_seconds=cfg.event_processing_stage_lease_seconds,
                                )

            heartbeat_task = asyncio.create_task(renew_claims())
            try:
                try:
                    result = await asyncio.wait_for(
                        self.triage_service.triage_stories_batch(
                            None,
                            [item.state for item in claimed],
                            edition_id=edition_id,
                            scope_config=scope_config,
                            scope_hash=batch[0].scope_config_hash,
                            excerpt_chars=cfg.triage_excerpt_chars,
                            min_ignore_confidence=cfg.triage_min_ignore_confidence,
                            assignment_id_by_story=assignment_map,
                            source_cutoff_at=next(
                                (
                                    target.source_cutoff_at
                                    for target in (item.target for item in claimed)
                                    if target.source_cutoff_at is not None
                                ),
                                None,
                            ),
                            decision_fence=decision_fence,
                            before_decision_persist=before_decision_persist,
                        ),
                        timeout=cfg.authority_provider_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    result = StoryGateBatchResult(
                        results=(),
                        deferred_story_ids=tuple(item.target.story_id for item in claimed),
                        batch_error_kind="timeout",
                    )

                stats.triaged += len(result.results)
                result_by_id = {gate.story_id: gate for gate in result.results}
                fence_lost = set(result.fence_lost_story_ids)
                deferred = set(result.deferred_story_ids) - fence_lost
                missing = set(result.missing_story_ids) - fence_lost

                singleton_failures: dict[int, StoryGateBatchResult] = {}
                partial_targets = [item for item in claimed if item.target.story_id in missing]
                extra_calls_budget = cfg.triage_split_max_extra_calls_per_cycle
                for item in partial_targets:
                    if extra_calls_budget <= 0:
                        break
                    extra_calls_budget -= 1
                    try:
                        singleton_result = await asyncio.wait_for(
                            self.triage_service.triage_stories_batch(
                                None,
                                [item.state],
                                edition_id=edition_id,
                                scope_config=scope_config,
                                scope_hash=batch[0].scope_config_hash,
                                excerpt_chars=cfg.triage_excerpt_chars,
                                min_ignore_confidence=cfg.triage_min_ignore_confidence,
                                assignment_id_by_story={
                                    item.target.story_id: item.target.assignment_id
                                },
                                source_cutoff_at=item.target.source_cutoff_at,
                                decision_fence=decision_fence,
                                before_decision_persist=before_decision_persist,
                            ),
                            timeout=cfg.authority_provider_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        singleton_result = StoryGateBatchResult(
                            results=(),
                            deferred_story_ids=(item.target.story_id,),
                            batch_error_kind="timeout",
                        )

                    if singleton_result.results:
                        recovered_gate = singleton_result.results[0]
                        result_by_id[recovered_gate.story_id] = recovered_gate
                        deferred.discard(recovered_gate.story_id)
                        missing.discard(recovered_gate.story_id)
                        stats.triaged += 1
                    elif singleton_result.fence_lost_story_ids:
                        for fid in singleton_result.fence_lost_story_ids:
                            fence_lost.add(fid)
                            deferred.discard(fid)
                            missing.discard(fid)
                    else:
                        missing.discard(item.target.story_id)
                        singleton_failures[item.target.story_id] = singleton_result
            finally:
                heartbeat_stop.set()
                await heartbeat_task

            durable_failure_ids = deferred - missing
            if result.batch_error_kind is not None or singleton_failures:
                stats.provider_failures += len(durable_failure_ids)
            if durable_failure_ids:
                async with self.runtime.uow.transaction() as conn:
                    for item in claimed:
                        if item.target.story_id not in durable_failure_ids:
                            continue
                        previous = retry_states.get(
                            (item.target.story_id, item.target.assignment_id)
                        )
                        failure_res = singleton_failures.get(item.target.story_id)
                        error_kind = (
                            (failure_res.batch_error_kind if failure_res else None)
                            or result.batch_error_kind
                            or "other"
                        )
                        prompt_hash = (
                            failure_res.prompt_hash if failure_res else None
                        ) or result.prompt_hash
                        retry_decision = decide_retry(
                            error_kind,
                            attempt_count=(previous.attempt_count if previous else 0) + 1,
                            max_attempts=cfg.triage_max_attempts_per_assignment,
                            base_backoff_seconds=cfg.provider_retry_backoff_seconds,
                            max_backoff_seconds=cfg.provider_retry_backoff_max_seconds,
                            now=now,
                        )
                        await self.retry_repo.record_failure(
                            conn,
                            story_id=item.target.story_id,
                            latest_assignment_id=item.target.assignment_id,
                            stage="triage",
                            error_kind=error_kind,
                            next_retry_at=retry_decision.next_retry_at,
                            exhausted=retry_decision.exhausted,
                            prompt_hash=prompt_hash,
                        )

            enrichment: list[AuthorityTarget] = []
            async with self.runtime.uow.transaction() as conn:
                for item in claimed:
                    if item.target.story_id in deferred or item.target.story_id in fence_lost:
                        continue
                    gate = result_by_id.get(item.target.story_id)
                    if gate is None:
                        continue
                    await self.retry_repo.clear(
                        conn,
                        story_id=item.target.story_id,
                        latest_assignment_id=item.target.assignment_id,
                        stage="triage",
                    )
                    if gate.scope in {"OUT_OF_SCOPE", "UNCERTAIN"} or gate.retention == "DROP":
                        await self.cluster_repo.mark_cluster_processed_without_analysis(
                            conn,
                            story_id=item.target.story_id,
                            assignment_id=item.target.assignment_id,
                        )
                    elif gate.enrichment == "ANALYZE":
                        enrichment.append(item.target)
                    else:
                        await self.cluster_repo.mark_cluster_processed_without_analysis(
                            conn,
                            story_id=item.target.story_id,
                            assignment_id=item.target.assignment_id,
                        )
            return AuthorityBatchResult(stats, tuple(enrichment))
        finally:
            async with self.runtime.uow.transaction() as conn:
                for item in claimed:
                    await self.claim_repo.release_stage(conn, item.claim)
