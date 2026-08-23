"""
Core digest/article entry points.

Since the Plan 4 cutover this module is a thin compatibility facade around
the durable publication pipeline (:mod:`src.publication.facade`): scheduled,
bot, MCP, and CLI callers request a ``PublicationRun`` over frozen knowledge
and the Procrastinate worker performs selection, generation, and delivery.
No function here collects from live providers or instantiates a Collector.
"""

import asyncio
import inspect
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.collector import Message
from src.config_loader import ChannelConfig, Config
from src.extensions.loader import load_class
from src.formatter import DigestFormatter
from src.grouper import DigestGrouper
from src.image_generator import NewsImageGenerator
from src.ingestion.reader import SourceRevisionReader
from src.ingestion.repository import IngestionRepository
from src.runtime import get_runtime
from src.summarizer import ERROR_SUMMARY_PREFIX, Summarizer
from src.ui_strings import get_ui_strings

_CHANNEL_URL_RE = re.compile(r"^https://t\.me/(?:c/\d+|[^/]{2,})$")

_DIGEST_CACHE_PATH = Path("data/last_digest.json")
MAX_DIGEST_HOURS = 168  # one week; guards against a caller asking for a year of history
MAX_CHANNEL_MESSAGES = 5000  # safety ceiling; normal collection is bounded by the time window

DIGEST_PUBLICATION_TYPE = "digest_grouped"
ARTICLE_PUBLICATION_TYPE = "daily_article"

# ponytail: one process-wide lock for the synchronous compatibility builders;
# the MCP server and CLI preview can both run them concurrently.
_digest_lock = asyncio.Lock()


async def _apply_filters(
    channel_cfg: ChannelConfig,
    messages: list,
    config: Config,
    logger: logging.Logger,
) -> list:
    """Apply the effective filter chain to messages for a given channel."""
    effective_specs = (
        channel_cfg.filters if channel_cfg.filters is not None else config.settings.filters
    )
    if not effective_specs:
        return messages

    for spec in effective_specs:
        try:
            cls = load_class(spec.class_path)
            filt = cls(**spec.config)
        except Exception as e:
            logger.error(
                f"Filter load/init failed ({spec.class_path}, {type(e).__name__}), skipping",
                exc_info=True,
            )
            continue
        if not hasattr(filt, "filter"):
            logger.error(
                f"Filter {spec.class_path!r} missing filter() method, skipping",
            )
            continue
        if not inspect.iscoroutinefunction(filt.filter):
            logger.error(
                f"Filter {spec.class_path!r} filter() is not async — add 'async' keyword, skipping",
            )
            continue
        try:
            result = await filt.filter(channel_cfg, messages)
            if not isinstance(result, list):
                logger.error(
                    f"Filter {getattr(filt, 'name', spec.class_path)!r} filter() returned"
                    f" {type(result).__name__} (expected list), skipping",
                )
            else:
                messages = result
        except Exception as e:
            logger.error(
                f"Filter {getattr(filt, 'name', spec.class_path)!r} filter() raised"
                f" {type(e).__name__} ({spec.class_path}), skipping",
                exc_info=True,
            )

    return messages


async def _apply_configured_filters(
    messages_by_channel: dict, config: Config, logger: logging.Logger
) -> dict:
    """Run the configured filter chain per logical channel, in place per key."""
    channel_map = {ch.name: ch for ch in config.channels}
    filtered: dict = {}
    for channel_name, msgs in messages_by_channel.items():
        ch_cfg = _channel_config_for_name(config, channel_name, channel_map)
        if ch_cfg is not None:
            msgs = await _apply_filters(ch_cfg, msgs, config, logger)
        else:
            logger.warning(f"Channel {channel_name!r} not in config; skipping filters")
        filtered[channel_name] = msgs
    return filtered


