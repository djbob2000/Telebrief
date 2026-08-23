"""Completeness-oriented Facebook comments and replies collection (Plan 5 Task 4)."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from src.config_loader import FacebookCommentsConfig
from src.db.uow import DatabaseUnitOfWork
from src.domain.sources import Source
from src.ingestion.models import (
    CollectionBatch,
    CollectionOutcome,
    CollectionTrigger,
    JSONValue,
    ObservedAsset,
    ObservedItem,
    ObservedStateEvent,
)
from src.ingestion.service import IngestionService
from src.providers.facebook.collector import (
    PLATFORM_FACEBOOK,
    extract_post_id_from_url,
)
from src.repositories.facebook import FacebookRepository

logger = logging.getLogger(__name__)

KIND_FACEBOOK_COMMENT = "facebook_comment"


@dataclass
class CommentCollectionBatch:
    """The result of a comments/replies collection pass on a single post."""

    source_id: int
    post_item_id: int
    requested_sort: str = "all"
    effective_sort: str = "all"
    completeness: str = "unknown"  # "complete" | "partial" | "unknown"
    stop_reason: str = (
        "exhausted"  # "exhausted" | "max_comments" | "max_pages" | "timeout" | "platform_behavior"
    )
    total_comments_observed: int = 0
    oldest_comment_published_at: dt.datetime | None = None
    newest_comment_published_at: dt.datetime | None = None
    items: list[ObservedItem] = field(default_factory=list)
    assets: list[ObservedAsset] = field(default_factory=list)
    state_events: list[ObservedStateEvent] = field(default_factory=list)
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    completed_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))

    def as_collection_batch(self) -> CollectionBatch:
        return CollectionBatch(
            outcome=CollectionOutcome.SUCCESS,
            items=tuple(self.items),
            assets=tuple(self.assets),
            state_events=tuple(self.state_events),
            adapter_state={
                "requested_sort": self.requested_sort,
                "effective_sort": self.effective_sort,
                "completeness": self.completeness,
                "stop_reason": self.stop_reason,
            },
            started_at=self.started_at,
            completed_at=self.completed_at,
        )


def parse_comment_from_data(
    *,
    source: Source,
    post_external_id: str,
    comment_id: str,
    text: str,
    author_name: str | None = None,
    author_id: str | None = None,
    parent_comment_id: str | None = None,
    published_at: dt.datetime | None = None,
    canonical_url: str | None = None,
    media_urls: list[str] | None = None,
    observed_at: dt.datetime | None = None,
) -> tuple[ObservedItem, list[ObservedAsset]]:
    """Construct an ObservedItem for a comment or nested reply."""
    now = observed_at or dt.datetime.now(dt.timezone.utc)
    external_id = f"comment:{comment_id}"
    parent_external_id = f"comment:{parent_comment_id}" if parent_comment_id else post_external_id
    root_external_id = post_external_id

    assets: list[ObservedAsset] = []
    if media_urls:
        for idx, murl in enumerate(media_urls):
            is_img = any(ext in murl.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"])
            assets.append(
                ObservedAsset(
                    item_external_id=external_id,
                    kind="image" if is_img else "video",
                    external_url=murl,
                    mime_type="image/jpeg" if is_img else "video/mp4",
                    content_hash=None,
                    metadata={"ordering": idx},
                )
            )

    metadata: dict[str, JSONValue] = {
        "platform": PLATFORM_FACEBOOK,
        "kind": KIND_FACEBOOK_COMMENT,
        "comment_id": comment_id,
        "parent_comment_id": parent_comment_id,
        "post_external_id": post_external_id,
        "author_id": author_id,
    }

    item = ObservedItem(
        kind=KIND_FACEBOOK_COMMENT,
        external_id=external_id,
        text=text,
        author_name=author_name,
        published_at=published_at,
        canonical_url=canonical_url,
        metadata=metadata,
        observed_at=now,
        parent_external_id=parent_external_id,
        root_external_id=root_external_id,
    )
    return item, assets


_SORT_LABELS = {
    "newest": ("Newest", "Сначала новые", "Новые"),
    "all": ("Most relevant", "Все комментарии", "Most relevant"),
}


async def _select_comment_sort(page: Any, requested_sort: str) -> bool:
    """Try to switch the comments section to the requested sort order.

    Best-effort UI interaction: returns True only when a matching sort entry
    was actually clicked, so callers never record an effective_sort they did
    not select.
    """
    labels = _SORT_LABELS.get(requested_sort)
    if not labels:
        return False
    for label in labels:
        try:
            candidate = page.locator(f"[role='feed'] >> text={label}").first
            if await candidate.count() == 0:
                continue
            await candidate.click(timeout=2000)
            return True
        except Exception as exc:
            logger.debug("sort selection %r not clickable: %s", label, exc)
            continue
    return False


async def _comment_nesting_depth(node: Any) -> int:
    """Depth of the comment within nested comment containers.

    0 means a top-level comment; 1+ means a reply nested inside another
    comment block (Facebook nests reply containers inside their parent).
    Counts every ``role='article'`` container from the node to the document
    root and subtracts the node's own container.
    """
    try:
        depth = await node.evaluate(
            "(el) => { let count = 0, n = el;"
            " while (n) { if (n.getAttribute && n.getAttribute('role') === 'article') count++;"
            " n = n.parentElement; } return Math.max(count - 1, 0); }"
        )
        return int(depth)
    except Exception:
        return 0


class FacebookCommentCollector:
    """Collector traversing Facebook comment threads with bounded pagination."""

    def __init__(
        self,
        auth_root: str = "/var/lib/telebrief/auth",
        fb_repo: FacebookRepository | None = None,
        comments_config: FacebookCommentsConfig | None = None,
    ) -> None:
        self.auth_root = auth_root
        self.fb_repo = fb_repo or FacebookRepository()
        self.comments_config = comments_config or FacebookCommentsConfig()

    async def scan_post_with_page(
        self,
        *,
        source: Source,
        post_item_id: int,
        post_external_id: str,
        page: Any,
        limits: FacebookCommentsConfig | None = None,
        mode: str = "incremental",
    ) -> CommentCollectionBatch:
        """Scan comments using an active Playwright page."""
        from src.providers.facebook.runtime_policy import is_facebook_enabled

        if not is_facebook_enabled():
            return CommentCollectionBatch(
                source_id=source.id,
                post_item_id=post_item_id,
                requested_sort="all" if mode == "deep" else "newest",
                effective_sort="all" if mode == "deep" else "newest",
                completeness="complete",
                stop_reason="disabled",
            )

        cfg = limits or self.comments_config
        started_at = dt.datetime.now(dt.timezone.utc)
        start_mono = time.monotonic()

        requested_sort = "all" if mode == "deep" else "newest"
        batch = CommentCollectionBatch(
            source_id=source.id,
            post_item_id=post_item_id,
            requested_sort=requested_sort,
            started_at=started_at,
        )
        # Only claim the effective sort when the UI selection actually
        # succeeded; otherwise the platform default is unknown to us.
        batch.effective_sort = "platform_default"
        if await _select_comment_sort(page, requested_sort):
            batch.effective_sort = f"{requested_sort}_selected"

        page_count = 0
        seen_comment_ids: set[str] = set()
        # Depth -> comment id stack for reply threading: Facebook nests reply
        # containers inside their parent comment block.
        depth_to_cid: dict[int, str] = {}

        while True:
            # Check limits
            if len(batch.items) >= cfg.max_comments_per_post:
                batch.completeness = "partial"
                batch.stop_reason = "max_comments"
                break

            if page_count >= cfg.max_pages_per_refresh:
                batch.completeness = "partial"
                batch.stop_reason = "max_pages"
                break

            if time.monotonic() - start_mono >= cfg.max_duration_per_post_seconds:
                batch.completeness = "partial"
                batch.stop_reason = "timeout"
                break

            page_count += 1
            new_found_this_page = False

            # Prefer structured comment containers; the broad dir='auto'
            # fallback runs only when none exist, otherwise inner text
            # fragments of an already-parsed comment would be ingested as
            # duplicate synthetic "replies" of their own parent.
            comment_nodes = await page.query_selector_all(
                "div[role='article'][aria-label*='comment' i]"
            )
            if not comment_nodes:
                comment_nodes = await page.query_selector_all("div[dir='auto']")
            for node in comment_nodes:
                try:
                    text = (await node.inner_text()).strip()
                    if not text or len(text) < 2:
                        continue

                    # Extract ID from links
                    links = await node.query_selector_all("a[href]")
                    cid = None
                    for link in links:
                        href = await link.get_attribute("href") or ""
                        pid = extract_post_id_from_url(href)
                        if pid and pid != post_external_id.replace("post:", ""):
                            cid = pid
                            break

                    if not cid:
                        # Derive synthetic stable ID from sha256 hash of text if no href
                        cid = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

                    if cid in seen_comment_ids:
                        continue

                    seen_comment_ids.add(cid)
                    new_found_this_page = True

                    # Threading: the nearest outer comment container wins as parent.
                    depth = await _comment_nesting_depth(node)
                    parent_comment_id = depth_to_cid.get(depth - 1) if depth > 0 else None
                    if depth >= 0:
                        depth_to_cid[depth] = cid
                        for deeper in [d for d in depth_to_cid if d > depth]:
                            del depth_to_cid[deeper]

                    item, assets = parse_comment_from_data(
                        source=source,
                        post_external_id=post_external_id,
                        comment_id=cid,
                        text=text,
                        published_at=None,
                        parent_comment_id=parent_comment_id,
                    )
                    batch.items.append(item)
                    batch.assets.extend(assets)

                    if len(batch.items) >= cfg.max_comments_per_post:
                        break
                except Exception as e:
                    logger.debug("Failed parsing comment node: %s", e)

            if len(batch.items) >= cfg.max_comments_per_post:
                batch.completeness = "partial"
                batch.stop_reason = "max_comments"
                break

            # Try clicking "View more comments" or "Load more"
            more_buttons = await page.query_selector_all(
                "div[role='button']:has-text('comments'), span:has-text('View more')"
            )
            if not more_buttons:
                # No further pagination affordance is visible. That alone does
                # NOT prove completeness under platform-curated ("Most
                # relevant") ordering: complete requires that this full pass
                # surfaced nothing new after at least one expansion round.
                if not new_found_this_page and page_count > 1:
                    batch.completeness = "complete"
                    batch.stop_reason = "exhausted"
                elif not new_found_this_page and page_count == 1:
                    batch.completeness = "unknown"
                    batch.stop_reason = "platform_behavior"
                else:
                    batch.completeness = "unknown"
                    batch.stop_reason = "no_more_buttons_with_new_content"
                break

            try:
                await more_buttons[0].click()
                await page.wait_for_timeout(1000)
            except Exception:
                batch.completeness = "unknown"
                batch.stop_reason = "platform_behavior"
                break

        batch.completed_at = dt.datetime.now(dt.timezone.utc)
        batch.total_comments_observed = len(batch.items)
        if batch.items:
            pub_times = [i.published_at for i in batch.items if i.published_at is not None]
            if pub_times:
                batch.oldest_comment_published_at = min(pub_times)
                batch.newest_comment_published_at = max(pub_times)

        return batch


@dataclass
class CommentRefreshResult:
    ingestion: Any
    batch: CommentCollectionBatch


class FacebookCommentRefreshService:
    """Refreshes Facebook post comments and co-commits domain items with comment state."""

    def __init__(
        self,
        uow: DatabaseUnitOfWork,
        fb_repo: FacebookRepository,
        ingestion_service: IngestionService,
        collector: FacebookCommentCollector,
    ) -> None:
        self.uow = uow
        self.fb_repo = fb_repo
        self.ingestion_service = ingestion_service
        self.collector = collector

    async def refresh_batch(
        self,
        *,
        source_id: int,
        post_item_id: int,
        batch: CommentCollectionBatch,
    ) -> CommentRefreshResult:
        """Persist comment collection batch and update tracking state atomically."""
        async with self.uow.transaction() as conn:
            ingestion = await self.ingestion_service.ingest_batch_in_transaction(
                conn,
                source_id=source_id,
                trigger=CollectionTrigger.ENRICHMENT,
                batch=batch.as_collection_batch(),
            )
            await self.fb_repo.update_comment_state(
                conn,
                source_item_id=post_item_id,
                last_scanned_at=batch.completed_at,
                oldest_comment_published_at=batch.oldest_comment_published_at,
                newest_comment_published_at=batch.newest_comment_published_at,
                total_comments_observed=batch.total_comments_observed,
                completeness=batch.completeness,
                continuation_state={
                    "stop_reason": batch.stop_reason,
                    "effective_sort": batch.effective_sort,
                },
            )

        return CommentRefreshResult(ingestion=ingestion, batch=batch)
