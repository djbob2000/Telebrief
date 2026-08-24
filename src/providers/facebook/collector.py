"""Facebook post collector using semantic selectors and persistent browser sessions (Plan 5 Task 3)."""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.domain.sources import Source
from src.ingestion.models import (
    CollectionBatch,
    CollectionCheckpoint,
    CollectionOutcome,
    CollectionTrigger,
    ObservedAsset,
    ObservedItem,
    ObservedStateEvent,
)
from src.ingestion.protocol import CollectionContext, Collector
from src.providers.facebook.auth import (
    FacebookAuthState,
    FacebookHumanActionRequired,
    classify_facebook_page_state,
)
from src.providers.facebook.browser import FacebookBrowserSession
from src.repositories.facebook import (
    FacebookRepository,
    resolve_auth_profile_name,
)

logger = logging.getLogger(__name__)

PLATFORM_FACEBOOK = "facebook"
KIND_FACEBOOK_POST = "facebook_post"


try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo  # type: ignore

_DEFAULT_FB_TZ = zoneinfo.ZoneInfo("Europe/Kyiv")


def _resolve_source_tz(source: Source | None) -> dt.tzinfo:
    """Resolve configured timezone from source collector options, falling back to Europe/Kyiv."""
    if source and source.collector_options:
        opts = source.collector_options
        tz_name = (
            opts.get("schedule", {}).get("timezone")
            if isinstance(opts.get("schedule"), dict)
            else None
        ) or opts.get("timezone")
        if tz_name and isinstance(tz_name, str):
            with contextlib.suppress(Exception):
                return zoneinfo.ZoneInfo(tz_name)
    return _DEFAULT_FB_TZ


def extract_post_id_from_url(url: str) -> str | None:
    """Extract canonical post ID from various Facebook URL formats."""
    if not url:
        return None

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "story_fbid" in qs and qs["story_fbid"]:
        return qs["story_fbid"][0]
    if "fbid" in qs and qs["fbid"]:
        return qs["fbid"][0]
    if "post_id" in qs and qs["post_id"]:
        return qs["post_id"][0]

    path = parsed.path.rstrip("/")
    m = re.search(r"/(?:posts|permalink|videos|photos)/(\d+)", path)
    if m:
        return m.group(1)

    m = re.search(r"/groups/[^/]+/(?:posts|permalink)/(\d+)", path)
    if m:
        return m.group(1)

    return None


def canonicalize_post_url(source_url: str, post_id: str) -> str:
    """Build a canonical permalink URL for a post."""
    parsed = urlparse(source_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    if "/groups/" in path:
        group_part = path.split("/posts")[0].split("/permalink")[0]
        return f"{base}{group_part}/posts/{post_id}/"
    return f"{base}/{path.strip('/')}/posts/{post_id}/" if path else f"{base}/posts/{post_id}/"


async def _extract_clean_post_text(node: Any) -> str:
    """Extract post body text excluding nested comments, action buttons, toolbars, and controls."""
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
        ignored_lines = {
            "like",
            "нравится",
            "подобається",
            "comment",
            "комментировать",
            "коментувати",
            "share",
            "поделиться",
            "поділитися",
            "see translation",
            "показать перевод",
            "показати переклад",
            "edited",
            "отредактировано",
            "відредаговано",
            "write a comment...",
            "напишите комментарий...",
            "напишіть коментар...",
        }
        filtered = [line for line in lines if line.lower() not in ignored_lines]
        return "\n".join(filtered).strip()
    except Exception:
        try:
            return (await node.inner_text()).strip()
        except Exception:
            return ""


_RU_MONTHS = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "май": 5,
    "мая": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}
_UA_MONTHS = {
    "січ": 1,
    "лют": 2,
    "бер": 3,
    "кві": 4,
    "тра": 5,
    "чер": 6,
    "лип": 7,
    "сер": 8,
    "вер": 9,
    "жов": 10,
    "лис": 11,
    "гру": 12,
}
_EN_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_ALL_MONTHS = {**_RU_MONTHS, **_UA_MONTHS, **_EN_MONTHS}