async def _read_persistent_messages(config: Config, hours: int) -> dict:
    """Read digest inputs from the persisted source history (no live Telegram).

    DB failures propagate explicitly on purpose: there is no live Telegram
    fallback, since the digest must reflect what the ingestion path actually
    persisted.
    """
    runtime = get_runtime()
    reader = SourceRevisionReader(runtime.uow, IngestionRepository())
    now = datetime.now(timezone.utc)
    return await reader.read_telegram_messages(
        edition_slug="berdyansk",
        since=now - timedelta(hours=hours),
        until=now,
    )


async def _collect_messages(config: Config, logger: logging.Logger, hours: int) -> dict:
    """Collect digest inputs from PostgreSQL history (the only source)."""
    messages_by_channel = await _read_persistent_messages(config, hours)
    total = sum(len(msgs) for msgs in messages_by_channel.values())
    logger.info(f"Read {total} persisted messages from {len(messages_by_channel)} sources")

    messages_by_channel = await _apply_configured_filters(messages_by_channel, config, logger)
    return messages_by_channel


def _channel_config_for_name(
    config: Config, channel_name: str, channel_map: dict[str, ChannelConfig] | None = None
) -> ChannelConfig | None:
    """Resolve a collected source name to its config, including forum topics."""
    channel_map = channel_map or {ch.name: ch for ch in config.channels}
    exact = channel_map.get(channel_name)
    if exact is not None:
        return exact
    topic_separator = " — "
    for channel in config.channels:
        if channel_name.startswith(f"{channel.name}{topic_separator}"):
            return channel
    return None


async def _summarize_channels(
    config: Config, logger: logging.Logger, messages_by_channel: dict
) -> dict:
    """Generate AI summaries for all channels."""
    logger.info("Generating AI summaries")
    summarizer = Summarizer(config, logger)
    summary_result = await summarizer.summarize_all(messages_by_channel)
    logger.info(f"Generated summaries for {len(summary_result['channel_summaries'])} channels")
    return summary_result


def _filter_valid_summaries(channel_summaries: dict) -> dict:
    """Filter out empty or error summaries."""
    return {
        name: summary
        for name, summary in channel_summaries.items()
        if summary and not summary.lower().startswith(ERROR_SUMMARY_PREFIX.lower())
    }


def _format_group_messages(
    formatter: "DigestFormatter",
    grouped: dict,
    config: "Config",
    hours: int,
) -> str:
    """Format ordered non-empty groups into one digest message."""
    sections = []
    for name in _order_groups(grouped, config):
        points = grouped.get(name, [])
        if not points:
            continue
        sections.append((name, points))
    return formatter.format_group_digest(sections, hours=hours)


def _order_groups(grouped: dict, config: "Config") -> list[str]:
    """Order group names: config-defined order first, then remaining, 'Other' last.

    Recognizes both the localized UI string and the literal "Other"
    (case-insensitive) as the special bucket to push to the end.
    """
    config_order = [g.name for g in config.settings.digest_groups]
    localized_other = get_ui_strings(config.settings.output_language).get("group_other", "Other")

    def _is_other(name: str) -> bool:
        return name == localized_other or name.lower() == "other"

    not_config_ordered = grouped.keys() - set(config_order)
    remaining = sorted(k for k in not_config_ordered if not _is_other(k))
    other_keys = sorted(k for k in not_config_ordered if _is_other(k))
    return [n for n in config_order if n in grouped] + remaining + other_keys


def _build_channel_urls(messages_by_channel: dict) -> dict[str, str]:
    """Extract base channel URL for each channel from its messages."""
    urls: dict[str, str] = {}
    for channel_name, messages in messages_by_channel.items():
        for msg in messages:
            if msg.link and msg.link != "#":
                base = msg.link.rsplit("/", 1)[0]
                if _CHANNEL_URL_RE.match(base):
                    urls[channel_name] = base
                    break
    return urls


