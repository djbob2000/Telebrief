"""Procrastinate task definitions and execution lock resolvers for Facebook (Plan 5 Task 3 & Task 5)."""

from __future__ import annotations

import datetime as dt
import logging

from src.config_loader import load_config
from src.domain.sources import Source
from src.ingestion.repository import IngestionRepository
from src.ingestion.service import IngestionService
from src.jobs.app import procrastinate_app
from src.providers.facebook.comments import (
    FacebookCommentCollector,
    FacebookCommentRefreshService,
)
from src.repositories.facebook import FacebookRepository
from src.runtime import get_runtime

logger = logging.getLogger(__name__)

REFRESH_COMMENTS_TASK_NAME = "refresh_facebook_comments"
DEEP_SWEEP_TASK_NAME = "dispatch_facebook_deep_sweep"


def resolve_facebook_execution_lock(source: Source) -> str:
    """Resolve Facebook profile lock for serialization across sources sharing the profile."""
    options = source.collector_options or {}
    auth_profile = options.get("auth_profile") or options.get("auth_profile_id") or "default"
    return f"facebook-auth-profile:{auth_profile}"


@procrastinate_app.task(
    name=REFRESH_COMMENTS_TASK_NAME,
    queue="enrichment",
    pass_context=False,
)
async def refresh_facebook_comments(
    source_item_revision_id: int, post_item_id: int, mode: str = "incremental"
) -> None:
    """Scan and persist comments and replies for a Facebook post."""
    runtime = get_runtime()
    fb_repo = FacebookRepository()
    ingestion_repo = IngestionRepository()
    ingestion_service = IngestionService(uow=runtime.uow, repo=ingestion_repo)

    cfg = load_config()
    comments_cfg = getattr(cfg.facebook, "comments", None)
    auth_root = getattr(cfg.facebook, "auth_root", "/var/lib/telebrief/auth")

    collector = FacebookCommentCollector(
        auth_root=auth_root,
        fb_repo=fb_repo,
        comments_config=comments_cfg,
    )
    service = FacebookCommentRefreshService(
        uow=runtime.uow,
        fb_repo=fb_repo,
        ingestion_service=ingestion_service,
        collector=collector,
    )

    async with runtime.uow.transaction() as conn:
        cursor = await conn.execute(
            """
            SELECT si.external_id,
                   s.id, s.platform, s.kind, s.external_id, s.url, s.name, s.role, s.enabled, s.collector_options, s.created_at, s.updated_at
            FROM source_items si
            JOIN sources s ON s.id = si.source_id
            WHERE si.id = %s
            """,
            (post_item_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            logger.warning("Post item %s not found for comment refresh", post_item_id)
            return

        post_external_id = row[0]
        source = Source.from_row(row[1:])

    # Perform browser collection outside domain transaction
    from src.providers.facebook.browser import FacebookBrowserSession

    prof_name = "default"
    if source.collector_options:
        prof_name = str(source.collector_options.get("auth_profile", "default"))

    async with runtime.uow.transaction() as conn:
        profile = await fb_repo.get_or_create_auth_profile(
            conn, name=prof_name, storage_ref=prof_name
        )

    if profile.status == "disabled":
        logger.warning(
            "Facebook auth profile %s is disabled; skipping comment refresh for post %s",
            prof_name,
            post_item_id,
        )
        return

    post_url = source.url
    if "/posts/" not in (source.url or "") and post_external_id:
        from src.providers.facebook.collector import canonicalize_post_url

        post_url = canonicalize_post_url(source.url or "", post_external_id.replace("post:", ""))

    try:
        async with FacebookBrowserSession(auth_root, profile, headless=True) as (_, page):
            if post_url:
                await page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
            batch = await collector.scan_post_with_page(
                source=source,
                post_item_id=post_item_id,
                post_external_id=post_external_id,
                page=page,
                limits=comments_cfg,
                mode=mode,
            )
    except Exception as e:
        logger.warning("Failed browser comment scan for post %s: %s", post_item_id, e)
        return

    # Atomic persistence
    await service.refresh_batch(
        source_id=source.id,
        post_item_id=post_item_id,
        batch=batch,
    )
    logger.info(
        "Refreshed comments for post %s: observed=%s", post_item_id, batch.total_comments_observed
    )


@procrastinate_app.periodic(cron="0 */6 * * *", periodic_id="facebook-deep-sweep")
@procrastinate_app.task(
    name=DEEP_SWEEP_TASK_NAME,
    queue="maintenance",
    queueing_lock="facebook-deep-sweep",
    pass_context=False,
)
async def dispatch_facebook_deep_sweep(timestamp: int) -> None:
    """Periodic maintenance sweep scheduling deep comment refresh on active posts."""
    from src.ingestion.enrichment import EnrichmentRequest, get_enrichment_dispatcher

    runtime = get_runtime()
    fb_repo = FacebookRepository()
    dispatcher = get_enrichment_dispatcher()
    scheduled_at = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)

    async with runtime.uow.transaction() as conn:
        candidates = await fb_repo.list_posts_due_for_deep_refresh(conn, scheduled_at=scheduled_at)

    for post in candidates:
        auth_prof = getattr(post, "auth_profile", "default")
        request = EnrichmentRequest(
            kind="facebook_comments",
            source_item_revision_id=post.current_revision_id,
            mode="deep",
            metadata={"post_item_id": post.source_item_id, "auth_profile": auth_prof},
        )
        await dispatcher.defer_without_domain_transaction(request, priority=10)