def parse_facebook_timestamp_with_fidelity(
    text: str,
    reference_time: dt.datetime | None = None,
    source_tz: dt.tzinfo | None = None,
) -> tuple[dt.datetime | None, str]:
    """Parse relative or absolute Facebook timestamp string into UTC datetime and fidelity."""
    if not text:
        return None, "unknown"
    raw = text.strip().lower()
    ref = reference_time or dt.datetime.now(dt.timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=dt.timezone.utc)
    target_tz = source_tz or _DEFAULT_FB_TZ

    # 1. Epoch integer timestamp (data-utime)
    if raw.isdigit() and len(raw) >= 9:
        with contextlib.suppress(Exception):
            return dt.datetime.fromtimestamp(int(raw), tz=dt.timezone.utc), "precise_epoch"

    # 2. ISO 8601 string
    with contextlib.suppress(Exception):
        iso_cand = text.strip().replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(iso_cand)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc), "precise_iso"

    # 3. Immediate relative: "Just now", "только что", "щойно"
    if raw in ("just now", "только что", "щойно", "сейчас", "now"):
        return ref, "relative"

    # 4. Minutes: e.g. "5 mins", "5 мин", "5 хв", "5m"
    m = re.search(r"(\d+)\s*(?:m|min|mins|минут|мин|хв|хвилини|хвилин)\b", raw)
    if m:
        return ref - dt.timedelta(minutes=int(m.group(1))), "relative"

    # 5. Hours: e.g. "2 hrs", "2 ч", "2 год", "2h"
    m = re.search(r"(\d+)\s*(?:h|hr|hrs|hours|час|часа|часов|ч|год|години|годин)\b", raw)
    if m:
        return ref - dt.timedelta(hours=int(m.group(1))), "relative"

    # 6. Days: e.g. "3 days", "3 дн", "3 дні", "3d"
    m = re.search(r"(\d+)\s*(?:d|day|days|дн|дня|дней|днів|д)\b", raw)
    if m:
        return ref - dt.timedelta(days=int(m.group(1))), "relative"

    ref_local = ref.astimezone(target_tz)

    # 7. "Yesterday at 14:30" / "вчера в 14:30" / "вчора о 14:30"
    m = re.search(r"(?:yesterday|вчера|вчора)\s+(?:at|в|о)\s+(\d{1,2}):(\d{2})", raw)
    if m:
        hr, mn = int(m.group(1)), int(m.group(2))
        yesterday_local = (ref_local - dt.timedelta(days=1)).replace(
            hour=hr, minute=mn, second=0, microsecond=0
        )
        return yesterday_local.astimezone(dt.timezone.utc), "absolute_local"

    # 8. "Today at 14:30" / "сегодня в 14:30" / "сьогодні о 14:30"
    m = re.search(r"(?:today|сегодня|сьогодні)\s+(?:at|в|о)\s+(\d{1,2}):(\d{2})", raw)
    if m:
        hr, mn = int(m.group(1)), int(m.group(2))
        today_local = ref_local.replace(hour=hr, minute=mn, second=0, microsecond=0)
        return today_local.astimezone(dt.timezone.utc), "absolute_local"

    # 9. Day + month + optional year + optional time: e.g. "24 фев в 14:00", "24 February at 14:00"
    m = re.search(r"(\d{1,2})\s+([a-zа-яіє]+)(?:\s+(\d{4}))?(?:[^\d]+(\d{1,2}):(\d{2}))?", raw)
    if m:
        day = int(m.group(1))
        mon_str = m.group(2)[:3]
        year = int(m.group(3)) if m.group(3) else ref_local.year
        hour = int(m.group(4)) if m.group(4) else 0
        minute = int(m.group(5)) if m.group(5) else 0
        mon = _ALL_MONTHS.get(mon_str)
        if mon:
            with contextlib.suppress(Exception):
                local_dt = dt.datetime(year, mon, day, hour, minute, tzinfo=target_tz)
                return local_dt.astimezone(dt.timezone.utc), "absolute_local"

    return None, "unknown"