def validate_hours(hours: int) -> None:
    """Reject lookback windows that are not a sane number of hours.

    Raises:
        ValueError: If hours is not an int in [1, MAX_DIGEST_HOURS]
    """
    if not isinstance(hours, int) or isinstance(hours, bool):
        raise ValueError(f"hours must be an int, got {hours!r}")
    if hours <= 0 or hours > MAX_DIGEST_HOURS:
        raise ValueError(f"hours must be between 1 and {MAX_DIGEST_HOURS}, got {hours}")


def _join_parts(parts: list[tuple[str, str]], summary_message: str) -> str:
    """Join digest messages into one document, header first (matches send order)."""
    return "\n\n".join([text for text in [summary_message, *(text for _, text in parts)] if text])


def _write_last_digest(text: str, hours: int, logger: logging.Logger) -> None:
    """Cache the digest so it can be served later without regenerating it.

    Written here rather than at the call sites so that scheduled digests land in
    the cache too — otherwise the cache would only ever hold on-demand runs.
    """
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hours": hours,
        "text": text,
    }
    try:
        _DIGEST_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # write-then-replace so a concurrent reader never sees a half-written file
        tmp_path = _DIGEST_CACHE_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(_DIGEST_CACHE_PATH)
    except OSError as e:
        logger.error(f"Caching digest failed ({type(e).__name__}), digest continues")


def _read_last_digest_file() -> Optional[dict]:
    """File-backed compatibility cache reader (legacy deployments/tests)."""
    try:
        data = json.loads(_DIGEST_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("text"), str) else None


async def read_last_digest_async() -> Optional[dict]:
    """Latest successfully delivered digest from PostgreSQL, else file cache.

    The durable pipeline records every delivered digest as an immutable
    Publication with a telegram_channel payload; that history — not a local
    file — is the source of truth once the database is enabled.
    """
    try:
        runtime = get_runtime()
    except RuntimeError:
        return _read_last_digest_file()
    from src.publication.repository import PublicationRepository
    from src.repositories.editions import EditionRepository

    try:
        async with runtime.uow.transaction() as conn:
            edition = await EditionRepository().get_by_slug(conn, "berdyansk")
            if edition is None:
                return _read_last_digest_file()
            text = await PublicationRepository().get_latest_delivered_digest_text(
                conn, edition_id=edition.id
            )
    except Exception:
        return _read_last_digest_file()
    if not text:
        return _read_last_digest_file()
    return {"generated_at": None, "hours": None, "text": text, "source": "publication"}


def read_last_digest() -> Optional[dict]:
    """Compatibility facade: file cache only (see :func:`read_last_digest_async`)."""
    return _read_last_digest_file()


async def _build_grouped_parts(
    config: Config, logger: logging.Logger, hours: int
) -> Optional[tuple[list[tuple[str, str]], str, dict]]:
    """Build topic-grouped digest messages. Returns None if there is nothing to send."""
    messages_by_channel = await _collect_messages(config, logger, hours)
    if not sum(len(msgs) for msgs in messages_by_channel.values()):
        logger.warning("No messages collected, skipping digest generation")
        return None

    summary_result = await _summarize_channels(config, logger, messages_by_channel)
    valid_summaries = _filter_valid_summaries(summary_result["channel_summaries"])
    if not valid_summaries:
        logger.warning("No valid channel summaries to group")
        return None

    logger.info("Grouping summaries by topic")
    channel_urls = _build_channel_urls(messages_by_channel)
    grouper = DigestGrouper(config, logger)
    grouped = await grouper.group_summaries(valid_summaries, channel_urls)
    if not grouped:
        logger.warning("No groups produced, skipping send")
        return None

    logger.info("Formatting group messages")
    formatter = DigestFormatter(config, logger)
    group_digest = _format_group_messages(formatter, grouped, config, hours)
    if not group_digest:
        logger.warning("No valid group messages to send")
        return None
    sections = [
        (name, grouped[name]) for name in _order_groups(grouped, config) if grouped.get(name)
    ]
    rich_document = formatter.format_group_rich_digest(sections)
    return [("digest", group_digest)], "", rich_document


