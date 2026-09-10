"""Bounded background and publication authority job wrappers."""

from __future__ import annotations

import datetime as dt
import logging

from src.config_loader import load_config
from src.jobs.app import procrastinate_app
from src.processing.edition_scope import SCOPE_VERSION, resolve_edition_scope, scope_config_hash
from src.processing.event_authority import EventAuthorityService
from src.processing.event_triage import TRIAGE_VERSION
from src.repositories.event_authority import EventAuthorityRepository
from src.runtime import get_runtime

logger = logging.getLogger(__name__)
PUBLICATION_AUTHORITY_PRIORITY = 100
BACKGROUND_AUTHORITY_PRIORITY = -10


async def _load_background_targets(edition_id: int, *, limit: int):
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    async with runtime.uow.transaction() as conn:
        _slug, scope_config = await resolve_edition_scope(conn, config, edition_id)
        return await EventAuthorityRepository().list_background_targets(
            conn,
            edition_id=edition_id,
            triage_version=TRIAGE_VERSION,
            scope_version=SCOPE_VERSION,
            scope_config_hash=scope_config_hash(scope_config),
            now=dt.datetime.now(dt.timezone.utc),
            limit=limit,
        )


@procrastinate_app.task(
    queue="processing",
    name="process_background_authority_batch",
    lock="authority-background:{edition_id}",
)
async def process_background_authority_batch(edition_id: int) -> None:
    runtime = get_runtime()
    config = getattr(runtime, "config", None) or load_config()
    targets = await _load_background_targets(
        edition_id, limit=config.settings.event_pipeline.triage_batch_size
    )
    if targets:
        result = await EventAuthorityService.from_runtime(runtime, config).process_batch(
            targets, mode="background"
        )
        from src.jobs.event_enrichment import defer_event_enrichment

        for target in result.enrichment_targets:
            await defer_event_enrichment(target.story_id, target.assignment_id)
    await request_background_authority_dispatch(edition_id)


async def request_background_authority_dispatch(edition_id: int) -> None:
    from procrastinate.exceptions import AlreadyEnqueued

    try:
        await dispatch_background_authority.configure(
            priority=BACKGROUND_AUTHORITY_PRIORITY,
            queueing_lock=f"authority-background-dispatch:{edition_id}",
        ).defer_async(edition_id=edition_id)
    except AlreadyEnqueued:
        return


@procrastinate_app.task(
    queue="maintenance",
    name="dispatch_background_authority",
    lock="authority-background-dispatch:{edition_id}",
)
async def dispatch_background_authority(edition_id: int) -> None:
    targets = await _load_background_targets(edition_id, limit=1)
    if not targets:
        return
    from procrastinate.exceptions import AlreadyEnqueued

    try:
        await process_background_authority_batch.configure(
            priority=BACKGROUND_AUTHORITY_PRIORITY,
            queueing_lock=f"authority-background:{edition_id}",
        ).defer_async(edition_id=edition_id)
    except AlreadyEnqueued:
        return


@procrastinate_app.periodic(
    cron="*/5 * * * *", periodic_id="periodic-background-authority-dispatch"
)
@procrastinate_app.task(
    queue="maintenance",
    name="periodic_background_authority_dispatch",
    queueing_lock="periodic-background-authority-dispatch",
)
async def periodic_background_authority_dispatch(timestamp: int) -> None:
    del timestamp
    runtime = get_runtime()
    async with runtime.uow.transaction() as conn:
        cursor = await conn.execute("SELECT id FROM editions ORDER BY id")
        edition_ids = [int(row[0]) for row in await cursor.fetchall()]
    for edition_id in edition_ids:
        await request_background_authority_dispatch(edition_id)
