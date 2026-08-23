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

from src.ai_providers import AIProvider, create_provider
from src.embedding_providers import GoogleGeminiEmbeddingProvider
from src.ingestion.repository import IngestionRepository
from src.jobs.app import procrastinate_app
from src.processing.claims import ClaimExtractionService
from src.processing.embeddings import EmbeddingService
from src.processing.places import (
    PlaceResolutionService,
    backfill_place_mentions_rows,
)
from src.processing.relevance import (
    ProviderUnavailableError,
    RelevanceService,
    TransientProcessingError,
)
from src.processing.story_matching import (
    StoryMatcher,
    StoryMatchingService,
    story_matching_execution_lock,
)
from src.processing.vision import (
    MetadataVisionProvider,
    VisionProviderUnavailable,
    VisionService,
    should_run_vision,
)
from src.repositories.claims import (
    ClaimExtractionPolicyRepository,
    ClaimExtractionRunRepository,
)
from src.repositories.embeddings import EmbeddingRepository
from src.repositories.places import (
    PlaceRepository,
    PlaceResolutionPolicyRepository,
)
from src.repositories.relevance import (
    EditionRelevanceDecisionRepository,
    RelevancePolicyVersionRepository,
    VisionAnalysisRunRepository,
    VisionPolicyRepository,
)
from src.repositories.story_candidates import (
    StoryMatchingPolicyVersionRepository,
    StoryMatchingRunRepository,
)
from src.runtime import get_runtime

logger = logging.getLogger(__name__)

EVALUATE_RELEVANCE_TASK_NAME = "evaluate_relevance"
ANALYZE_VISION_TASK_NAME = "analyze_vision"
EXTRACT_CLAIMS_TASK_NAME = "extract_claims"
EMBED_CLAIM_TASK_NAME = "embed_claim"
EMBED_STORY_REVISION_TASK_NAME = "embed_story_revision"
RESOLVE_PLACE_MENTION_TASK_NAME = "resolve_place_mention"
ASSESS_EVIDENCE_TASK_NAME = "assess_evidence"
MAYBE_VERIFY_EVIDENCE_TASK_NAME = "maybe_verify_evidence"
PROCESSING_QUEUE = "processing"

BACKFILL_BATCH_SIZE = 500

EVIDENCE_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=3,
    wait=30,
    linear_wait=60,
    retry_exceptions=(TransientProcessingError,),
)

# Total executions = initial attempt + 2 retries; waits mirror the collection
# strategy: 30s after the first failure, then 90s.
RELEVANCE_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=3,
    wait=30,
    linear_wait=60,
    retry_exceptions=(TransientProcessingError,),
)

VISION_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=3,
    wait=30,
    linear_wait=60,
    retry_exceptions=(TransientProcessingError,),
)

CLAIM_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=3,
    wait=30,
    linear_wait=60,
    retry_exceptions=(TransientProcessingError,),
)

STORY_MATCH_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=3,
    wait=30,
    linear_wait=60,
    retry_exceptions=(TransientProcessingError,),
)

PLACE_RESOLUTION_RETRY_STRATEGY = procrastinate.RetryStrategy(
    max_attempts=3,
    wait=30,
    linear_wait=60,
    retry_exceptions=(TransientProcessingError,),
)


def _create_ai_provider(config) -> AIProvider:
    """One AI cascade assembled from the real config for all processing tasks."""
    return create_provider(
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
    )


def build_relevance_service() -> RelevanceService:
    """Assemble the AI-backed relevance service from the real config."""
    from src.config_loader import load_config

    config = load_config()
    return RelevanceService(
        uow=get_runtime().uow,
        provider=_create_ai_provider(config),
        model=config.settings.ai_model,
        provider_name=config.settings.ai_provider,
        reasoning_effort=config.settings.reasoning_effort,
        vision_mode=config.settings.vision_mode,
        claims_enabled=True,
    )