async def _build_channel_parts(
    config: Config, logger: logging.Logger, hours: int
) -> Optional[tuple[list[tuple[str, str]], str, None]]:
    """Build one digest message per channel. Returns None if there is nothing to send."""
    messages_by_channel = await _collect_messages(config, logger, hours)
    total_messages = sum(len(msgs) for msgs in messages_by_channel.values())
    if total_messages == 0:
        logger.warning("No messages collected, skipping digest generation")
        return None

    summary_result = await _summarize_channels(config, logger, messages_by_channel)
    valid_summaries = _filter_valid_summaries(summary_result["channel_summaries"])

    formatter = DigestFormatter(config, logger)
    channel_messages = []
    for channel_name, summary in valid_summaries.items():
        messages = messages_by_channel.get(channel_name, [])
        formatted_message = formatter.format_channel_message(
            channel_name=channel_name, summary=summary, messages=messages, hours=hours
        )
        channel_messages.append((channel_name, formatted_message))

    if not channel_messages:
        logger.warning("No valid channel messages to send")
        return None

    summary_message = formatter.format_summary_message(
        total_channels=len(channel_messages), total_messages=total_messages, hours=hours
    )
    return channel_messages, summary_message, None


async def _build_digest_parts(
    config: Config, logger: logging.Logger, hours: int
) -> Optional[tuple[list[tuple[str, str]], str, Optional[dict]]]:
    """Collect, summarize and format the digest according to the configured digest_mode.

    Single chokepoint for every digest producer (scheduler, bot, MCP server), so the
    lock and the cache write only need to exist here.

    Returns:
        (messages, summary_header, rich_document) or None when there is nothing worth sending
    """
    validate_hours(hours)
    async with _digest_lock:
        mode = config.settings.digest_mode
        start_time = datetime.now(timezone.utc)
        logger.info(f"Starting {mode!r} digest build for last {hours} hours")

        built: Optional[tuple[list[tuple[str, str]], str, Optional[dict]]] = None
        if mode == "digest":
            built = await _build_grouped_parts(config, logger, hours)
        else:
            built = await _build_channel_parts(config, logger, hours)

        if built is None:
            return None

        parts, summary_message, _ = built
        _write_last_digest(_join_parts(parts, summary_message), hours, logger)

        execution_time = (datetime.now(timezone.utc) - start_time).total_seconds()
        logger.info(f"Digest build completed in {execution_time:.1f}s")
        return built


def _resolve_channel(config: Config, channel: str) -> ChannelConfig:
    """Find the configured channel by its name or its id.

    Raises:
        ValueError: If no configured channel matches
    """
    wanted = channel.strip().lower()
    for ch in config.channels:
        if wanted in (ch.name.lower(), str(ch.id).lower()):
            return ch
    known = ", ".join(ch.name for ch in config.channels) or "none configured"
    raise ValueError(f"Unknown channel {channel!r}. Configured channels: {known}")


async def collect_channel_messages(
    config: Config,
    logger: logging.Logger,
    channel: str,
    hours: int = 24,
    limit: int = 200,
) -> tuple[list[Message], str]:
    """Fetch one channel's messages, preferring stored ones over a live Telegram read.

    With ``settings.persistent_ingestion`` or ``database.enabled`` this reads persisted source
    history only; storage snapshots and live Telegram reads are never consulted.
    """
    validate_hours(hours)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_CHANNEL_MESSAGES
    ):
        raise ValueError(f"limit must be between 1 and {MAX_CHANNEL_MESSAGES}, got {limit!r}")

    channel_cfg = _resolve_channel(config, channel)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    messages = await _read_persistent_channel_messages(config, logger, channel_cfg, since)
    return messages[-limit:], "persistent"


