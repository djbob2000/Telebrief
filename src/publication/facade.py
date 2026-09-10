"""Publication request facade for the unified durable publication intent."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from src.config_loader import Config

logger = logging.getLogger(__name__)

DEFAULT_EDITION_SLUG = "berdyansk"


class PublicationConfigError(RuntimeError):
    """Raised when publication is requested without the persistent pipeline."""


@dataclass(frozen=True)
class PublicationRequestResult:
    """Outcome of a request: the durable intent accepted by the orchestrator."""

    intent_id: int
    request_key: str
    edition_slug: str
    publication_type: str
    snapshot_at: dt.datetime
    readiness_status: str

    @property
    def run_id(self) -> int:
        """Compatibility alias; PublicationRun creation happens downstream."""
        return self.intent_id


@dataclass(frozen=True)
class PublicationPreviewResult:
    """Outcome of a publication preview generated without delivery side effects."""

    run_id: int
    publication_id: int
    title: str
    lead: str
    body: str
    publication_type: str
    snapshot_at: dt.datetime


def validate_publication_config(config: Config) -> None:
    """Fail fast unless the normalized database path is fully enabled.

    The publication facade has no live-provider fallback: requesting a
    publication with ``database.enabled=false`` or
    ``settings.persistent_ingestion=false`` is a configuration error, not a
    switch between collection modes.
    """
    if not config.database.enabled:
        raise PublicationConfigError(
            "publication requires database.enabled=true in config.yaml; "
            "disabling the database is not a valid production configuration"
        )
    if not config.settings.persistent_ingestion:
        raise PublicationConfigError(
            "publication requires settings.persistent_ingestion=true in config.yaml; "
            "digests and articles are generated only from persisted source history"
        )


async def request_publication(
    publication_type: str,
    edition_slug: str = DEFAULT_EDITION_SLUG,
    *,
    snapshot_at: dt.datetime | None = None,
    lookback_hours: int | None = None,
    request_key: str | None = None,
    requested_by_user_id: int | None = None,
    dry_run: bool = False,
    config: Config | None = None,
) -> PublicationRequestResult:
    """Request one durable publication intent over persisted source history.

    Args:
        publication_type: e.g. ``"digest_grouped"`` or ``"daily_article"``.
        edition_slug: target edition slug (default ``berdyansk``).
        snapshot_at: target time override; defaults to now (UTC).
        lookback_hours: lookback hours override for publication eligibility.
        request_key: deterministic key; on-demand callers get a fresh UUID.
        dry_run: accepted for call-site compatibility; the durable pipeline
            always persists its outputs, so dry-run previews are handled by
            the caller before requesting a publication.
        config: pre-loaded configuration (loaded from disk when omitted).

    Returns:
        PublicationRequestResult describing the created run.

    Raises:
        PublicationConfigError: configuration does not enable the pipeline.
        RuntimeError: no runtime installed (call outside app/worker process).
        ValueError: unknown edition slug or invalid publication type.
    """
    del dry_run  # see docstring: previews never reach the durable pipeline
    if config is None:
        from src.config_loader import load_config

        config = load_config()
    validate_publication_config(config)

    from src.publication.orchestrator import PublicationOrchestrator
    from src.runtime import get_runtime

    runtime = get_runtime()
    snap = snapshot_at or dt.datetime.now(dt.timezone.utc)
    key = request_key or f"on-demand:{edition_slug}:{publication_type}:{uuid.uuid4().hex}"

    result = await PublicationOrchestrator(uow=runtime.uow, config=config).request(
        edition_slug=edition_slug,
        publication_type=publication_type,
        trigger="manual",
        target_at=snap,
        requested_by_user_id=requested_by_user_id,
        request_key=key,
        lookback_hours=lookback_hours,
    )
    logger.info(
        "requested %s publication intent %s (edition=%s, target_at=%s, status=%s)",
        publication_type,
        result.intent_id,
        edition_slug,
        snap.isoformat(),
        result.readiness_status,
    )
    return PublicationRequestResult(
        intent_id=result.intent_id,
        request_key=result.request_key,
        edition_slug=result.edition_slug,
        publication_type=result.publication_type,
        snapshot_at=result.target_at,
        readiness_status=result.readiness_status,
    )


async def build_publication_preview(
    publication_type: str,
    edition_slug: str = DEFAULT_EDITION_SLUG,
    *,
    snapshot_at: dt.datetime | None = None,
    lookback_hours: int | None = None,
    config: Config | None = None,
) -> PublicationPreviewResult:
    """Generate a full publication preview in-process without delivery side effects.

    Executes the canonical production publication pipeline: drain candidate
    knowledge -> freeze a PublicationRun -> seal_candidates -> select ->
    generate -> Publication, with explicit flags to suppress queueing
    selection, generation, and delivery jobs.

    Raises:
        ArticlePublicationRejected: the one-call Event-First article writer did not
            produce a publication-safe draft; no preview publication exists.
    """
    if config is None:
        from src.config_loader import load_config

        config = load_config()
    validate_publication_config(config)

    from src.publication.generation import PublicationGenerationService
    from src.publication.policies import PublicationPolicyService
    from src.publication.selection import EditorialSelectionService
    from src.publication.snapshot import IncompleteTriageError, PublicationSnapshotService
    from src.repositories.editions import EditionRepository
    from src.runtime import get_runtime

    runtime = get_runtime()
    snap = snapshot_at or dt.datetime.now(dt.timezone.utc)
    key = f"preview:{edition_slug}:{publication_type}:{uuid.uuid4().hex}"

    async with runtime.uow.transaction() as conn:
        edition = await EditionRepository().get_by_slug(conn, edition_slug)
        if edition is None:
            raise ValueError(f"edition slug {edition_slug!r} not found")
        policy_set = await PublicationPolicyService().ensure_current(
            conn,
            edition_id=edition.id,
            publication_type=publication_type,
            config=config,
            lookback_hours_override=lookback_hours,
        )

    service = PublicationSnapshotService(uow=runtime.uow)
    knowledge_snapshot_at: dt.datetime | None = None
    remaining = 1
    for _ in range(3):
        candidate_at = dt.datetime.now(dt.timezone.utc)
        await service.drain_authority_gap(
            edition_id=edition.id,
            source_cutoff_at=snap,
            snapshot_at=candidate_at,
            eligibility_policy_id=policy_set.eligibility_policy_id,
            max_rounds=1,
        )
        knowledge_snapshot_at = dt.datetime.now(dt.timezone.utc)
        remaining = await service.count_authority_gap(
            edition_id=edition.id,
            source_cutoff_at=snap,
            snapshot_at=knowledge_snapshot_at,
            eligibility_policy_id=policy_set.eligibility_policy_id,
        )
        if remaining == 0:
            break
    if knowledge_snapshot_at is None or remaining != 0:
        raise IncompleteTriageError("authority gap did not converge for preview")

    run = await service.create_run(
        edition_id=edition.id,
        publication_type=publication_type,
        source_cutoff_at=snap,
        snapshot_at=knowledge_snapshot_at,
        request_key=key,
        policy_ids=policy_set,
        metadata={"preview": True},
    )

    async with runtime.uow.transaction() as conn:
        await service.seal_candidates(run.id, conn=conn)

    selector = EditorialSelectionService(uow=runtime.uow, config=config)
    await selector.select(run.id, defer_generation=False)

    generator = PublicationGenerationService(uow=runtime.uow, config=config)
    pub = await generator.generate(
        run.id,
        defer_delivery=False,
        publication_metadata={"preview": True, "preview_mode": "no_delivery"},
    )

    logger.info(
        "generated preview %s publication %s (run=%s, edition=%s)",
        publication_type,
        pub.id,
        run.id,
        edition_slug,
    )
    return PublicationPreviewResult(
        run_id=run.id,
        publication_id=pub.id,
        title=pub.title,
        lead=pub.lead or "",
        body=pub.body,
        publication_type=pub.publication_type,
        snapshot_at=knowledge_snapshot_at,
    )
