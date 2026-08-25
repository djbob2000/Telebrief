"""Completeness-oriented Facebook comments and replies collection (Plan 5 Task 4)."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
import urllib.parse
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
    _resolve_source_tz,
    extract_facebook_node_timestamp,
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


def extract_comment_id_from_url(url: str | None) -> str | None:
    """Extract Facebook comment/reply ID from query parameters or path."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if "reply_comment_id" in query and query["reply_comment_id"]:
            return str(query["reply_comment_id"][0])
        if "comment_id" in query and query["comment_id"]:
            return str(query["comment_id"][0])
        parts = parsed.path.strip("/").split("/")
        if "comments" in parts:
            idx = parts.index("comments")
            if idx + 1 < len(parts) and parts[idx + 1].isdigit():
                return parts[idx + 1]
    except Exception as exc:
        logger.debug("Failed to extract comment ID from URL %s: %s", url, exc)
    return None


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
    identity_quality: str = "native",
    temporal_fidelity: str | None = None,
    raw_timestamp: str | None = None,
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

    fidelity = temporal_fidelity or ("precise" if published_at is not None else "unknown")
    metadata: dict[str, JSONValue] = {
        "platform": PLATFORM_FACEBOOK,
        "kind": KIND_FACEBOOK_COMMENT,
        "comment_id": comment_id,
        "parent_comment_id": parent_comment_id,
        "post_external_id": post_external_id,
        "author_id": author_id,
        "identity_quality": identity_quality,
        "temporal_fidelity": fidelity,
        "raw_timestamp": raw_timestamp,
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


async def _extract_clean_comment_text(node: Any, author_name: str | None = None) -> str:
    """Extract comment body text excluding nested replies, buttons, and author header."""
    try:
        raw_text = await node.evaluate(
            """(el) => {
                const clone = el.cloneNode(true);
                clone.querySelectorAll('[role="article"]').forEach(n => n.remove());
                clone.querySelectorAll('button, [role="button"], [role="toolbar"], ul, form').forEach(n => n.remove());
                return clone.innerText || clone.textContent || '';
            }"""
        )
        if not raw_text:
            return ""
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if author_name and lines and lines[0] == author_name.strip():
            lines = lines[1:]
        ignored_lines = {
            "reply",
            "ответить",
            "нравится",
            "like",
            "share",
            "поделиться",
            "показать перевод",
            "see translation",
            "edited",
            "отредактировано",
        }
        filtered = [line for line in lines if line.lower() not in ignored_lines]
        return "\n".join(filtered).strip()
    except Exception:
        try:
            return (await node.inner_text()).strip()
        except Exception:
            return ""


_SORT_LABELS = {
    "newest": ("Newest", "Сначала новые", "Новые"),
    "all": ("All comments", "Все комментарии", "All"),
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
        parent_reply_counts: dict[str, int] = {}

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
                    # Threading: the nearest outer comment container wins as parent.
                    depth = await _comment_nesting_depth(node)
                    parent_comment_id = depth_to_cid.get(depth - 1) if depth > 0 else None

                    # Enforce reply configuration knobs
                    if depth > 0 or parent_comment_id is not None:
                        if not cfg.include_replies:
                            continue
                        if (
                            parent_comment_id
                            and parent_reply_counts.get(parent_comment_id, 0)
                            >= cfg.max_replies_per_comment
                        ):
                            continue

                    # Extract ID, author, and canonical URL from links
                    links = await node.query_selector_all("a[href]")
                    cid = None
                    author_name = None
                    author_id = None
                    canonical_url = None
                    identity_quality = "synthetic"
                    for link in links:
                        href = await link.get_attribute("href") or ""
                        parsed_cid = extract_comment_id_from_url(href)
                        if parsed_cid and not cid:
                            cid = parsed_cid
                            canonical_url = href
                            identity_quality = "native"

                        # Author ID from profile link
                        if not author_id and (
                            "/user/" in href or "/profile.php" in href or "facebook.com/" in href
                        ):
                            parsed_url = urllib.parse.urlparse(href)
                            if "profile.php" in parsed_url.path:
                                qs = urllib.parse.parse_qs(parsed_url.query)
                                if "id" in qs and qs["id"]:
                                    author_id = f"https://facebook.com/profile.php?id={qs['id'][0]}"
                            else:
                                clean_href = href.split("?")[0].rstrip("/")
                                if not any(
                                    k in clean_href.lower()
                                    for k in [
                                        "/posts/",
                                        "/permalink/",
                                        "/groups/",
                                        "/photo",
                                        "/video",
                                    ]
                                ):
                                    author_id = clean_href

                        # Author name extraction from user profile / author link in comment node
                        if not author_name:
                            link_text = (await link.inner_text()).strip()
                            if (
                                link_text
                                and len(link_text) < 60
                                and not link_text.isdigit()
                                and not link_text.startswith("#")
                            ):
                                if link_text.lower() not in {
                                    "reply",
                                    "ответить",
                                    "нравится",
                                    "like",
                                    "share",
                                    "поделиться",
                                    "показать перевод",
                                    "see translation",
                                }:
                                    author_name = link_text

                    # Extract isolated comment text excluding nested replies, buttons, and author header
                    text = await _extract_clean_comment_text(node, author_name)
                    if not text or len(text) < 2:
                        continue

                    source_tz = _resolve_source_tz(source)
                    pub_at, fidelity, raw_ts = await extract_facebook_node_timestamp(
                        node, reference_time=started_at, source_tz=source_tz
                    )

                    if not cid:
                        # Stable synthetic fallback ID: post_external_id + parent_comment_id + author_id/name + normalized text
                        # Only include timestamp for stable absolute/precise timestamps, never scan-relative ones
                        norm_text = " ".join(text.split())
                        time_sig = (
                            pub_at.isoformat()
                            if (
                                pub_at
                                and fidelity
                                in (
                                    "precise",
                                    "precise_iso",
                                    "precise_epoch",
                                    "absolute_local",
                                    "exact",
                                )
                            )
                            else ""
                        )
                        author_sig = author_id or author_name or ""
                        scope_str = f"{post_external_id}:{parent_comment_id or ''}:{author_sig}:{norm_text}:{time_sig}"
                        cid = hashlib.sha256(scope_str.encode("utf-8")).hexdigest()[:16]
                        identity_quality = "synthetic"

                    if cid in seen_comment_ids:
                        # Re-observation of the same synthetic or native comment during pagination.
                        # Crucial: update depth stack so subsequent replies on this page bind to the correct parent.
                        if depth >= 0:
                            depth_to_cid[depth] = cid
                            for deeper in [d for d in list(depth_to_cid.keys()) if d > depth]:
                                del depth_to_cid[deeper]
                        continue

                    seen_comment_ids.add(cid)
                    new_found_this_page = True

                    if depth >= 0:
                        depth_to_cid[depth] = cid
                        for deeper in [d for d in list(depth_to_cid.keys()) if d > depth]:
                            del depth_to_cid[deeper]

                    if parent_comment_id:
                        parent_reply_counts[parent_comment_id] = (
                            parent_reply_counts.get(parent_comment_id, 0) + 1
                        )

                    item, assets = parse_comment_from_data(
                        source=source,
                        post_external_id=post_external_id,
                        comment_id=cid,
                        text=text,
                        author_name=author_name,
                        author_id=author_id,
                        published_at=pub_at,
                        canonical_url=canonical_url,
                        parent_comment_id=parent_comment_id,
                        identity_quality=identity_quality,
                        temporal_fidelity=fidelity,
                        raw_timestamp=raw_ts,
                        observed_at=started_at,
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
                # No further pagination affordance is visible.
                # Invariant: effective_sort != 'all_selected' can NEVER be complete.
                if not new_found_this_page and page_count > 1:
                    batch.completeness = (
                        "complete" if batch.effective_sort == "all_selected" else "partial"
                    )
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

        if batch.effective_sort != "all_selected" and batch.completeness == "complete":
            batch.completeness = "partial"

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