async def _read_persistent_channel_messages(
    config: Config,
    logger: logging.Logger,
    channel_cfg: ChannelConfig,
    since: datetime,
) -> list[Message]:
    """Read one channel's inputs from persisted source history instead of Telethon."""
    runtime = get_runtime()
    reader = SourceRevisionReader(runtime.uow, IngestionRepository())
    all_messages = await reader.read_telegram_messages(
        edition_slug="berdyansk",
        since=since,
        until=datetime.now(timezone.utc),
    )
    # Topic keys ("channel — topic") resolve to their parent channel config.
    messages = [
        message
        for key, keyed in all_messages.items()
        if _channel_config_for_name(config, key) is channel_cfg
        for message in keyed
    ]
    messages = await _apply_filters(channel_cfg, messages, config, logger)
    logger.info(f"Read {len(messages)} persisted messages for {channel_cfg.name!r}")
    return messages


async def build_digest(config: Config, logger: logging.Logger, hours: int = 24) -> str:
    """Build the digest as a single Markdown document without sending it.

    Returns:
        The digest text, or an empty string when there was nothing to report
    """
    built = await _build_digest_parts(config, logger, hours)
    return "" if built is None else _join_parts(built[0], built[1])


async def generate_and_send_digest(
    config: Config,
    logger: logging.Logger,
    hours: int = 24,
    user_id: Optional[int] = None,
) -> bool:
    """Request a durable digest publication over current frozen knowledge.

    Compatibility facade for bot/MCP/CLI callers (Plan 4 Task 8): the run is
    created, candidates sealed, and the selection -> generation -> delivery
    chain deferred onto the Procrastinate ``publication`` queue. The worker
    performs generation and delivery asynchronously; nothing is collected or
    generated inline here.

    Args:
        config: Application configuration
        logger: Logger instance
        hours: Retained for call-site compatibility; the persistent pipeline
            derives its window from the sealed snapshot, not this value.
        user_id: Unused delivery detail; destinations are resolved by the
            delivery service.

    Returns:
        True when the publication request was durably queued.

    Raises:
        PublicationConfigError: database/persistent_ingestion not enabled.
        ValueError: If hours is outside [1, MAX_DIGEST_HOURS]
    """
    validate_hours(hours)
    del user_id  # delivery destinations come from configuration, not callers
    try:
        from src.publication.facade import request_publication

        result = await request_publication(
            DIGEST_PUBLICATION_TYPE,
            config=config,
        )
        logger.info("digest publication requested: run %s (%s)", result.run_id, result.request_key)
        return True
    except Exception as e:
        logger.error(f"Digest publication request failed: {e}", exc_info=True)
        return False


def _build_fallback_article(
    messages_by_channel: dict[str, list[Message]], channels: list[ChannelConfig] | None = None
) -> tuple[str, str, str]:
    """Build the same thematic fallback as ArticleGenerator, never a raw message dump."""
    from src.config_loader import SourceRoleResolver
    from src.editorial_fallback import DeterministicStoryCardBuilder, StoryCardRenderer
    from src.editorial_input import EditorialInputBuilder

    bundle = EditorialInputBuilder(SourceRoleResolver(channels or [])).build(messages_by_channel)
    cards = DeterministicStoryCardBuilder().build(bundle)
    draft = StoryCardRenderer().render(cards)
    markdown = draft.to_markdown()
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    title = lines[0].removeprefix("# ") if lines else "Редакционная заметка"
    lead = lines[1] if len(lines) > 1 else ""
    return title, lead, markdown