def build_vision_service() -> VisionService:
    """Assemble the bounded vision service from the real config.

    The shipped provider is the offline descriptor classifier: Plan 2
    ingestion stores no pixel bytes, so until a media downloader plus a
    pixel-capable adapter exist, runs derive observations from asset
    descriptors (and concrete adapters may report assets unavailable).
    """
    from src.config_loader import load_config

    config = load_config()
    return VisionService(
        uow=get_runtime().uow,
        provider=MetadataVisionProvider(),
        relevance_service=build_relevance_service(),
        analyze_timeout=float(config.settings.api_timeout),
        claims_enabled=True,
    )


def build_claim_extraction_service() -> ClaimExtractionService:
    """Assemble the AI-backed claim extraction service from the real config."""
    from src.config_loader import load_config

    config = load_config()
    return ClaimExtractionService(
        uow=get_runtime().uow,
        provider=_create_ai_provider(config),
        model=config.settings.ai_model,
        provider_name=config.settings.ai_provider,
        reasoning_effort=config.settings.reasoning_effort,
        # Frozen into each embed_claim defer so retries keep the queued space.
        embedding_config=config.embedding,
        # T8: materialize mentions/entities and defer resolve_place_mention
        # per new mention on the success transaction.
        place_resolution_handoff=True,
    )


def build_embedding_service() -> EmbeddingService:
    """Assemble the semantic embedding service from the real config.

    The story-matching handoff is enabled: every visible claim embedding
    defers ``match_claim`` atomically with its policy resolution."""
    from src.config_loader import load_config

    config = load_config()
    embedding_config = config.embedding
    provider = GoogleGeminiEmbeddingProvider(
        api_key=embedding_config.api_key or config.gemini_api_key,
        logger=logger,
        timeout=embedding_config.timeout,
    )
    return EmbeddingService(uow=get_runtime().uow, provider=provider, matching_handoff=True)


def build_story_matching_service() -> StoryMatchingService:
    """Assemble the AI-backed story matching orchestrator from the config."""
    from src.config_loader import load_config

    config = load_config()
    matcher = StoryMatcher(
        provider=_create_ai_provider(config),
        model=config.settings.ai_model,
        provider_name=config.settings.ai_provider,
        reasoning_effort=config.settings.reasoning_effort,
    )
    return StoryMatchingService(uow=get_runtime().uow, matcher=matcher)


def build_place_resolution_service() -> PlaceResolutionService:
    """Assemble the place resolution service from the real config.

    The shipped resolver is deterministic (seeded alias lookup); the optional
    LLM assist stays unwired until its prompt/config identity joins the
    policy hash, so no AI provider is constructed here yet.
    """
    return PlaceResolutionService(uow=get_runtime().uow)


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


@procrastinate_app.task(
    name=ANALYZE_VISION_TASK_NAME,
    queue=PROCESSING_QUEUE,
    retry=VISION_RETRY_STRATEGY,
    pass_context=True,
)
async def analyze_vision(
    context, source_item_revision_id: int, relevance_decision_id: int, policy_id: int
):
    """Run one bounded vision analysis and land its fail-open handoff.

    Same retry math as relevance: only VisionProviderUnavailable (mapped to
    TransientProcessingError) retries; the final failed attempt completes the
    run as unavailable and returns the handoff — ready_for_claims stays True
    for text-supported relevant decisions, while needs_media items without
    observations remain unresolved (never a false irrelevant).
    """
    service = build_vision_service()
    try:
        return await service.run(source_item_revision_id, relevance_decision_id, policy_id)
    except VisionProviderUnavailable:
        if context.job.attempts < 2:
            raise TransientProcessingError("vision provider unavailable") from None
        return await service.finalize_provider_failure(
            source_item_revision_id, relevance_decision_id, policy_id
        )


