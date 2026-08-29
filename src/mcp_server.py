"""MCP server exposing Telebrief digests to MCP clients.

Runs inside the main Telebrief process so that digest generation reuses the same
Telegram session, configuration and lock as the scheduler and the bot.
"""

from __future__ import annotations

import ipaddress
import logging

from mcp.server import MCPServer

from src.collector import Message
from src.config_loader import Config
from src.core import (
    MAX_CHANNEL_MESSAGES,
    MAX_DIGEST_HOURS,
    build_digest,
    collect_channel_messages,
    read_last_digest_async,
    validate_hours,
)


def _is_loopback(host: str) -> bool:
    """Whether host keeps the server reachable only from this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _format_messages(channel: str, messages: list[Message], source: str, hours: int) -> str:
    """Render collected messages as the flat text the model reads."""
    header = f"channel: {channel} (from {source}, {len(messages)} msgs, last {hours}h)"
    body = "\n\n".join(
        f"[{msg.timestamp.isoformat() if msg.timestamp else 'unknown'}] {msg.sender}\n{msg.text}\n{msg.link}"
        for msg in messages
    )
    return f"{header}\n\n{body}"


def build_server(config: Config, logger: logging.Logger) -> MCPServer:
    """Build the MCP server exposing the digest tools.

    Args:
        config: Application configuration
        logger: Logger instance

    Returns:
        Configured MCPServer, not yet running
    """
    if not _is_loopback(config.mcp.host):
        logger.warning(
            f"MCP server binds to {config.mcp.host!r}, which is reachable from the network. "
            "It has no authentication — anyone who can reach this port can trigger digest "
            "generation and read your channels. Bind 127.0.0.1 or firewall the port."
        )

    mcp = MCPServer("telebrief")

    @mcp.tool()
    async def get_digest(hours: int = 24) -> str:
        """Generate a fresh digest of the configured channels using the unified publication pipeline.

        Args:
            hours: How many hours back to look, 1 to 168 (default 24)
        """
        validate_hours(hours)
        from src.core import DIGEST_PUBLICATION_TYPE
        from src.publication.facade import build_publication_preview

        try:
            preview = await build_publication_preview(
                publication_type=DIGEST_PUBLICATION_TYPE,
                lookback_hours=hours,
                config=config,
            )
            parts = []
            if preview.title:
                parts.append(f"📰 {preview.title}")
            if preview.lead:
                parts.append(preview.lead)
            parts.append(preview.body)
            return (
                "\n\n".join(parts)
                if preview.body
                else f"No messages found in the last {hours} hours."
            )
        except Exception as exc:
            logger.warning(
                "Unified publication preview failed in MCP: %s; falling back to build_digest", exc
            )
            digest = await build_digest(config, logger, hours)
            return digest or f"No messages found in the last {hours} hours."

    @mcp.tool()
    async def get_last_digest() -> str:
        """Return the most recently generated digest without regenerating it.

        Instant and free. The digest may be stale — its generation time is included
        in the response, so check whether it is recent enough before relying on it.
        """
        try:
            from src.runtime import get_runtime

            runtime = get_runtime()
            async with runtime.uow.transaction() as conn:
                cur = await conn.execute(
                    """
                    SELECT p.id, p.title, p.lead, p.body, p.created_at
                    FROM publications p
                    WHERE p.publication_type IN ('digest', 'digest_grouped', 'digest_channel')
                      AND (p.metadata->>'preview' IS NULL OR p.metadata->>'preview' != 'true')
                    ORDER BY p.id DESC LIMIT 1
                    """
                )
                row = await cur.fetchone()
                if row is not None:
                    title, lead, body, created_at = row[1], row[2], row[3], row[4]
                    header = f"Digest generated at {created_at.isoformat() if created_at else 'unknown'}:"
                    content = []
                    if title:
                        content.append(f"📰 {title}")
                    if lead:
                        content.append(lead)
                    content.append(body)
                    return f"{header}\n\n" + "\n\n".join(content)
        except Exception as exc:
            logger.debug("Database read for get_last_digest failed: %s, checking disk cache", exc)

        cached = await read_last_digest_async()
        if cached is None:
            return "No digest has been generated yet. Use get_digest to build one."
        return (
            f"Digest generated at {cached.get('generated_at', 'unknown time')} "
            f"covering the previous {cached.get('hours', '?')} hours:\n\n{cached['text']}"
        )

    @mcp.tool()
    async def get_digest_rubrics() -> str:
        """Return the active configured digest rubrics (presentation sections)."""
        rubrics = config.settings.digest_rubrics
        lines = [f"Configured rubrics (min_similarity={rubrics.min_similarity}):"]
        for r in rubrics.items:
            fb = " [FALLBACK]" if r.fallback else ""
            emoji = f"{r.emoji} " if r.emoji else ""
            lines.append(f"- {emoji}{r.id}: {r.name}{fb} - {r.description}")
        return "\n".join(lines)

    @mcp.tool()
    async def get_channel_messages(channel: str, hours: int = 24, limit: int = 200) -> str:
        """Return the individual messages of one configured channel, unsummarized.

        Reads from Telebrief's message store when it holds the requested window, and
        falls back to a live Telegram read otherwise. Free and instant on the stored
        path; the fallback takes a few seconds. The response header says which was used.

        Args:
            channel: Channel name or id as configured under channels[*] in config.yaml
            hours: How many hours back to look, 1 to 168 (default 24)
            limit: Maximum messages to return, 1 to 5000, newest kept (default 200)
        """
        messages, source = await collect_channel_messages(config, logger, channel, hours, limit)
        if not messages:
            return f"No messages in {channel!r} in the last {hours} hours."
        return _format_messages(channel, messages, source, hours)

    logger.info(
        f"MCP tools registered: get_digest (max {MAX_DIGEST_HOURS}h), get_last_digest, "
        f"get_digest_rubrics, get_channel_messages (max {MAX_CHANNEL_MESSAGES} msgs)"
    )
    return mcp