async def generate_and_publish_article(
    config: Config,
    logger: logging.Logger,
    hours: int = 24,
    user_id: Optional[int] = None,
    dry_run: bool = False,
) -> bool:
    """Request a durable editorial article publication (Plan 4 Task 8 facade).

    Without ``dry_run`` this is a thin wrapper over
    :func:`src.publication.facade.request_publication`: the worker performs
    generation and delivery asynchronously from frozen knowledge.

    With ``dry_run=True`` a synchronous read-only preview is built from the
    persisted source history instead — no live collection, no Telegraph page,
    no Telegram send. The preview exists so CLI operators can inspect the
    editorial material without producing a Publication.

    Args:
        config: Application configuration
        logger: Logger instance
        hours: Lookback window in hours (preview window; ignored by the
            durable pipeline, which uses its sealed snapshot)
        user_id: Unused delivery detail; kept for call-site compatibility.
        dry_run: If True, generate and print/save a preview without publishing

    Returns:
        True when the request was queued (or preview produced), False otherwise
    """
    validate_hours(hours)
    del user_id
    start_time = datetime.now(timezone.utc)
    logger.info(f"Starting editorial article workflow for last {hours} hours (dry_run={dry_run})")

    if not dry_run:
        try:
            from src.publication.facade import request_publication

            result = await request_publication(
                ARTICLE_PUBLICATION_TYPE,
                config=config,
            )
            logger.info(
                "article publication requested: run %s (%s)",
                result.run_id,
                result.request_key,
            )
            return True
        except Exception as e:
            logger.error(f"Article publication request failed: {e}", exc_info=True)
            return False

    # Dry-run preview: read-only generation over persisted history only.
    messages_by_channel = await _collect_messages(config, logger, hours)
    total_messages = sum(len(msgs) for msgs in messages_by_channel.values())
    if total_messages == 0:
        logger.warning("No persisted messages available for article preview")
        return False

    from src.article_generator import ArticleGenerator
    from src.editorial_fallback import NoSubstantiveMaterialError

    try:
        generator = ArticleGenerator(config, logger)
        try:
            title, lead, markdown_body = await generator.generate_article(messages_by_channel)
        except NoSubstantiveMaterialError:
            logger.info("No publishable local material for article period")
            return False
        except Exception as exc:
            logger.warning(
                "ArticleGenerator failed; entering thematic fallback: %s", type(exc).__name__
            )
            title, lead, markdown_body = _build_fallback_article(
                messages_by_channel, config.channels
            )

        # Preview local save
        fallback_dir = Path(config.settings.article.fallback_save_dir)
        try:
            fallback_dir.mkdir(parents=True, exist_ok=True)
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
            fallback_file = fallback_dir / f"preview_{now_str}_editorial.md"
            fallback_file.write_text(markdown_body, encoding="utf-8")
            logger.info(f"Saved article copy to {fallback_file}")
        except Exception as e:
            logger.warning(f"Could not save local article backup: {e}")

        # Editorial cover image prompt (no reference photo bytes under
        # persistent ingestion; live Telethon downloads are retired).
        image_generator = NewsImageGenerator(config, logger)
        image_prompt = await image_generator.generate_prompt(
            title=title,
            lead=lead,
            article_text=markdown_body,
            has_reference_image=False,
        )

        print("\n" + "=" * 70)
        print("📰 ТЕСТОВОЕ ПРЕВЬЮ СТАТЬИ (DRY-RUN)")
        print("=" * 70)
        print(f"Заголовок: {title}")
        print(f"Лид: {lead}\n")
        print(f"Сгенерированный промпт для иллюстрации: {image_prompt}\n")
        print("--- ПОЛНЫЙ ТЕКСТ СТАТЬИ ДЛЯ TELEGRA.PH ---")
        print(markdown_body)
        print("\n--- КАК ЭТО БУДЕТ ВЫГЛЯДЕТЬ В TELEGRAM-КАНАЛЕ (ФОТО-ПОСТ) ---")
        lead_part = f"{lead.strip()}\n\n" if lead.strip() else ""
        mock_url = "https://telegra.ph/Primer-stati-08-14"
        print("[ ФОТО: Иллюстрация к новости ]")
        print(f"📰 *{title.strip()}*\n\n{lead_part}")
        print(f"[ КНОПКА: ⚡️ Читать статью полностью -> {mock_url} ]")
        print("=" * 70 + "\n")

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()
        logger.info(f"Article preview finished in {duration:.1f}s")
        return True
    except Exception as e:
        logger.error(f"Article preview failed: {e}", exc_info=True)
        return False