def parse_facebook_timestamp_str(
    text: str,
    reference_time: dt.datetime | None = None,
    source_tz: dt.tzinfo | None = None,
) -> dt.datetime | None:
    """Parse relative or absolute Facebook timestamp string into UTC datetime."""
    parsed, _ = parse_facebook_timestamp_with_fidelity(
        text, reference_time=reference_time, source_tz=source_tz
    )
    return parsed


async def extract_facebook_node_timestamp(
    node: Any,
    reference_time: dt.datetime | None = None,
    source_tz: dt.tzinfo | None = None,
) -> tuple[dt.datetime | None, str, str | None]:
    """Extract publication timestamp, fidelity, and raw string from a post or comment DOM node."""
    ref = reference_time or dt.datetime.now(dt.timezone.utc)
    try:
        attr_data = await node.evaluate(
            """(el) => {
                const candidates = [];
                const utimeEl = el.querySelector('[data-utime], [data-time]') || (el.hasAttribute && (el.hasAttribute('data-utime') || el.hasAttribute('data-time')) ? el : null);
                if (utimeEl) {
                    const u = utimeEl.getAttribute('data-utime') || utimeEl.getAttribute('data-time');
                    if (u) candidates.push({type: 'utime', val: u});
                }
                const timeEls = el.querySelectorAll('time, abbr');
                for (const t of timeEls) {
                    const dtAttr = t.getAttribute('datetime');
                    if (dtAttr) candidates.push({type: 'datetime', val: dtAttr});
                    const title = t.getAttribute('title');
                    if (title) candidates.push({type: 'title', val: title});
                    const text = (t.innerText || t.textContent || '').trim();
                    if (text) candidates.push({type: 'text', val: text});
                }
                const timestampLinks = el.querySelectorAll('a[href*="/posts/"], a[href*="story_fbid"], a[href*="comment_id"], a[href*="permalink"]');
                for (const l of timestampLinks) {
                    const title = l.getAttribute('title');
                    if (title) candidates.push({type: 'title', val: title});
                    const text = (l.innerText || l.textContent || '').trim();
                    if (text) candidates.push({type: 'text', val: text});
                }
                return candidates;
            }"""
        )
        for item in attr_data:
            val = item.get("val", "")
            if not val:
                continue
            parsed, fidelity = parse_facebook_timestamp_with_fidelity(
                val, reference_time=ref, source_tz=source_tz
            )
            if parsed:
                return parsed, fidelity, str(val)
    except Exception as exc:
        logger.debug("Failed extracting node timestamp: %s", exc)
    return None, "unknown", None


def parse_post_from_data(
    *,
    source: Source,
    post_id: str,
    text: str,
    author_name: str | None = None,
    author_id: str | None = None,
    published_at: dt.datetime | None = None,
    canonical_url: str | None = None,
    media_urls: list[str] | None = None,
    extra_metadata: dict[str, Any] | None = None,
    observed_at: dt.datetime | None = None,
    temporal_fidelity: str | None = None,
    raw_timestamp: str | None = None,
) -> tuple[ObservedItem, list[ObservedAsset]]:
    """Create a standardized ObservedItem and attachments for a Facebook post."""
    now = observed_at or dt.datetime.now(dt.timezone.utc)
    canonical = canonical_url or canonicalize_post_url(source.url or "", post_id)
    external_id = f"post:{post_id}"

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
    metadata = {
        "platform": PLATFORM_FACEBOOK,
        "kind": KIND_FACEBOOK_POST,
        "post_id": post_id,
        "source_url": source.url,
        "author_id": author_id,
        "temporal_fidelity": fidelity,
        "raw_timestamp": raw_timestamp,
        **(extra_metadata or {}),
    }

    item = ObservedItem(
        kind=KIND_FACEBOOK_POST,
        external_id=external_id,
        text=text,
        author_name=author_name,
        published_at=published_at,
        canonical_url=canonical,
        metadata=metadata,
        observed_at=now,
    )
    return item, assets


