"""Publication request facade: the single production entry for digests/articles.

Every caller (scheduler dispatcher, bot commands, MCP, CLI) goes through
:func:`request_publication`, which validates the fail-fast configuration
invariant, creates (or gets) a deterministic :class:`PublicationRun`, seals
the candidate snapshot, and defers the durable selection -> generation ->
delivery chain onto the Procrastinate ``publication`` queue. The facade never
collects from providers, never generates content inline, and never owns a
clock (Plan 4 Task 8).
"""

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
    """Outcome of a publication request: the durable run that was enqueued."""

    run_id: int
    request_key: str
    edition_slug: str
    publication_type: str
    snapshot_at: dt.datetime


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
    dry_run: bool = False,
    config: Config | None = None,
) -> PublicationRequestResult:
    """Request one durable publication run over current frozen knowledge.

    Args:
        publication_type: e.g. ``"digest_grouped"`` or ``"daily_article"``.
        edition_slug: target edition slug (default ``berdyansk``).
        snapshot_at: knowledge cutoff; defaults to now (UTC). Scheduled
            orchestration always passes the scheduled slot so repeated
            dispatcher executions dedupe on the derived request key.
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

    from src.jobs.publication import select_stories_for_publication
    from src.publication.snapshot import PublicationSnapshotService
    from src.repositories.editions import EditionRepository
    from src.runtime import get_runtime

    runtime = get_runtime()
    snap = snapshot_at or dt.datetime.now(dt.timezone.utc)
    key = request_key or f"on-demand:{edition_slug}:{publication_type}:{uuid.uuid4().hex}"

    async with runtime.uow.transaction() as conn:
        edition = await EditionRepository().get_by_slug(conn, edition_slug)
        if edition is None:
            raise ValueError(f"edition slug {edition_slug!r} not found")

    service = PublicationSnapshotService(uow=runtime.uow)
    run = await service.create_run(
        edition_id=edition.id,
        publication_type=publication_type,
        snapshot_at=snap,
        request_key=key,
        config=config,
        lookback_hours_override=lookback_hours,
    )
    # Seal and defer share one transaction: a failed defer rolls the sealing
    # back instead of stranding the run in candidates_sealed forever.
    async with runtime.uow.transaction() as conn:
        await service.seal_candidates(run.id, conn=conn)
        try:
            await select_stories_for_publication.configure(connection=conn).defer_async(
                run_id=run.id
            )
        except Exception as err:
            logger.error("could not defer selection for run %s (%s): %s", run.id, key, err)
            raise
    logger.info(
        "requested %s publication run %s (edition=%s, snapshot_at=%s)",
        publication_type,
        run.id,
        edition_slug,
        snap.isoformat(),
    )
    return PublicationRequestResult(
        run_id=run.id,
        request_key=key,
        edition_slug=edition_slug,
        publication_type=publication_type,
        snapshot_at=snap,
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

    Executes the canonical production publication pipeline:
    create_run -> seal_candidates -> select -> generate -> Publication,
    with explicit flags to suppress queueing selection, generation, and delivery jobs.
    """
    if config is None:
        from src.config_loader import load_config

        config = load_config()
    validate_publication_config(config)

    from src.publication.generation import PublicationGenerationService
    from src.publication.selection import EditorialSelectionService
    from src.publication.snapshot import PublicationSnapshotService
    from src.repositories.editions import EditionRepository
    from src.runtime import get_runtime

    runtime = get_runtime()
    snap = snapshot_at or dt.datetime.now(dt.timezone.utc)
    key = f"preview:{edition_slug}:{publication_type}:{uuid.uuid4().hex}"

    async with runtime.uow.transaction() as conn:
        edition = await EditionRepository().get_by_slug(conn, edition_slug)
        if edition is None:
            raise ValueError(f"edition slug {edition_slug!r} not found")

    service = PublicationSnapshotService(uow=runtime.uow)
    run = await service.create_run(
        edition_id=edition.id,
        publication_type=publication_type,
        snapshot_at=snap,
        request_key=key,
        config=config,
        lookback_hours_override=lookback_hours,
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
        publication_metadata={"preview": True},
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
        snapshot_at=snap,
    )
