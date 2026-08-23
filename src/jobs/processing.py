"""Durable knowledge-processing jobs: edition relevance decisions.

``evaluate_relevance`` receives the EXACT policy id resolved at ingestion
time, so a job retried by Procrastinate keeps its originally queued policy
even when a newer relevance policy becomes current in the meantime — every
decision stays attributable to the prompt/config that produced it.

Retry math (Plan 2 lesson): procrastinate ``max_attempts`` counts TOTAL
executions, so ``max_attempts=3`` is the initial attempt plus exactly two
retries. Only :class:`~src.processing.relevance.TransientProcessingError`
enters the retry loop; on the final failed attempt the gate below persists
``uncertain(provider_unavailable)`` and returns successfully (conservative
fail-open: an unavailable AI never yields a false "irrelevant").

Duplicate executions are acceptable: immutable decisions plus the canonical
uq_root_relevance_decision constraint are authoritative, so no queueing lock
is used as a correctness mechanism.
"""

from __future__ import annotations

import logging

import procrastinate

from src.ai_providers import create_provider
from src.jobs.app import procrastinate_app
from src.processing.relevance import (
    ProviderUnavailableError,
    RelevanceService,
    TransientProcessingError,
)
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    RelevancePolicyVersionRepository,
)
from src.runtime import get_runtime

logger = logging.getLogger(__name__)

EVALUATE_RELEVANCE_TASK_NAME = "evaluate_relevance"
PROCESSING_QUEUE = "processing"

BACKFILL_BATCH_SIZE = 500

# Total executions = initial attempt + 2 retries; waits mirror the collection
# strategy: 30s after the first failure, then 90s.
RELEVANCE_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=3,
    wait=30,
    linear_wait=60,
    retry_exceptions=(TransientProcessingError,),
)


def build_relevance_service() -> RelevanceService:
    """Assemble the AI-backed relevance service from the real config."""
    from src.config_loader import load_config

    config = load_config()
    return RelevanceService(
        uow=get_runtime().uow,
        provider=create_provider(
            provider_name=config.settings.ai_provider,
            logger=logger,
            openai_api_key=config.openai_api_key,
            openai_base_url=config.openai_base_url,
            anthropic_api_key=config.anthropic_api_key,
            google_api_key=config.google_api_key,
            google_api_keys=config.google_api_backup_keys,
            openrouter_api_key=config.openrouter_api_key,
            openrouter_base_url=config.openrouter_base_url,
            openrouter_model=config.openrouter_model,
            ollama_base_url=config.settings.ollama_base_url,
            api_timeout=config.settings.api_timeout,
            reasoning_effort=config.settings.reasoning_effort,
        ),
        model=config.settings.ai_model,
        provider_name=config.settings.ai_provider,
        reasoning_effort=config.settings.reasoning_effort,
    )


@procrastinate_app.task(
    name=EVALUATE_RELEVANCE_TASK_NAME,
    queue=PROCESSING_QUEUE,
    retry=RELEVANCE_RETRY_STRATEGY,
    pass_context=True,
)
async def evaluate_relevance(
    context, source_item_revision_id: int, edition_id: int, policy_id: int
):
    """Decide one revision's relevance under the exact queued policy."""
    service = build_relevance_service()
    try:
        return await service.evaluate(source_item_revision_id, edition_id, policy_id)
    except ProviderUnavailableError:
        if context.job.attempts < 2:
            raise TransientProcessingError("relevance provider unavailable") from None
        return await service.persist_uncertain(
            source_item_revision_id, edition_id, policy_id, reason="provider_unavailable"
        )


async def backfill_relevance(
    edition_id: int,
    policy_id: int,
    after_revision_id: int | None = None,
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
) -> int:
    """Queue evaluate_relevance for revisions missing this exact policy's root.

    Bounded slice (batch_size + optional id cursor); safe to re-run since the
    gap query targets only revisions without a ROOT decision for the exact
    policy and duplicate jobs converge on the immutable decision.
    """
    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        policy = await RelevancePolicyVersionRepository().get(conn, policy_id)
        if policy is None or policy.edition_id != edition_id:
            raise ValueError(
                f"relevance policy {policy_id} does not belong to edition {edition_id}"
            )
        gap_ids = await _decisions().list_revision_ids_missing_root(
            conn,
            edition_id=edition_id,
            relevance_policy_id=policy_id,
            after_revision_id=after_revision_id,
            limit=batch_size,
        )
        for revision_id in gap_ids:
            await evaluate_relevance.configure(connection=conn).defer_async(
                source_item_revision_id=revision_id,
                edition_id=edition_id,
                policy_id=policy_id,
            )
    logger.info(
        "backfill_relevance queued %d revisions for edition=%s policy=%s",
        len(gap_ids),
        edition_id,
        policy_id,
    )
    return len(gap_ids)


def _decisions() -> EditionRelevanceDecisionRepository:
    return EditionRelevanceDecisionRepository()