@procrastinate_app.task(
    name=EXTRACT_CLAIMS_TASK_NAME,
    queue=PROCESSING_QUEUE,
    retry=CLAIM_RETRY_STRATEGY,
    pass_context=True,
)
async def extract_claims(
    context,
    source_item_revision_id: int,
    edition_id: int,
    relevance_decision_id: int,
    policy_id: int,
    vision_run_id: int | None = None,
):
    """Extract immutable claims for one relevant decision under the exact policy.

    Same retry math as relevance/vision (Plan 2 lesson): max_attempts=3 counts
    TOTAL executions; only ProviderUnavailableError maps to the retryable
    TransientProcessingError. On the final failed attempt the run completes as
    ``failed(provider_unavailable)`` and the task returns successfully —
    fail-open: the item stays relevant-but-claimless, bounded backfill retries
    it later, and the pipeline never blocks on the AI. Duplicate or retried
    executions converge on the single canonical succeeded run.
    """
    service = build_claim_extraction_service()
    try:
        return await service.extract(
            source_item_revision_id,
            edition_id,
            relevance_decision_id,
            policy_id,
            vision_run_id,
        )
    except ProviderUnavailableError:
        if context.job.attempts < 2:
            raise TransientProcessingError("claim extraction provider unavailable") from None
        return await service.finalize_provider_failure(
            source_item_revision_id,
            edition_id,
            relevance_decision_id,
            policy_id,
            vision_run_id,
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


async def backfill_vision(
    edition_id: int,
    vision_policy_id: int,
    after_decision_id: int | None = None,
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
) -> int:
    """Queue analyze_vision for decisions that still owe this exact policy a run.

    Bounded slice (batch_size + optional id cursor); safe to re-run since the
    gap query targets only latest-per-revision decisions without a run for the
    exact vision policy, and duplicate jobs converge on succeeded-run replay.
    ``needs_media`` always qualifies (with media); ``relevant`` joins only in
    full mode, and should_run_vision re-gates each candidate before deferral.
    """
    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        policy = await VisionPolicyRepository().get(conn, vision_policy_id)
        if policy is None or policy.edition_id != edition_id:
            raise ValueError(
                f"vision policy {vision_policy_id} does not belong to edition {edition_id}"
            )
        statuses = ["needs_media"] + (["relevant"] if policy.mode == "full" else [])
        candidates = await VisionAnalysisRunRepository().list_decisions_missing_run(
            conn,
            edition_id=edition_id,
            policy_id=vision_policy_id,
            statuses=statuses,
            after_decision_id=after_decision_id,
            limit=batch_size,
        )
        ingestion_repo = IngestionRepository()
        deferred = 0
        for decision in candidates:
            revision = await ingestion_repo.get_revision(conn, decision.source_item_revision_id)
            assets = await ingestion_repo.list_asset_summaries(
                conn, decision.source_item_revision_id
            )
            if revision is None or not should_run_vision(
                decision, revision, assets, mode=policy.mode
            ):
                continue
            await analyze_vision.configure(connection=conn).defer_async(
                source_item_revision_id=decision.source_item_revision_id,
                relevance_decision_id=decision.id,
                policy_id=vision_policy_id,
            )
            deferred += 1
    logger.info(
        "backfill_vision queued %d decisions for edition=%s vision_policy=%s mode=%s",
        deferred,
        edition_id,
        vision_policy_id,
        policy.mode,
    )
    return deferred


def _decisions() -> EditionRelevanceDecisionRepository:
    return EditionRelevanceDecisionRepository()


async def backfill_claims(
    edition_id: int,
    extraction_policy_id: int,
    after_decision_id: int | None = None,
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
    vision_mode: str | None = None,
) -> int:
    """Queue extract_claims for relevant decisions still owing this policy a
    SUCCESSFUL extraction.

    Bounded slice (batch_size + optional exclusive id cursor); safe to re-run:
    duplicate jobs converge on the canonical succeeded run. Only a SUCCEEDED
    run satisfies the debt — failed/unavailable prior runs re-queue so the
    fail-open boundary is always caught up later, and stale running rows do
    not orphan items either. When ``vision_mode == "full"``, candidates with
    media are skipped so bounded vision enrichment routes them first (same
    handoff rules as the live pipeline); pass the configured mode explicitly
    when invoking from full-mode deployments.
    """
    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        policy = await ClaimExtractionPolicyRepository().get(conn, extraction_policy_id)
        if policy is None or policy.edition_id != edition_id:
            raise ValueError(
                f"claim extraction policy {extraction_policy_id} does not belong "
                f"to edition {edition_id}"
            )
        candidates = await ClaimExtractionRunRepository().list_decisions_missing_success(
            conn,
            edition_id=edition_id,
            extraction_policy_id=extraction_policy_id,
            after_decision_id=after_decision_id,
            limit=batch_size,
        )
        ingestion_repo = IngestionRepository()
        deferred = 0
        for decision in candidates:
            if vision_mode == "full":
                assets = await ingestion_repo.list_asset_summaries(
                    conn, decision.source_item_revision_id
                )
                if assets:
                    continue
            await extract_claims.configure(connection=conn).defer_async(
                source_item_revision_id=decision.source_item_revision_id,
                edition_id=decision.edition_id,
                relevance_decision_id=decision.id,
                policy_id=extraction_policy_id,
            )
            deferred += 1
    logger.info(
        "backfill_claims queued %d decisions for edition=%s policy=%s vision_mode=%s",
        deferred,
        edition_id,
        extraction_policy_id,
        vision_mode or "default",
    )
    return deferred


@procrastinate_app.task(queue=PROCESSING_QUEUE)
async def embed_claim(claim_id: int, model: str, dimensions: int) -> None:
    """Embed one whole claim into the exact queued model/dimension space.

    ``model`` and ``dimensions`` are frozen task arguments copied from the
    embedding config at defer time, so a retried execution keeps writing into
    the space it was queued for even after a config change. Duplicate
    executions converge on the one immutable row per
    (claim, model, dimensions, purpose, content_hash).
    """
    await build_embedding_service().embed_claim(claim_id, model=model, dimensions=dimensions)


@procrastinate_app.task(queue=PROCESSING_QUEUE)
async def embed_story_revision(story_revision_id: int, model: str, dimensions: int) -> None:
    """Embed one whole story revision (its compact semantic_text)."""
    await build_embedding_service().embed_story_revision(
        story_revision_id, model=model, dimensions=dimensions
    )


@procrastinate_app.task(
    queue=PROCESSING_QUEUE,
    retry=STORY_MATCH_RETRY_STRATEGY,
    pass_context=True,
)
async def match_claim(context, claim_id: int, policy_id: int, claim_embedding_id: int):
    """Match one claim (via its frozen embedding) into persistent stories.

    The EXACT policy id and embedding id resolved at defer time are queued
    as task arguments, so a retried execution keeps its original policy and
    vector space even when newer versions become current. The per-edition
    execution lock (story-matching-edition:<edition_id>, set at defer time)
    serializes matching within one edition; duplicate executions converge on
    the single canonical succeeded run via uq_story_match_success.

    Retry math mirrors relevance/vision/claims: only the mapped provider
    outage retries, and the final failed attempt marks the run
    ``failed(provider_unavailable)`` through the guarded write that never
    demotes a concurrently succeeded winner.
    """
    service = build_story_matching_service()
    try:
        return await service.run(claim_id, policy_id, claim_embedding_id)
    except ProviderUnavailableError:
        if context.job.attempts < 2:
            raise TransientProcessingError("story matching provider unavailable") from None
        return await service.finalize_provider_failure(claim_id, policy_id)


@procrastinate_app.task(
    name=RESOLVE_PLACE_MENTION_TASK_NAME,
    queue=PROCESSING_QUEUE,
    retry=PLACE_RESOLUTION_RETRY_STRATEGY,
    pass_context=True,
)
async def resolve_place_mention(context, mention_id: int, policy_id: int):
    """Resolve one immutable claim place mention under the exact queued policy.

    The EXACT policy id resolved at defer time is a frozen task argument, so
    a retried execution keeps its original prompt/config identity. A NULL
    ``place_id`` with status 'unresolved' is a COMPLETED outcome that
    satisfies the matching barrier — unresolved geography never blocks.

    Retry math mirrors relevance/vision/claims/matching: max_attempts=3
    counts TOTAL executions and only ProviderUnavailableError maps to the
    retryable TransientProcessingError; on the final failed attempt the run
    completes failed(provider_unavailable) through the guarded write (never
    demoting a succeeded winner) and the task returns successfully.
    Duplicate executions replay the canonical result without new rows.
    """
    service = build_place_resolution_service()
    try:
        return await service.resolve_mention(mention_id, policy_id)
    except ProviderUnavailableError:
        if context.job.attempts < 2:
            raise TransientProcessingError("place resolution provider unavailable") from None
        return await service.finalize_provider_failure(mention_id, policy_id)


async def backfill_story_matching(
    edition_id: int,
    policy_id: int,
    after_claim_embedding_id: int | None = None,
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
) -> int:
    """Queue match_claim for compatible embeddings still owing this exact
    policy a run.

    Covers ClaimEmbeddings persisted before the live handoff existed (Task 5
    without Task 7) plus any window where a run failed. Bounded slice
    (batch_size + optional exclusive id cursor); safe to re-run since runs
    with status succeeded/running/stale count as coverage — failed runs keep
    their debt visible until a successful matching lands. Duplicate queued
    jobs converge on the canonical succeeded run.
    """
    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        policy = await StoryMatchingPolicyVersionRepository().get(conn, policy_id)
        if policy is None or policy.edition_id != edition_id:
            raise ValueError(
                f"story matching policy {policy_id} does not belong to edition {edition_id}"
            )
        gaps = await StoryMatchingRunRepository().list_claim_embedding_gaps(
            conn,
            edition_id=edition_id,
            policy_id=policy.id,
            model=policy.embedding_model,
            dimensions=policy.embedding_dimensions,
            after_embedding_id=after_claim_embedding_id,
            limit=batch_size,
        )
        for gap in gaps:
            await match_claim.configure(
                connection=conn,
                lock=story_matching_execution_lock(edition_id),
            ).defer_async(
                claim_id=gap.claim_id,
                policy_id=policy.id,
                claim_embedding_id=gap.embedding_id,
            )
    logger.info(
        "backfill_story_matching queued %d claims edition=%s policy=%s",
        len(gaps),
        edition_id,
        policy_id,
    )
    return len(gaps)


async def backfill_claim_embeddings(
    model: str,
    dimensions: int,
    after_claim_id: int | None = None,
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
) -> int:
    """Queue embed_claim for claims missing that exact model/dimension vector.

    Covers claims created while Task 4 ran without Task 5 and any window
    after an embedding-config change. Bounded slice (batch_size + optional
    exclusive id cursor); safe to re-run since duplicate jobs converge on the
    immutable row. Old embedding rows are never updated — new spaces get new
    rows.
    """
    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        gap_ids = await EmbeddingRepository().list_claim_ids_missing_embedding(
            conn,
            model=model,
            dimensions=dimensions,
            after_claim_id=after_claim_id,
            limit=batch_size,
        )
        for claim_id in gap_ids:
            await embed_claim.configure(connection=conn).defer_async(
                claim_id=claim_id, model=model, dimensions=dimensions
            )
    logger.info(
        "backfill_claim_embeddings queued %d claims model=%s dimensions=%d",
        len(gap_ids),
        model,
        dimensions,
    )
    return len(gap_ids)


async def backfill_story_revision_embeddings(
    model: str,
    dimensions: int,
    after_story_revision_id: int | None = None,
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
) -> int:
    """Queue embed_story_revision for revisions missing the exact vector."""
    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        gap_ids = await EmbeddingRepository().list_story_revision_ids_missing_embedding(
            conn,
            model=model,
            dimensions=dimensions,
            after_story_revision_id=after_story_revision_id,
            limit=batch_size,
        )
        for revision_id in gap_ids:
            await embed_story_revision.configure(connection=conn).defer_async(
                story_revision_id=revision_id, model=model, dimensions=dimensions
            )
    logger.info(
        "backfill_story_revision_embeddings queued %d revisions model=%s dimensions=%d",
        len(gap_ids),
        model,
        dimensions,
    )
    return len(gap_ids)


async def backfill_place_mentions(*, batch_size: int = BACKFILL_BATCH_SIZE) -> int:
    """Migrate legacy T4-staging metadata mentions/entities into rows.

    Bounded slice; idempotent (NOT EXISTS per place/claim key) and the
    claims.metadata staging record is never modified. Duplicate rows are
    impossible on re-run, so at-least-once scheduling is safe.
    """
    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        created = await backfill_place_mentions_rows(conn, batch_size=batch_size)
    logger.info("backfill_place_mentions created %d evidence rows", created)
    return created


async def backfill_place_resolutions(
    edition_id: int,
    policy_id: int,
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
) -> int:
    """Queue resolve_place_mention for mentions still owing this exact policy
    a completed result.

    Covers ClaimPlaceMentions persisted before the live handoff existed
    (Task 4 without Task 8) plus any window where a run failed — failed runs
    never occupy the canonical slot. Bounded slice; safe to re-run since
    resolved/unresolved results count as coverage and duplicate jobs converge
    on the canonical winner.
    """
    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        policy = await PlaceResolutionPolicyRepository().get(conn, policy_id)
        if policy is None or policy.edition_id != edition_id:
            raise ValueError(
                f"place resolution policy {policy_id} does not belong to edition {edition_id}"
            )
        gaps = await PlaceRepository().list_mentions_missing_result(
            conn, policy_id=policy.id, limit=batch_size
        )
        for mention in gaps:
            await resolve_place_mention.configure(connection=conn).defer_async(
                mention_id=mention.id, policy_id=policy.id
            )
    logger.info(
        "backfill_place_resolutions queued %d mentions edition=%s policy=%s",
        len(gaps),
        edition_id,
        policy.id,
    )
    return len(gaps)


@procrastinate_app.task(
    name=ASSESS_EVIDENCE_TASK_NAME,
    queue=PROCESSING_QUEUE,
    retry=EVIDENCE_RETRY_STRATEGY,
    pass_context=True,
)
async def assess_evidence(
    context,
    story_id: int,
    story_revision_id: int,
    policy_id: int,
) -> None:
    """Assess evidence clustering for a story revision under the queued policy."""
    from src.processing.evidence import EvidenceAssessmentService

    runtime = get_runtime()
    service = EvidenceAssessmentService(uow=runtime.uow)
    await service.assess(
        story_id=story_id,
        story_revision_id=story_revision_id,
        policy_id=policy_id,
    )


@procrastinate_app.task(
    name=MAYBE_VERIFY_EVIDENCE_TASK_NAME,
    queue=PROCESSING_QUEUE,
    retry=EVIDENCE_RETRY_STRATEGY,
    pass_context=True,
)
async def maybe_verify_evidence(
    context,
    evidence_assessment_run_id: int,
) -> None:
    """Optional lightweight verification for an evidence assessment run."""
    from src.processing.verification import VerificationService
    from src.repositories.evidence import (
        EvidenceAssessmentRunRepository,
        EvidenceClusterRepository,
    )

    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        run = await EvidenceAssessmentRunRepository().get_by_id(conn, evidence_assessment_run_id)
        if run is None or run.status != "succeeded":
            return
        clusters = await EvidenceClusterRepository().list_clusters_for_run(conn, run.id)
    if not clusters:
        return
    service = VerificationService(uow=runtime.uow)
    await service.assess(run=run, clusters=clusters)


async def backfill_evidence_assessments(
    edition_id: int,
    policy_id: int,
    *,
    batch_size: int = BACKFILL_BATCH_SIZE,
) -> int:
    """Queue assess_evidence for active/reopened stories lacking a succeeded assessment under this policy."""
    from src.repositories.evidence import EvidencePolicyRepository

    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        policy = await EvidencePolicyRepository().get_by_id(conn, policy_id)
        if policy is None or policy.edition_id != edition_id:
            raise ValueError(f"evidence policy {policy_id} does not belong to edition {edition_id}")
        cursor = await conn.execute(
            """
            SELECT s.id, s.current_revision_id
            FROM stories s
            WHERE s.edition_id = %s
              AND s.lifecycle_state IN ('active', 'reopened')
              AND s.current_revision_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM evidence_assessment_runs ear
                  WHERE ear.story_revision_id = s.current_revision_id
                    AND ear.policy_id = %s
                    AND ear.status = 'succeeded'
              )
            LIMIT %s
            """,
            (edition_id, policy_id, batch_size),
        )
        stories_to_queue = await cursor.fetchall()
        for story_id, revision_id in stories_to_queue:
            await assess_evidence.configure(connection=conn).defer_async(
                story_id=story_id,
                story_revision_id=revision_id,
                policy_id=policy.id,
            )
    logger.info(
        "backfill_evidence_assessments queued %d stories edition=%s policy=%s",
        len(stories_to_queue),
        edition_id,
        policy.id,
    )
    return len(stories_to_queue)