class FacebookCollector(Collector):
    """Collector for Facebook group and page posts."""

    def __init__(
        self,
        auth_root: str = "/var/lib/telebrief/auth",
        fb_repo: FacebookRepository | None = None,
    ) -> None:
        self.auth_root = auth_root
        self.fb_repo = fb_repo or FacebookRepository()

    async def scan(
        self,
        source: Source,
        checkpoint: CollectionCheckpoint | None,
        context: CollectionContext,
    ) -> CollectionBatch:
        """Collector protocol implementation."""
        return await self.collect(source, checkpoint, trigger=CollectionTrigger.SCHEDULED)

    async def collect(
        self,
        source: Source,
        checkpoint: CollectionCheckpoint | None,
        trigger: CollectionTrigger,
    ) -> CollectionBatch:
        """Scan posts from the explicit source URL."""
        started_at = dt.datetime.now(dt.timezone.utc)
        from src.providers.facebook.runtime_policy import is_facebook_enabled

        if not is_facebook_enabled():
            return CollectionBatch(
                outcome=CollectionOutcome.SUCCESS,
                items=(),
                assets=(),
                state_events=(),
                adapter_state=checkpoint.adapter_state if checkpoint else {},
                started_at=started_at,
                completed_at=dt.datetime.now(dt.timezone.utc),
            )

        if not source.url:
            return CollectionBatch(
                outcome=CollectionOutcome.PERMANENT,
                items=(),
                assets=(),
                state_events=(),
                adapter_state={},
                started_at=started_at,
                completed_at=dt.datetime.now(dt.timezone.utc),
                error_kind="missing_source_url",
            )

        from src.runtime import get_runtime

        runtime = get_runtime()
        async with runtime.uow.transaction() as conn:
            auth_profile_name = await resolve_auth_profile_name(conn, source.id)
            profile = await self.fb_repo.get_or_create_auth_profile(
                conn, name=auth_profile_name, storage_ref=auth_profile_name
            )

        from src.providers.facebook.auth import is_profile_runnable

        if not is_profile_runnable(profile.status):
            return CollectionBatch(
                outcome=CollectionOutcome.AUTH_REQUIRED,
                items=(),
                assets=(),
                state_events=(),
                adapter_state={},
                started_at=started_at,
                completed_at=dt.datetime.now(dt.timezone.utc),
                error_kind=f"profile_{profile.status}",
            )

        items: list[ObservedItem] = []
        assets: list[ObservedAsset] = []
        state_events: list[ObservedStateEvent] = []

        try:
            async with FacebookBrowserSession(self.auth_root, profile, headless=True) as (_, page):
                try:
                    await page.goto(source.url, wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    return CollectionBatch(
                        outcome=CollectionOutcome.TRANSIENT,
                        items=(),
                        assets=(),
                        state_events=(),
                        adapter_state={},
                        started_at=started_at,
                        completed_at=dt.datetime.now(dt.timezone.utc),
                        error_kind="navigation_timeout",
                    )

                session_state = await classify_facebook_page_state(page)
                if session_state in (
                    FacebookAuthState.AUTH_REQUIRED,
                    FacebookAuthState.CHECKPOINT_REQUIRED,
                    FacebookAuthState.ACCOUNT_ACTION_REQUIRED,
                ):
                    async with runtime.uow.transaction() as conn:
                        await self.fb_repo.update_auth_profile_status(
                            conn,
                            profile.id,
                            status=session_state.value,
                            error_kind=session_state.value,
                            error_message=f"Encountered {session_state.value} on {source.url}",
                        )
                    outcome = (
                        CollectionOutcome.AUTH_REQUIRED
                        if session_state == FacebookAuthState.AUTH_REQUIRED
                        else CollectionOutcome.ACCOUNT_ACTION_REQUIRED
                    )
                    return CollectionBatch(
                        outcome=outcome,
                        items=(),
                        assets=(),
                        state_events=(),
                        adapter_state={},
                        started_at=started_at,
                        completed_at=dt.datetime.now(dt.timezone.utc),
                        error_kind=session_state.value,
                    )

                content = (await page.content()).lower()
                now = dt.datetime.now(dt.timezone.utc)
                if any(
                    m in content
                    for m in [
                        "this content isn't available right now",
                        "this page isn't available",
                        "the link you followed may be broken",
                    ]
                ):
                    logger.info("Facebook source %s page content is unavailable", source.id)
                    # Nothing item-scoped can be recorded (we cannot know
                    # which posts are gone), but the scan-level observation is
                    # persisted durably via collection_batches.adapter_state.
                    return CollectionBatch(
                        outcome=CollectionOutcome.SUCCESS,
                        items=(),
                        assets=(),
                        state_events=(),
                        adapter_state={
                            "page_unavailable": True,
                            "source_url": source.url,
                        },
                        started_at=started_at,
                        completed_at=now,
                    )

                # Scroll down 3 times to load feed posts
                for _ in range(3):
                    await page.evaluate("window.scrollBy(0, window.innerHeight * 2);")
                    await page.wait_for_timeout(1500)

                articles = await page.query_selector_all(
                    "div[role='article'], div[role='feed'] > div"
                )
                for article in articles:
                    try:
                        text_content = await _extract_clean_post_text(article)
                        if not text_content or len(text_content) < 10:
                            continue

                        links = await article.query_selector_all("a[href]")
                        post_id = None
                        post_url = None
                        for link in links:
                            href = await link.get_attribute("href") or ""
                            pid = extract_post_id_from_url(href)
                            if pid:
                                post_id = pid
                                post_url = href
                                break

                        if not post_id:
                            continue

                        author_name = None
                        if links:
                            first_link_text = (await links[0].inner_text()).strip()
                            if first_link_text and len(first_link_text) < 60:
                                author_name = first_link_text

                        media_urls: list[str] = []
                        images = await article.query_selector_all("img[src]")
                        for img in images:
                            src = await img.get_attribute("src") or ""
                            if (
                                src.startswith("http")
                                and "emoji" not in src
                                and "static" not in src
                            ):
                                media_urls.append(src)

                        source_tz = _resolve_source_tz(source)
                        pub_at, fidelity, raw_ts = await extract_facebook_node_timestamp(
                            article, reference_time=started_at, source_tz=source_tz
                        )

                        item, item_assets = parse_post_from_data(
                            source=source,
                            post_id=post_id,
                            text=text_content,
                            author_name=author_name,
                            canonical_url=post_url,
                            media_urls=media_urls,
                            published_at=pub_at,
                            temporal_fidelity=fidelity,
                            raw_timestamp=raw_ts,
                            observed_at=started_at,
                        )
                        items.append(item)
                        assets.extend(item_assets)
                    except Exception as e:
                        logger.debug("Failed parsing article node: %s", e)

        except FacebookHumanActionRequired as e:
            async with runtime.uow.transaction() as conn:
                await self.fb_repo.update_auth_profile_status(
                    conn,
                    profile.id,
                    status=e.state.value,
                    error_kind=e.state.value,
                    error_message=str(e),
                )
            return CollectionBatch(
                outcome=CollectionOutcome.AUTH_REQUIRED,
                items=(),
                assets=(),
                state_events=(),
                adapter_state={},
                started_at=started_at,
                completed_at=dt.datetime.now(dt.timezone.utc),
                error_kind=e.state.value,
            )
        except Exception as e:
            logger.exception("Unexpected error in FacebookCollector: %s", e)
            return CollectionBatch(
                outcome=CollectionOutcome.TRANSIENT,
                items=(),
                assets=(),
                state_events=(),
                adapter_state={},
                started_at=started_at,
                completed_at=dt.datetime.now(dt.timezone.utc),
                error_kind="unexpected_browser_error",
            )

        return CollectionBatch(
            outcome=CollectionOutcome.SUCCESS,
            items=tuple(items),
            assets=tuple(assets),
            state_events=tuple(state_events),
            adapter_state={},
            started_at=started_at,
            completed_at=dt.datetime.now(dt.timezone.utc),
        )
